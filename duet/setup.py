"""One-time setup: pair a Spotify playlist with an Apple Music playlist."""
import re
from urllib.parse import quote

from .core import SyncError
from .providers import Apple, Spotify, music_bridge, pages

PLAYLIST_LINK = re.compile(r"(?:https://open\.spotify\.com/(?:intl-[\w-]+/)?playlist/|spotify:playlist:)?"
                           r"([A-Za-z0-9]{22})(?:\?\S*)?")


def spotify_playlist_id(link):
    """Accept a share link, a spotify:playlist: URI, or a bare playlist ID."""
    match = PLAYLIST_LINK.fullmatch(link.strip())
    if not match:
        raise SyncError("That isn't a Spotify playlist link. In Spotify, use Share → Copy link to playlist; "
                        "it starts with https://open.spotify.com/playlist/")
    return match.group(1)


def adopt_spotify_playlist(config, store, spotify, link):
    """Use an existing Spotify playlist instead of creating one. Its name becomes the shared name."""
    playlist_id = spotify_playlist_id(link)
    previous = config.get("spotify_playlist")
    if previous not in (None, playlist_id):
        state = store.read("state", {})
        if state.get("baseline") or store.read("pending"):
            raise SyncError("This pair has already synced with a different Spotify playlist, so it can't be switched.")
    playlist = spotify.api("playlists/" + playlist_id)
    if playlist["owner"]["id"] != spotify.api("me")["id"]:
        raise SyncError('"%s" belongs to another Spotify account. Duet can only use a playlist owned by the '
                        "Spotify account you connected." % playlist["name"])
    if previous not in (None, playlist_id):
        print("Switched from the empty Spotify playlist Duet created earlier; you can delete that one in Spotify.")
    # Keeping her name means the first sync never renames her playlist.
    config.update(spotify_playlist=playlist_id, name=playlist["name"])
    store.write("config", config)
    print('Using the Spotify playlist "%s".' % playlist["name"])


def setup_playlists(config, store, spotify_link=None):
    spotify, apple = Spotify(config, store), Apple(config, store)
    # Store each created playlist ID immediately. A rerun resumes setup.
    # When creation timed out, look for its unique setup marker before retrying.
    marker = "Duet pair: " + config["pair_id"]
    if not config.get("storefront"):
        config["storefront"] = apple.api("me/storefront")["data"][0]["id"]
        store.write("config", config)
    if spotify_link:
        adopt_spotify_playlist(config, store, spotify, spotify_link)
    name = config["name"]
    if not config.get("spotify_playlist"):
        existing = pages("me/playlists?limit=50", spotify.api, "items")
        matches = [p for p in existing if p.get("description") == marker]
        if len(matches) > 1:
            raise SyncError("Multiple Spotify playlists have this pair marker; review setup.")
        playlist = matches[0] if matches else spotify.api("me/playlists", "POST", {
            "name": name, "public": False, "description": marker})
        config["spotify_playlist"] = playlist["id"]
        store.write("config", config)
    if not config.get("apple_playlist"):
        existing = pages("me/library/playlists?limit=100", apple.api)
        matches = [p for p in existing if p.get("attributes", {}).get("description", {}).get("standard") == marker]
        if len(matches) > 1:
            raise SyncError("Multiple Apple playlists have this pair marker; review setup.")
        playlist = matches[0] if matches else apple.api("me/library/playlists", "POST", {
            "attributes": {"name": name, "description": marker}})["data"][0]
        config["apple_playlist"] = playlist["id"]
        store.write("config", config)
    if not config.get("apple_local_id"):
        # Look up by the Apple playlist's current name, which differs from the
        # shared name if setup switched to an existing Spotify playlist midway.
        current = apple.api("me/library/playlists/" + quote(config["apple_playlist"], safe=""))
        local = music_bridge({"action": "snapshot", "name": current["data"][0]["attributes"]["name"],
                              "marker": marker})
        config["apple_local_id"] = local["id"]
        store.write("config", config)
    # Verify that the local and remote copies really agree before linking.
    Apple(config, store).preflight(Apple(config, store).snapshot())
    print("Playlist pair ready: " + name)
    print("Next, see what the first sync will do: python3 -m duet preview")
