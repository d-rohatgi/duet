from copy import deepcopy
from datetime import datetime, timezone
import uuid

from .core import (NoMatch, SyncError, assert_recoverable, canonicalize,
                   choose_match, delta, fingerprint, merge)


def now():
    return datetime.now(timezone.utc).isoformat()


def link_tracks(snapshots, providers, existing):
    links = deepcopy(existing)
    for side in ("spotify", "apple"):
        other = "apple" if side == "spotify" else "spotify"
        for track in snapshots[side]["tracks"]:
            known = next((key for key, link in links.items()
                          if link.get(side, {}).get("id") == track["id"]), None)
            if known:
                links[known][side] = track
                continue
            try:
                match = choose_match(track, snapshots[other]["tracks"])
            except NoMatch as error:
                if str(error).startswith("Ambiguous"):
                    raise
                try:
                    match = providers[other].find(track)
                except NoMatch:
                    continue  # Not on the other service; skipped and retried next run.
            key = next((key for key, link in links.items()
                        if link.get(other, {}).get("id") == match["id"]), None)
            if key and links[key].get(side, {}).get("id") not in (None, track["id"]):
                raise SyncError("Multiple editions map to one song: " + track["title"])
            key = key or side + ":" + track["id"]
            links.setdefault(key, {})[side] = track
            links[key][other] = match
    linked = {side: {link[side]["id"] for link in links.values() if side in link}
              for side in ("spotify", "apple")}
    skipped = {side: [t for t in snapshots[side]["tracks"] if t["id"] not in linked[side]]
               for side in ("spotify", "apple")}
    return links, skipped


def ignored(plan, side):
    return {t["id"] for t in plan.get("skipped", {}).get(side, [])}


def plan_sync(config, store, providers):
    # Read both services fully, twice, before deriving any deletion.
    snapshots = {p: providers[p].snapshot() for p in providers}
    state = store.read("state", {})
    links, skipped = link_tracks(snapshots, providers, state.get("links", {}))
    ignore = {p: {t["id"] for t in skipped[p]} for p in providers}
    current = {p: canonicalize(snapshots[p], p, links, ignore[p]) for p in providers}
    target = merge(state.get("baseline"), current, config["name"], config.get("rename_priority", "apple"))
    for p in providers:
        again = providers[p].snapshot()
        if fingerprint(again) != fingerprint(snapshots[p]):
            raise SyncError("%s changed during planning. Retry sync." % p)
        providers[p].preflight(again)
    # Songs without catalog IDs can remain in Apple, but cannot be added there.
    for key in delta(current["apple"], target)["add"]:
        if not links[key]["apple"]["id"].isdigit():
            raise SyncError("Song has no Apple catalog equivalent: " + links[key]["apple"]["title"])
    return {"id": str(uuid.uuid4()), "created_at": now(), "before": current,
            "snapshots": snapshots, "target": target, "links": links, "skipped": skipped,
            "playlists": {p: config[p + "_playlist"] for p in providers}}


def describe(plan):
    result = {"name": plan["target"]["name"], "songs": len(plan["target"]["keys"])}
    for side in ("spotify", "apple"):
        changes = delta(plan["before"][side], plan["target"])
        result[side] = {
            "add": [plan["links"][k][side]["title"] for k in changes["add"]],
            "remove": [plan["links"][k][side]["title"] for k in changes["remove"]],
            "rename": changes["rename"],
            "not_synced": ["%s — %s" % (t["title"], t["artist"]) for t in plan.get("skipped", {}).get(side, [])],
        }
    return result


def sync(config, store, providers, preview=False):
    pending = store.read("pending")
    state = store.read("state", {})
    if pending and state.get("last_plan_id") == pending["id"]:
        # Crash after durable checkpoint but before deleting the journal.
        if not preview:
            store.remove("pending")
        pending = None
    plan = pending or plan_sync(config, store, providers)
    if plan["playlists"] != {p: config[p + "_playlist"] for p in providers}:
        raise SyncError("Playlist configuration changed while a sync was pending.")
    if preview:
        return describe(plan)
    if not pending:
        store.write("pending", plan)
    target, links = plan["target"], plan["links"]
    # Validate both endpoints before the first mutation, including recovery.
    for side in providers:
        raw = providers[side].snapshot()
        current = canonicalize(raw, side, links, ignored(plan, side))
        assert_recoverable(plan["before"][side], current, target)
        providers[side].preflight(raw)
    for side in ("apple", "spotify"):
        raw = providers[side].snapshot()
        current = canonicalize(raw, side, links, ignored(plan, side))
        assert_recoverable(plan["before"][side], current, target)
        changes = delta(current, target)
        if changes["add"] or changes["remove"] or changes["rename"]:
            # Skipped songs are part of the desired playlist, so apply keeps them.
            keep = [t for t in raw["tracks"] if t["id"] in ignored(plan, side)]
            providers[side].apply([links[k][side] for k in target["keys"]] + keep, target["name"], raw)
    # Baseline advances only after both independent services confirm the result.
    for side in providers:
        raw = providers[side].snapshot()
        actual = canonicalize(raw, side, links, ignored(plan, side))
        if actual["name"] != target["name"] or set(actual["keys"]) != set(target["keys"]):
            raise SyncError("%s has not converged. Pending sync saved; retry shortly." % side)
        providers[side].preflight(raw)
    store.write("state", {"baseline": target, "links": links, "skipped": plan.get("skipped", {}),
                          "last_success": now(), "last_plan_id": plan["id"]})
    store.remove("pending")
    return describe(plan)
