import subprocess
import tempfile
import unittest
from unittest.mock import patch

from duet import jobs, texts
from duet.core import SyncError
from duet.storage import Store
from test_sync import FakeProvider

NAMES = {"apple": "big dawg", "spotify": "lil bug"}


def result(**sides):
    empty = {"add": [], "remove": [], "rename": None, "not_synced": []}
    return {"name": "Big dawg & lil bug", "songs": 3,
            **{side: dict(empty, **sides.get(side, {})) for side in ("apple", "spotify")}}


class ComposeTests(unittest.TestCase):
    def test_changes_are_credited_to_whoever_made_them(self):
        # Songs added to Apple Music came from her Spotify, and the reverse.
        message = texts.compose(result(apple={"add": ["Opalite"]}, spotify={"add": ["Glamorama"]}), NAMES)
        self.assertEqual(message.splitlines(), ["🎶 big dawg added Glamorama", "🎶 lil bug added Opalite"])

    def test_removals_and_renames(self):
        message = texts.compose(result(apple={"remove": ["Cough Syrup"], "rename": "Road trip"}), NAMES)
        self.assertEqual(message.splitlines(), ["🗑️ lil bug removed Cough Syrup",
                                                '✏️ lil bug renamed the playlist to "Road trip"'])

    def test_removals_can_be_turned_off(self):
        self.assertIsNone(texts.compose(result(apple={"remove": ["Cough Syrup"]}), NAMES, removals=False))

    def test_nothing_changed_sends_nothing(self):
        self.assertIsNone(texts.compose(result(apple={"not_synced": ["X — Y"]}), NAMES))

    def test_song_lists_read_naturally(self):
        self.assertEqual(texts.listing(["A"]), "A")
        self.assertEqual(texts.listing(["A", "B"]), "A and B")
        self.assertEqual(texts.listing(["A", "B", "C"]), "A, B and C")
        self.assertEqual(texts.listing(list("ABCDEFG")), "A, B, C, D, E and 2 more")


class SendTests(unittest.TestCase):
    def test_chat_and_message_are_arguments_not_script(self):
        tricky = 'Opalite" & (do shell script "echo hi") & "'
        with patch("duet.texts.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            texts.send({"chat": "Big dawg & lil bug 🎶"}, tricky)
        command = run.call_args[0][0]
        self.assertEqual(command[-2:], ["Big dawg & lil bug 🎶", tricky])
        self.assertTrue(all(tricky not in part for part in command[:-1]))

    def test_one_on_one_chat_is_addressed_by_phone_number(self):
        with patch("duet.texts.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            texts.send({"to": "+15555550123"}, "hi")
        command = run.call_args[0][0]
        self.assertEqual(command[-2:], ["+15555550123", "hi"])
        self.assertIn("participant (item 1 of argv)", " ".join(command))
        self.assertNotIn("chats whose name", " ".join(command))

    def test_timeout_explains_permission_without_leaking_the_number(self):
        with patch("duet.texts.subprocess.run", side_effect=subprocess.TimeoutExpired(["osascript", "+15555550123"], 30)):
            with self.assertRaises(SyncError) as caught:
                texts.send({"to": "+15555550123"}, "🎶 secret song")
        self.assertIn("permissions", str(caught.exception))
        self.assertNotIn("+15555550123", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)  # tracebacks won't show the command

    def test_failure_is_reported(self):
        with patch("duet.texts.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 1, "", "Expected exactly one Messages chat")):
            with self.assertRaisesRegex(SyncError, "Messages chat"):
                texts.send({"chat": "Missing chat"}, "hi")


class AnnounceDuringSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.config = {"name": "Together", "timezone": "UTC", "spotify_playlist": "s", "apple_playlist": "a",
                       "apple_local_id": "L", "storefront": "us",
                       "texts": {"chat": "Us 🎶", "removals": True, "names": NAMES}}
        self.store.write("config", self.config)
        fakes = {"spotify": FakeProvider("spotify", [1]), "apple": FakeProvider("apple", [2])}
        for side, cls in (("spotify", "Spotify"), ("apple", "Apple")):
            patcher = patch("duet.jobs." + cls, return_value=fakes[side])
            patcher.start()
            self.addCleanup(patcher.stop)
        for target in ("duet.alerts.notify", "duet.jobs.log"):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_sync_posts_its_changes(self):
        with patch("duet.texts.send") as send:
            jobs.run_sync(self.store, "sync", "manual")
        send.assert_called_once_with(self.config["texts"], "🎶 big dawg added Song 2\n🎶 lil bug added Song 1")

    def test_a_failed_post_never_fails_the_sync(self):
        with patch("duet.texts.send", side_effect=SyncError("Messages isn't signed in")):
            jobs.run_sync(self.store, "sync", "manual")
        self.assertIsNotNone(self.store.read("state")["last_success"])
        jobs.log.assert_called_once()

    def test_no_chat_configured_posts_nothing(self):
        del self.config["texts"]
        self.store.write("config", self.config)
        with patch("duet.texts.send") as send:
            jobs.run_sync(self.store, "sync", "manual")
        send.assert_not_called()

    def test_preview_never_posts(self):
        with patch("duet.texts.send") as send:
            jobs.run_sync(self.store, "preview", "manual")
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
