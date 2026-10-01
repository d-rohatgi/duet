import tempfile
import unittest
from unittest.mock import patch

from duet.core import SyncError
from duet.engine import sync
from duet.setup import setup_playlists, spotify_playlist_id
from duet.storage import Store
from test_sync import FakeProvider

PLAYLIST = "37i9dQZF1DXcBWIGoYBM5M"


class FakeSpotify:
    def __init__(self, owner="her", me="her"):
        self.owner, self.me, self.calls = owner, me, []

    def api(self, path, method="GET", body=None):
        self.calls.append((method, path))
        if path == "me":
            return {"id": self.me}
        if path.startswith("playlists/"):
            return {"id": path.split("/")[1], "name": "Her Mix", "owner": {"id": self.owner}}
        raise AssertionError("unexpected Spotify call: %s %s" % (method, path))


class FakeApple:
    def __init__(self, existing_name=None):
        self.created = []
        self.name = existing_name

    def api(self, path, method="GET", body=None):
        if path == "me/storefront":
            return {"data": [{"id": "us"}]}
        if path.startswith("me/library/playlists?"):
            return {"data": []}
        if path == "me/library/playlists" and method == "POST":
            self.created.append(body["attributes"])
            self.name = body["attributes"]["name"]
            return {"data": [{"id": "p.new"}]}
        if path.startswith("me/library/playlists/"):
            return {"data": [{"attributes": {"name": self.name}}]}
        raise AssertionError("unexpected Apple call: %s %s" % (method, path))

    def snapshot(self):
        return {"name": self.name, "tracks": []}

    def preflight(self, snapshot):
        return None


class LinkTests(unittest.TestCase):
    def test_accepts_share_links_uris_and_ids(self):
        for link in ("https://open.spotify.com/playlist/%s?si=a1b2c3" % PLAYLIST,
                     "https://open.spotify.com/intl-de/playlist/%s" % PLAYLIST,
                     "spotify:playlist:" + PLAYLIST, "  %s  " % PLAYLIST):
            self.assertEqual(spotify_playlist_id(link), PLAYLIST)

    def test_rejects_other_links(self):
        for link in ("https://open.spotify.com/album/%s" % PLAYLIST, "https://spotify.link/abc",
                     "hello", "https://evil.example/playlist/%s" % PLAYLIST):
            with self.assertRaises(SyncError):
                spotify_playlist_id(link)


class AdoptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.config = {"name": "Both of Us", "pair_id": "pair"}

    def setup(self, spotify, apple, link=PLAYLIST):
        with patch("duet.setup.Spotify", return_value=spotify), patch("duet.setup.Apple", return_value=apple), \
                patch("duet.setup.music_bridge", return_value={"id": "LOCAL"}) as bridge, \
                patch("builtins.print"):
            setup_playlists(self.config, self.store, link)
        return bridge

    def test_adopts_her_playlist_and_its_name(self):
        spotify, apple = FakeSpotify(), FakeApple()
        self.setup(spotify, apple)
        self.assertEqual(self.config["spotify_playlist"], PLAYLIST)
        self.assertEqual(self.config["name"], "Her Mix")
        self.assertEqual(apple.created, [{"name": "Her Mix", "description": "Duet pair: pair"}])
        self.assertNotIn("POST", [method for method, _ in spotify.calls])  # nothing created or changed
        self.assertEqual(self.store.read("config")["name"], "Her Mix")

    def test_refuses_a_playlist_owned_by_someone_else(self):
        with self.assertRaisesRegex(SyncError, "another Spotify account"):
            self.setup(FakeSpotify(owner="someone-else"), FakeApple())
        self.assertNotIn("spotify_playlist", self.config)

    def test_can_switch_before_the_first_sync(self):
        # A plain `create` already made an empty Spotify playlist and an Apple
        # playlist, but the Mac hadn't seen the Apple one yet.
        self.config.update(spotify_playlist="EmptyOneDuetMade000000", apple_playlist="p.new", storefront="us")
        bridge = self.setup(FakeSpotify(), FakeApple(existing_name="Both of Us"))
        self.assertEqual(self.config["spotify_playlist"], PLAYLIST)
        self.assertEqual(bridge.call_args[0][0]["name"], "Both of Us")  # found by its real current name

    def test_cannot_switch_after_syncing(self):
        self.config.update(spotify_playlist="EmptyOneDuetMade000000", apple_playlist="p.new", storefront="us")
        self.store.write("state", {"baseline": {"name": "Both of Us", "keys": []}})
        with self.assertRaisesRegex(SyncError, "already synced"):
            self.setup(FakeSpotify(), FakeApple(existing_name="Both of Us"))
        self.assertEqual(self.config["spotify_playlist"], "EmptyOneDuetMade000000")


class FirstSyncTests(unittest.TestCase):
    def test_first_sync_of_an_existing_playlist_never_writes_to_spotify(self):
        with tempfile.TemporaryDirectory() as folder:
            spotify = FakeProvider("spotify", [1, 2, 3, 9], name="Her Mix")
            apple = FakeProvider("apple", [], name="Her Mix")
            apple.missing.add(9)  # one of her songs isn't on Apple Music
            config = {"name": "Her Mix", "spotify_playlist": PLAYLIST, "apple_playlist": "p.new"}
            sync(config, Store(folder), {"spotify": spotify, "apple": apple})
        self.assertEqual(spotify.writes, 0)
        self.assertEqual(sorted(t["id"] for t in apple.value["tracks"]), ["1", "2", "3"])
        self.assertEqual(apple.value["name"], "Her Mix")


if __name__ == "__main__":
    unittest.main()
