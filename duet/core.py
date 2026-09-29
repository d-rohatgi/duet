"""Pure reconciliation rules; provider IDs are linked before calling merge."""
import hashlib
import json
import re
import unicodedata


class SyncError(RuntimeError):
    pass


class NoMatch(SyncError):
    """The other service has no single, safe equivalent recording."""


def norm(value):
    value = unicodedata.normalize("NFKD", value).casefold()
    return " ".join(re.sub(r"[^\w\s]", " ", value).split())


def unique(values):
    return list(dict.fromkeys(values))


def metadata_match(a, b):
    """Never strip 'live', 'remix', or other edition identifiers from titles."""
    if not a.get("duration_ms") or not b.get("duration_ms"):
        return False
    if abs(a["duration_ms"] - b["duration_ms"]) > 2500:
        return False
    if norm(a["title"]) != norm(b["title"]):
        return False
    if a.get("explicit") is not None and b.get("explicit") is not None:
        if a["explicit"] != b["explicit"]:
            return False
    return norm(a["artist"]) == norm(b["artist"])


def choose_match(source, candidates):
    candidates = {c["id"]: c for c in candidates}.values()
    exact = [c for c in candidates if source.get("isrc")
             and c.get("isrc") == source["isrc"]
             and not (source.get("explicit") is not None
                      and c.get("explicit") is not None
                      and source["explicit"] != c["explicit"])]
    matches = exact or [c for c in candidates if metadata_match(source, c)]
    if len(matches) > 1:
        album = [c for c in matches if norm(c.get("album", ""))
                 == norm(source.get("album", ""))]
        if album:
            matches = album
    if len(matches) == 1:
        return matches[0]
    # Identical ISRC editions of the same recording are acceptable when their
    # explicit flags agree. Use a stable ID, never search-result rank.
    if matches and exact and len({c.get("explicit") for c in matches}) == 1:
        return sorted(matches, key=lambda c: c["id"])[0]
    label = "%s — %s" % (source["title"], source["artist"])
    raise NoMatch(("Ambiguous match: " if matches else "No safe match: ") + label)


def fingerprint(snapshot):
    # This project syncs membership and name. Manual track reordering is local.
    payload = [snapshot["name"], sorted(t["id"] for t in snapshot["tracks"])]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()


def merge(baseline, current, initial_name, priority="apple"):
    """Snapshot three-way merge: additions combine; observed deletions win."""
    first = "spotify" if priority == "apple" else "apple"
    order = [first, priority]
    if baseline is None:
        keys = unique(current[first]["keys"] + current[priority]["keys"])
        return {"name": initial_name, "keys": keys}
    previous = set(baseline["keys"])
    removed = set().union(*(previous - set(current[p]["keys"]) for p in order))
    keys = [k for k in baseline["keys"] if k not in removed]
    for p in order:
        keys.extend(k for k in current[p]["keys"] if k not in previous)
    name = baseline["name"]
    for p in order:
        if current[p]["name"] != baseline["name"]:
            name = current[p]["name"]
    return {"name": name, "keys": unique(keys)}


def delta(before, target):
    return {
        "add": [x for x in target["keys"] if x not in before["keys"]],
        "remove": [x for x in before["keys"] if x not in target["keys"]],
        "rename": target["name"] if before["name"] != target["name"] else None,
    }


def assert_recoverable(before, current, target):
    """Permit only intermediate states of the saved plan; reject new edits."""
    old, now, goal = map(lambda s: set(s["keys"]), (before, current, target))
    if (now - old) - (goal - old) or (old - now) - (old - goal):
        raise SyncError("Playlist changed outside the pending sync. Review pending.json before continuing.")
    if current["name"] not in {before["name"], target["name"]}:
        raise SyncError("Playlist renamed during a pending sync; review pending.json.")


def canonicalize(snapshot, provider, links, ignore=()):
    """Map provider tracks to shared keys, leaving out songs skipped as unmatched."""
    reverse = {v[provider]["id"]: k for k, v in links.items() if provider in v}
    keys = []
    for track in snapshot["tracks"]:
        if track["id"] in ignore:
            continue
        if track["id"] not in reverse:
            raise SyncError("Unlinked song appeared during sync: " + track["title"])
        keys.append(reverse[track["id"]])
    if len(keys) != len(set(keys)):
        raise SyncError("Duplicate recordings in %s playlist. Remove duplicates before syncing." % provider)
    return {"name": snapshot["name"], "keys": keys}
