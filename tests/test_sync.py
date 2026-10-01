from copy import deepcopy
from datetime import datetime, timedelta, timezone
import itertools
import tempfile
import unittest
from unittest.mock import patch

from duet import alerts
from duet.core import (NoMatch, SyncError, assert_recoverable, canonicalize, choose_match,
                       merge, metadata_match, relink, same_recording, same_songs)
from duet.engine import sync
from duet.providers import pages, apple_track, spotify_track
from duet.storage import Store


def track(number, provider="apple", **overrides):
    result = {"id": str(number) if provider == "apple" else "s" + str(number),
              "title": "Song " + str(number), "artist": "Artist", "album": "Album",
              "duration_ms": 180000, "explicit": False, "isrc": "ISRC" + str(number)}
    result.update(overrides)
    return result


class FakeProvider:
    def __init__(self, side, numbers=(), name="Together"):
        self.side = side
        self.value = {"name": name, "tracks": [track(n, side) for n in numbers]}
        self.writes = 0
        self.missing = set()
        self.fail_after_write = False
        self.error = None
        self.preflight_error = None

    def snapshot(self):
        if self.error:
            raise SyncError(self.error)
        return deepcopy(self.value)

    def find(self, source):
        number = int(source["isrc"].removeprefix("ISRC"))
        if number in self.missing:
            raise NoMatch("No safe match: " + source["title"])
        return track(number, self.side)

    def preflight(self, snapshot):
        if self.preflight_error:
            raise SyncError(self.preflight_error)

    def apply(self, target, name, before):
        self.writes += 1
        self.value = {"name": name, "tracks": deepcopy(target)}
        if self.fail_after_write:
            self.fail_after_write = False
            raise SyncError("Response lost after successful write")


