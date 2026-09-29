import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse, quote
from urllib.request import Request, urlopen

from .core import SyncError, choose_match, fingerprint, metadata_match


class HTTPFailure(SyncError):
    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


def request_json(url, method="GET", body=None, headers=None, form=None):
    headers = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if form is not None:
        data = urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    # Never blindly retry writes: a timed-out write might already have succeeded.
    for attempt in range(3 if method == "GET" else 1):
        try:
            with urlopen(Request(url, data=data, headers=headers, method=method), timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except HTTPError as error:
            if method == "GET" and attempt < 2 and error.code in (429, 502, 503):
                try:
                    delay = min(10, max(1, int(error.headers.get("Retry-After", "2"))))
                except ValueError:
                    delay = 2
                time.sleep(delay)
                continue
            # Response bodies can contain account data; keep logs limited.
            raise HTTPFailure("%s %s returned HTTP %s. Check authorization, permissions, and rate limits." %
                              (method, urlparse(url).path, error.code), error.code) from None
        except (URLError, TimeoutError, OSError) as error:
            raise SyncError("Network request failed for %s. Retry to reconcile any pending writes." %
                            urlparse(url).hostname) from error


def pages(first, fetch, field="data"):
    result, seen, url = [], set(), first
    while url:
        if url in seen:
            raise SyncError("Provider returned a pagination loop; no sync performed.")
        seen.add(url)
        page = fetch(url)
        if field not in page or not isinstance(page[field], list):
            raise SyncError("Incomplete playlist response; refusing to interpret it as an empty playlist.")
        result.extend(page[field])
        url = page.get("next")
    return result


def chunks(values, size=100):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def spotify_track(item):
    if not item or item.get("type") != "track" or item.get("is_local") or not item.get("id"):
        raise SyncError("Spotify playlist contains a local, unavailable, or non-song item. Remove it before syncing.")
    return {"id": item["id"], "title": item["name"],
            "artist": ", ".join(a["name"] for a in item["artists"]),
            "album": item.get("album", {}).get("name", ""),
            "duration_ms": item["duration_ms"], "explicit": item.get("explicit"),
            "isrc": item.get("external_ids", {}).get("isrc")}


def apple_track(item):
    if item.get("type") not in ("songs", "library-songs"):
        raise SyncError("Apple playlist contains a non-song item. Remove it before syncing.")
    a = item["attributes"]
    catalog = a.get("playParams", {}).get("catalogId")
    return {"id": str(catalog or item["id"]), "library_id": item["id"],
            "title": a["name"], "artist": a["artistName"], "album": a.get("albumName", ""),
            "duration_ms": a["durationInMillis"],
            "explicit": a.get("contentRating") == "explicit", "isrc": a.get("isrc")}


class Spotify:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.playlist = config.get("spotify_playlist")

    def token(self):
        secrets = self.store.read("secrets", {})
        token = secrets.get("spotify", {})
        if token.get("expires_at", 0) > time.time() + 60:
            return token["access_token"]
        if not token.get("refresh_token"):
            raise SyncError("Connect Spotify first: python3 -m duet connect-spotify")
        fresh = request_json("https://accounts.spotify.com/api/token", method="POST", form={
            "grant_type": "refresh_token", "refresh_token": token["refresh_token"],
            "client_id": self.config["spotify_client_id"]})
        fresh["refresh_token"] = fresh.get("refresh_token", token["refresh_token"])
        fresh["expires_at"] = time.time() + fresh["expires_in"]
        secrets["spotify"] = fresh
        self.store.write("secrets", secrets)
        return fresh["access_token"]

    def api(self, path, method="GET", body=None):
        url = urljoin("https://api.spotify.com/v1/", path)
        if urlparse(url).scheme != "https" or urlparse(url).netloc != "api.spotify.com":
            raise SyncError("Unexpected Spotify pagination host.")
        return request_json(url, method, body, {"Authorization": "Bearer " + self.token()})

    def snapshot(self):
        if not self.playlist:
            raise SyncError("Set up the playlist pair first.")
        before = self.api("playlists/" + self.playlist)
        raw = pages("playlists/%s/items?limit=50" % self.playlist, self.api, "items")
        # A snapshot ID protects the multi-page read from concurrent changes.
        after = self.api("playlists/" + self.playlist)
        if before["snapshot_id"] != after["snapshot_id"] or before["name"] != after["name"]:
            raise SyncError("Spotify changed while reading it. Retry sync.")
        total = after.get("items", after.get("tracks", {})).get("total")
        if total is not None and total != len(raw):
            raise SyncError("Incomplete Spotify track listing.")
        tracks = [spotify_track(x.get("item", x.get("track"))) for x in raw]
        return {"name": after["name"], "tracks": tracks, "version": after["snapshot_id"]}

    def find(self, source):
        def search(query):
            data = self.api("search?" + urlencode({"q": query, "type": "track", "limit": 10}))
            return [spotify_track(t) for t in data["tracks"]["items"] if t.get("is_playable", True)]
        candidates = search("isrc:" + source["isrc"]) if source.get("isrc") else []
        if not candidates:
            candidates = search('track:"%s" artist:"%s"' % (source["title"], source["artist"]))
        return choose_match(source, candidates)

    def preflight(self, snapshot):
        return None

    def apply(self, target, name, before):
        current = self.snapshot()
        if fingerprint(current) != fingerprint(before):
            raise SyncError("Spotify changed before writing; retry sync.")
        desired = {t["id"] for t in target}
        existing = {t["id"] for t in current["tracks"]}
        removals = sorted(existing - desired)
        for batch in chunks(removals):
            self.api("playlists/%s/items" % self.playlist, "DELETE", {
                "items": [{"uri": "spotify:track:" + x} for x in batch],
                "snapshot_id": current["version"]})
            expected = existing - set(batch)
            current = self.snapshot()
            if {t["id"] for t in current["tracks"]} != expected or current["name"] != before["name"]:
                raise SyncError("Spotify changed during removal; pending sync saved.")
            existing = expected
        additions = [t["id"] for t in target if t["id"] not in existing]
        for batch in chunks(additions):
            self.api("playlists/%s/items" % self.playlist, "POST", {
                "uris": ["spotify:track:" + x for x in batch]})
            existing.update(batch)
            current = self.snapshot()
            if {t["id"] for t in current["tracks"]} != existing or current["name"] != before["name"]:
                raise SyncError("Spotify changed during addition; pending sync saved.")
        if current["name"] != name:
            self.api("playlists/" + self.playlist, "PUT", {"name": name})


def music_bridge(payload):
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        process = subprocess.run(["/usr/bin/osascript", "-l", "JavaScript",
                                  str(Path(__file__).with_name("bridge.js")), path],
                                 capture_output=True, text=True, timeout=60)
        if process.returncode:
            raise SyncError("Music automation failed: " + process.stderr.strip())
        return json.loads(process.stdout)
    finally:
        os.unlink(path)


class Apple:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.playlist = config.get("apple_playlist")

    def api(self, path, method="GET", body=None):
        from .auth import developer_token
        secrets = self.store.read("secrets", {})
        user_token = secrets.get("apple_user_token")
        if not user_token:
            raise SyncError("Connect Apple Music first: python3 -m duet connect-apple")
        url = urljoin("https://api.music.apple.com/v1/", path)
        if urlparse(url).scheme != "https" or urlparse(url).netloc != "api.music.apple.com":
            raise SyncError("Unexpected Apple pagination host.")
        return request_json(url, method, body, {
            "Authorization": "Bearer " + developer_token(self.config),
            "Music-User-Token": user_token})

    def snapshot(self):
        if not self.playlist:
            raise SyncError("Set up the playlist pair first.")
        path = "me/library/playlists/" + quote(self.playlist, safe="")
        before = self.api(path)["data"][0]
        if before["attributes"].get("canEdit") is not True:
            raise SyncError("Apple playlist is not editable by this account.")
        first = path + "/tracks?limit=100"

        def fetch(url):
            try:
                return self.api(url)
            except HTTPFailure as error:
                # Apple answers 404, not an empty list, for a playlist with no
                # tracks. Only the first page qualifies; the playlist itself was
                # just read, and preflight compares against the Mac copy.
                if error.status == 404 and url == first:
                    return {"data": []}
                raise
        raw = pages(first, fetch)
        tracks = [apple_track(t) for t in raw]
        # Catalog metadata exposes ISRC; library-only uploads may lack it.
        for batch in chunks([t["id"] for t in tracks if t["id"].isdigit()]):
            catalog = self.api("catalog/%s/songs?%s" %
                               (self.config["storefront"], urlencode({"ids": ",".join(batch)})))
            by_id = {x["id"]: x["attributes"] for x in catalog["data"]}
            for track in tracks:
                if track["id"] in by_id:
                    track["isrc"] = by_id[track["id"]].get("isrc")
        after = self.api(path)["data"][0]
        a, b = before["attributes"], after["attributes"]
        if a["name"] != b["name"] or a.get("lastModifiedDate") != b.get("lastModifiedDate"):
            raise SyncError("Apple playlist changed while reading it. Retry sync.")
        return {"name": b["name"], "tracks": tracks}

    def find(self, source):
        prefix = "catalog/%s/" % self.config["storefront"]
        candidates = []
        if source.get("isrc"):
            result = self.api(prefix + "songs?" + urlencode({"filter[isrc]": source["isrc"]}))
            candidates = [apple_track(t) for t in result["data"] if t["attributes"].get("playParams")]
        if not candidates:
            result = self.api(prefix + "search?" + urlencode({"term": source["title"] + " " + source["artist"],
                                                             "types": "songs", "limit": 25}))
            candidates = [apple_track(t) for t in result.get("results", {}).get("songs", {}).get("data", [])
                          if t["attributes"].get("playParams")]
        return choose_match(source, candidates)

    def local(self):
        return music_bridge({"action": "snapshot", "id": self.config["apple_local_id"],
                             "marker": "Duet pair: " + self.config["pair_id"]})

    def preflight(self, snapshot):
        local = self.local()
        if local["name"] != snapshot["name"] or len(local["tracks"]) != len(snapshot["tracks"]):
            raise SyncError("Mac Music and Apple cloud playlist differ. Enable Sync Library, let Music finish syncing, then retry.")
        mapping, used = {}, set()
        for remote in snapshot["tracks"]:
            matches = [t for t in local["tracks"] if t["id"] not in used and metadata_match(remote, t)]
            if len(matches) != 1:
                raise SyncError("Cannot safely identify a local track for deletion: " + remote["title"])
            mapping[remote["id"]] = matches[0]["id"]
            used.add(matches[0]["id"])
        return local, mapping

    def apply(self, target, name, before):
        current = self.snapshot()
        if fingerprint(current) != fingerprint(before):
            raise SyncError("Apple changed before writing; retry sync.")
        local, mapping = self.preflight(current)
        desired = {t["id"] for t in target}
        existing = {t["id"] for t in current["tracks"]}
        removals = existing - desired
        if removals or current["name"] != name:
            music_bridge({"action": "edit", "id": self.config["apple_local_id"],
                          "marker": "Duet pair: " + self.config["pair_id"],
                          "expected": local, "remove_ids": [mapping[x] for x in removals],
                          "rename": name if current["name"] != name else None})
            self.await_state(name, existing - removals)
        for batch in chunks([t for t in target if t["id"] not in existing]):
            self.api("me/library/playlists/%s/tracks" % self.playlist, "POST", {
                "data": [{"id": t["id"], "type": "songs"} for t in batch]})
            existing.update(t["id"] for t in batch)
            self.await_state(name, existing - removals)

    def await_state(self, name, ids):
        # Cloud propagation is asynchronous. Bounded polling leaves a durable
        # pending plan if convergence takes longer than this run.
        for attempt in range(8):
            current = self.snapshot()
            if current["name"] == name and {t["id"] for t in current["tracks"]} == ids:
                try:
                    self.preflight(current)
                    return
                except SyncError:
                    pass  # The cloud may converge before the local Music app.
            time.sleep(3)
        raise SyncError("Waiting for Apple cloud and Mac Music to converge. Pending sync saved; retry shortly.")