class ReidentifyingApple(FakeProvider):
    """Like Apple: after adding songs, it may swap them for another edition of the
    same recording (new ID, same ISRC) or for a copy already in the library (new
    ID, no ISRC, no explicit rating, slightly different length)."""

    def __init__(self, *args, swaps=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.swaps = swaps or {}

    def apply(self, target, name, before):
        try:
            super().apply(target, name, before)
        finally:
            for t in self.value["tracks"]:
                t.update(self.swaps.get(t["id"], {}))


EDITION = {"id": "1001"}  # same ISRC
LIBRARY_COPY = {"id": "i.library", "isrc": None, "explicit": True, "duration_ms": 181500}


class MergeTests(unittest.TestCase):
    def test_initial_union_is_not_a_deletion(self):
        current = {"apple": {"name": "A", "keys": ["a"]}, "spotify": {"name": "S", "keys": ["b"]}}
        self.assertEqual(merge(None, current, "Together"), {"name": "Together", "keys": ["b", "a"]})

    def test_additions_deletions_and_rename(self):
        before = {"name": "Together", "keys": ["a", "b"]}
        current = {"apple": {"name": "Road trip", "keys": ["b", "c"]},
                   "spotify": {"name": "Together", "keys": ["a", "b", "d"]}}
        self.assertEqual(merge(before, current, "Together"), {"name": "Road trip", "keys": ["b", "d", "c"]})

    def test_conflicting_renames_have_fixed_priority(self):
        current = {"apple": {"name": "A", "keys": []}, "spotify": {"name": "S", "keys": []}}
        self.assertEqual(merge({"name": "Old", "keys": []}, current, "", "apple")["name"], "A")
        self.assertEqual(merge({"name": "Old", "keys": []}, current, "", "spotify")["name"], "S")

    def test_one_sided_rename_propagates(self):
        current = {"apple": {"name": "Old", "keys": []}, "spotify": {"name": "New", "keys": []}}
        self.assertEqual(merge({"name": "Old", "keys": []}, current, "")["name"], "New")

    def test_exhaustive_membership_and_idempotence(self):
        # All histories representable by three snapshots across three songs.
        universe = ["a", "b", "c"]
        subsets = [list(itertools.compress(universe, bits)) for bits in itertools.product([False, True], repeat=3)]
        for base, apple, spotify in itertools.product(subsets, repeat=3):
            old = {"name": "N", "keys": base}
            current = {"apple": {"name": "N", "keys": apple}, "spotify": {"name": "N", "keys": spotify}}
            target = merge(old, current, "N")
            expected = (set(base) & set(apple) & set(spotify)) | ((set(apple) | set(spotify)) - set(base))
            self.assertEqual(set(target["keys"]), expected)
            self.assertEqual(merge(target, {"apple": target, "spotify": target}, "N"), target)

    def test_pending_new_user_edit_is_rejected(self):
        with self.assertRaises(SyncError):
            assert_recoverable({"name": "N", "keys": ["a"]},
                               {"name": "N", "keys": ["a", "c"]}, {"name": "N", "keys": ["b"]})

    def test_pending_partial_write_is_allowed(self):
        assert_recoverable({"name": "N", "keys": ["a", "b"]},
                           {"name": "N", "keys": ["b"]}, {"name": "New", "keys": ["b", "c"]})


class MatchingTests(unittest.TestCase):
    def test_isrc_is_preferred(self):
        wanted = track(1)
        self.assertEqual(choose_match(wanted, [track(2), track(1, "spotify")])["id"], "s1")

    def test_no_fuzzy_live_or_explicit_substitution(self):
        source = track(1, isrc=None)
        self.assertFalse(metadata_match(source, track(1, title="Song 1 (Live)")))
        self.assertFalse(metadata_match(source, track(1, explicit=True)))
        self.assertFalse(metadata_match(source, track(1, duration_ms=200000)))

    def test_ambiguous_metadata_is_rejected(self):
        source = track(1, isrc=None)
        with self.assertRaisesRegex(SyncError, "Ambiguous"):
            choose_match(source, [track(1, id="a", isrc=None), track(1, id="b", isrc=None)])

    def test_duplicate_recordings_are_rejected(self):
        links = {"one": {"apple": track(1)}}
        with self.assertRaisesRegex(SyncError, "Duplicate"):
            canonicalize({"name": "N", "tracks": [track(1), track(1)]}, "apple", links)

    def test_apple_catalog_identity_is_stable(self):
        row = {"id": "i.library", "type": "library-songs", "attributes": {
            "name": "Song", "artistName": "Artist", "durationInMillis": 1234,
            "playParams": {"catalogId": "123"}}}
        self.assertEqual(apple_track(row)["id"], "123")

    def test_unavailable_spotify_item_fails_closed(self):
        for item in [None, {"type": "episode"}, {"type": "track", "id": "local", "is_local": True}]:
            with self.assertRaises(SyncError):
                spotify_track(item)

    def test_same_recording_prefers_isrc_then_loose_metadata(self):
        self.assertTrue(same_recording(track(1), track(1, id="1001")))
        self.assertFalse(same_recording(track(1), track(1, isrc="OTHER")))  # ISRCs disagree
        self.assertTrue(same_recording(track(1), track(1, **LIBRARY_COPY)))  # no ISRC, rating differs
        self.assertFalse(same_recording(track(1), track(1, isrc=None, title="Song 1 (Live)")))
        self.assertFalse(same_recording(track(1), track(1, isrc=None, duration_ms=190000)))

    def test_relink_follows_a_song_whose_old_id_vanished(self):
        links = {"a": {"apple": track(1)}, "b": {"apple": track(2)}}
        relink([track(1), track(2, id="2002")], "apple", links, ["a", "b"])
        self.assertEqual(links["b"]["apple"]["id"], "2002")

    def test_relink_leaves_genuine_second_editions_alone(self):
        links = {"b": {"apple": track(2)}}
        relink([track(2), track(2, id="2002")], "apple", links, ["b"])  # old ID still present
        self.assertEqual(links["b"]["apple"]["id"], "2")

    def test_relink_ignores_songs_not_expected_and_ambiguous_matches(self):
        links = {"b": {"apple": track(2)}}
        relink([track(2, id="2002")], "apple", links, [])  # e.g. a song deleted long ago
        self.assertEqual(links["b"]["apple"]["id"], "2")
        twins = {"b": {"apple": track(2)}, "c": {"apple": track(2, id="2b")}}
        relink([track(2, id="2002")], "apple", twins, ["b", "c"])
        self.assertEqual([twins[k]["apple"]["id"] for k in "bc"], ["2", "2b"])

    def test_same_songs_allows_re_identified_ids_but_not_extras(self):
        self.assertTrue(same_songs([track(1, id="1001"), track(2)], [track(1), track(2)]))
        self.assertFalse(same_songs([track(1), track(2), track(3)], [track(1), track(2)]))
        self.assertFalse(same_songs([track(1)], [track(1), track(2)]))

    def test_pagination_incomplete_is_not_empty(self):
        with self.assertRaises(SyncError):
            pages("first", lambda _: {})

    def test_pagination_follows_all_pages(self):
        responses = {"a": {"data": [1], "next": "b"}, "b": {"data": [2]}}
        self.assertEqual(pages("a", responses.__getitem__), [1, 2])

    def test_pagination_loop_is_rejected(self):
        with self.assertRaises(SyncError):
            pages("a", lambda _: {"data": [], "next": "a"})


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.config = {"name": "Together", "spotify_playlist": "s", "apple_playlist": "a"}
        self.providers = {"spotify": FakeProvider("spotify", [1]), "apple": FakeProvider("apple", [2])}

    def run_sync(self, preview=False):
        return sync(self.config, self.store, self.providers, preview)

    def test_preview_never_writes(self):
        result = self.run_sync(preview=True)
        self.assertEqual(result["songs"], 2)
        self.assertIsNone(self.store.read("state"))
        self.assertIsNone(self.store.read("pending"))
        self.assertEqual(sum(p.writes for p in self.providers.values()), 0)

    def test_two_way_sync_and_second_run_noops(self):
        self.run_sync()
        self.assertEqual([len(p.value["tracks"]) for p in self.providers.values()], [2, 2])
        self.run_sync()
        self.assertEqual(sum(p.writes for p in self.providers.values()), 2)

    def test_deletion_does_not_resurrect_song(self):
        self.run_sync()
        apple = self.providers["apple"]
        apple.value["tracks"] = [t for t in apple.value["tracks"] if t["id"] != "1"]
        self.run_sync()
        self.assertEqual([t["id"] for t in self.providers["spotify"].value["tracks"]], ["s2"])
        self.run_sync()
        self.assertEqual([t["id"] for t in apple.value["tracks"]], ["2"])

    def test_response_loss_resumes_without_duplicate_writes(self):
        self.providers["apple"].fail_after_write = True
        with self.assertRaises(SyncError):
            self.run_sync()
        self.assertIsNone(self.store.read("state"))
        self.assertIsNotNone(self.store.read("pending"))
        self.run_sync()
        self.assertIsNotNone(self.store.read("state"))
        self.assertIsNone(self.store.read("pending"))
        self.assertEqual(self.providers["apple"].writes, 1)

    def test_song_added_during_pending_sync_is_kept_and_synced_next_run(self):
        apple = self.providers["apple"]
        apple.fail_after_write = True
        with self.assertRaises(SyncError):
            self.run_sync()
        apple.value["tracks"].append(track(3))  # added while the plan was pending
        self.run_sync()  # finishes the saved plan and leaves the new song alone
        self.assertIn("3", self.ids("apple"))
        self.assertNotIn("s3", self.ids("spotify"))
        self.assertIsNone(self.store.read("pending"))
        self.run_sync()  # the next sync copies it across
        self.assertIn("s3", self.ids("spotify"))

    def test_songs_apple_re_identifies_are_followed(self):
        spotify = FakeProvider("spotify", [1, 2, 3])
        apple = ReidentifyingApple("apple", [], swaps={"1": EDITION, "3": dict(LIBRARY_COPY, id="i.3")})
        self.providers = {"spotify": spotify, "apple": apple}
        self.run_sync()
        links = self.store.read("state")["links"]
        self.assertEqual(sorted(l["apple"]["id"] for l in links.values()), ["1001", "2", "i.3"])
        writes = spotify.writes + apple.writes
        self.run_sync()
        self.assertEqual(spotify.writes + apple.writes, writes)
        # Deleting a re-identified song still deletes it on the other side.
        apple.value["tracks"] = [t for t in apple.value["tracks"] if t["id"] != "i.3"]
        self.run_sync()
        self.assertEqual(sorted(self.ids("spotify")), ["s1", "s2"])

    def test_first_sync_of_her_playlist_recovers_like_it_happened_for_real(self):
        # The real first sync: songs were added to Apple, the confirmation timed
        # out, Apple re-identified some of them, and she added a song meanwhile.
        spotify = FakeProvider("spotify", [1, 2, 3])
        apple = ReidentifyingApple("apple", [], swaps={"1": EDITION, "3": dict(LIBRARY_COPY, id="i.3")})
        apple.fail_after_write = True
        self.providers = {"spotify": spotify, "apple": apple}
        with self.assertRaises(SyncError):
            self.run_sync()
        spotify.value["tracks"].append(track(4, "spotify"))
        self.run_sync()
        self.assertEqual(apple.writes, 1)  # nothing added twice
        self.assertEqual(sorted(self.ids("apple")), ["1001", "2", "i.3"])
        self.assertIn("s4", self.ids("spotify"))
        self.run_sync()
        self.assertEqual(sorted(self.ids("apple")), ["1001", "2", "4", "i.3"])

    def test_read_failure_performs_no_writes(self):
        self.providers["apple"].error = "API unavailable"
        with self.assertRaises(SyncError):
            self.run_sync()
        self.assertEqual(sum(p.writes for p in self.providers.values()), 0)

    def test_preflight_failure_performs_no_writes(self):
        self.providers["apple"].preflight_error = "Cloud has not caught up"
        with self.assertRaises(SyncError):
            self.run_sync()
        self.assertEqual(sum(p.writes for p in self.providers.values()), 0)

    def test_lookup_failure_performs_no_writes(self):
        def fail(_):
            raise SyncError("Network request failed")
        self.providers["apple"].find = fail
        with self.assertRaises(SyncError):
            self.run_sync()
        self.assertEqual(sum(p.writes for p in self.providers.values()), 0)

    def ids(self, side):
        return [t["id"] for t in self.providers[side].value["tracks"]]

    def test_unmatched_song_is_skipped_and_the_rest_syncs(self):
        spotify, apple = self.providers["spotify"], self.providers["apple"]
        spotify.value["tracks"].append(track(9, "spotify"))
        apple.missing.add(9)
        result = self.run_sync()
        self.assertEqual(result["spotify"]["not_synced"], ["Song 9 — Artist"])
        self.assertEqual(sorted(self.ids("apple")), ["1", "2"])
        self.assertEqual(sorted(self.ids("spotify")), ["s1", "s2", "s9"])  # kept, not deleted
        self.assertEqual([t["id"] for t in self.store.read("state")["skipped"]["spotify"]], ["s9"])
        writes = spotify.writes + apple.writes
        self.run_sync()
        self.assertEqual(spotify.writes + apple.writes, writes)

    def test_skipped_song_syncs_once_available(self):
        self.providers["spotify"].value["tracks"].append(track(9, "spotify"))
        self.providers["apple"].missing.add(9)
        self.run_sync()
        self.providers["apple"].missing.clear()
        self.run_sync()
        self.assertIn("9", self.ids("apple"))
        self.assertEqual(self.store.read("state")["skipped"]["spotify"], [])

    def test_deleting_skipped_song_changes_nothing_else(self):
        spotify, apple = self.providers["spotify"], self.providers["apple"]
        spotify.value["tracks"].append(track(9, "spotify"))
        apple.missing.add(9)
        self.run_sync()
        writes = spotify.writes + apple.writes
        spotify.value["tracks"] = [t for t in spotify.value["tracks"] if t["id"] != "s9"]
        self.run_sync()
        self.assertEqual(spotify.writes + apple.writes, writes)
        self.assertEqual(sorted(self.ids("apple")), ["1", "2"])

    def test_skipped_song_survives_other_edits(self):
        spotify, apple = self.providers["spotify"], self.providers["apple"]
        spotify.value["tracks"].append(track(9, "spotify"))
        apple.missing.add(9)
        self.run_sync()
        apple.value["tracks"] = [t for t in apple.value["tracks"] if t["id"] != "1"]
        self.run_sync()
        self.assertEqual(sorted(self.ids("spotify")), ["s2", "s9"])

    def test_state_commit_then_journal_cleanup_crash(self):
        original = self.store.remove
        def crash(_):
            raise OSError("simulated crash")
        self.store.remove = crash
        with self.assertRaises(OSError):
            self.run_sync()
        self.store.remove = original
        self.run_sync()
        self.assertEqual(sum(p.writes for p in self.providers.values()), 2)
        self.assertIsNone(self.store.read("pending"))

    def test_state_files_are_private(self):
        self.store.write("secrets", {"test": True})
        self.assertEqual((self.store.root / "secrets.json").stat().st_mode & 0o777, 0o600)

    def test_config_change_during_pending_sync_is_rejected(self):
        self.providers["apple"].fail_after_write = True
        with self.assertRaises(SyncError):
            self.run_sync()
        self.config["spotify_playlist"] = "different"
        with self.assertRaisesRegex(SyncError, "configuration changed"):
            self.run_sync()


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.store.write("config", {"timezone": "UTC"})
        self.start = datetime(2026, 9, 28, 0, 5, tzinfo=timezone.utc)
        patcher = patch("duet.alerts.notify")
        self.notify = patcher.start()
        self.addCleanup(patcher.stop)

    def fail_at(self, hours, notify_user=True):
        alerts.failed(self.store, "Boom", notify_user, self.start + timedelta(hours=hours))

    def test_nightly_failure_notifies_at_most_once_per_day(self):
        self.fail_at(0)
        self.fail_at(9)  # e.g. a second attempt at login the same day
        self.assertEqual(self.notify.call_count, 1)
        self.assertIn("Boom", self.notify.call_args[0][0])
        self.fail_at(24)
        self.assertEqual(self.notify.call_count, 2)

    def test_manual_and_button_failures_never_notify(self):
        self.fail_at(0, notify_user=False)
        self.notify.assert_not_called()
        self.assertEqual(self.store.read("alert")["last_error"], "Boom")

    def test_recovery_notifies_only_after_an_alert(self):
        self.fail_at(0, notify_user=False)
        alerts.succeeded(self.store, {}, {}, scheduled=True)
        self.notify.assert_not_called()
        self.assertIsNone(self.store.read("alert"))
        self.fail_at(0)
        alerts.succeeded(self.store, {}, {}, scheduled=True)
        self.assertEqual(self.notify.call_args[0][0], "Playlists are syncing again.")

    def test_only_newly_skipped_songs_are_announced(self):
        song = track(9, "spotify")
        alerts.succeeded(self.store, {}, {"spotify": [song]}, scheduled=True)
        self.assertIn("couldn't be found on Apple Music", self.notify.call_args[0][0])
        alerts.succeeded(self.store, {"spotify": [song]}, {"spotify": [song]}, scheduled=True)
        self.assertEqual(self.notify.call_count, 1)
        self.assertIn("2 new songs", alerts.skipped_message({}, {"apple": [track(1), track(2)]}))

if __name__ == "__main__":
    unittest.main()
