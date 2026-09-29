from datetime import datetime, timedelta, timezone
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from duet import jobs
from duet.core import SyncError
from duet.engine import summarize
from duet.storage import Store
from test_sync import FakeProvider


class JobTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "home")
        for target in ("duet.jobs.time.sleep", "duet.alerts.notify"):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)


class RetryTests(JobTestCase):
    def test_nightly_retries_then_succeeds_quietly(self):
        result = {"name": "N", "songs": 0, **{s: {"add": [], "remove": [], "rename": None, "not_synced": []}
                                              for s in ("apple", "spotify")}}
        with patch("duet.jobs.sync_once", side_effect=[SyncError("iCloud lag"), SyncError("iCloud lag"), result]), \
                patch("duet.jobs.alerts.failed") as failed:
            self.assertEqual(jobs.run_sync(self.store, "sync", "nightly"), result)
        self.assertEqual([c.args[0] for c in jobs.time.sleep.call_args_list], list(jobs.RETRY_DELAYS[:2]))
        failed.assert_not_called()

    def test_nightly_gives_up_after_retries_and_notifies(self):
        with patch("duet.jobs.sync_once", side_effect=SyncError("down")), \
                patch("duet.jobs.alerts.failed") as failed:
            with self.assertRaises(SyncError):
                jobs.run_sync(self.store, "sync", "nightly")
        self.assertEqual(jobs.time.sleep.call_count, len(jobs.RETRY_DELAYS))
        failed.assert_called_once_with(self.store, "down", notify_user=True)

    def test_unexpected_errors_are_not_retried(self):
        with patch("duet.jobs.sync_once", side_effect=TypeError("bug")), patch("duet.jobs.alerts.failed"):
            with self.assertRaises(TypeError):
                jobs.run_sync(self.store, "sync", "nightly")
        jobs.time.sleep.assert_not_called()

    def test_button_failure_is_reported_without_retry_or_notification(self):
        with patch("duet.jobs.sync_once", side_effect=SyncError("down")), \
                patch("duet.jobs.alerts.failed") as failed:
            with self.assertRaises(SyncError):
                jobs.run_sync(self.store, "sync", "button")
        jobs.time.sleep.assert_not_called()
        failed.assert_called_once_with(self.store, "down", notify_user=False)
        self.assertEqual(self.store.read("trigger")["message"], "Didn't sync: down")
        self.assertFalse(self.store.read("trigger")["ok"])

    def test_preview_failure_is_not_recorded(self):
        with patch("duet.jobs.sync_once", side_effect=SyncError("down")), \
                patch("duet.jobs.alerts.failed") as failed:
            with self.assertRaises(SyncError):
                jobs.run_sync(self.store, "preview", "manual")
        failed.assert_not_called()


class EndToEndTests(JobTestCase):
    def setUp(self):
        super().setUp()
        self.store.write("config", {"name": "Together", "timezone": "UTC", "spotify_playlist": "s",
                                    "apple_playlist": "a", "apple_local_id": "L", "storefront": "us"})
        self.fakes = {"spotify": FakeProvider("spotify", [1]), "apple": FakeProvider("apple", [2])}
        for side, cls in (("spotify", "Spotify"), ("apple", "Apple")):
            patcher = patch("duet.jobs." + cls, return_value=self.fakes[side])
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_button_sync_reports_a_readable_summary(self):
        jobs.run_sync(self.store, "sync", "button")
        outcome = self.store.read("trigger")
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["message"],
                         "Synced: 2 songs.\nAdded to Apple Music: Song 1\nAdded to Spotify: Song 2")

    def test_nightly_skips_once_synced_today(self):
        jobs.run_sync(self.store, "sync", "button")
        writes = sum(p.writes for p in self.fakes.values())
        self.assertIsNone(jobs.run_sync(self.store, "sync", "nightly"))
        self.assertEqual(sum(p.writes for p in self.fakes.values()), writes)

    def test_synced_today_uses_configured_timezone(self):
        config = {"timezone": "UTC"}
        self.store.write("state", {"last_success": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()})
        self.assertFalse(jobs.synced_today(self.store, config))
        self.store.write("state", {"last_success": datetime.now(timezone.utc).isoformat()})
        self.assertTrue(jobs.synced_today(self.store, config))


class TriggerTests(JobTestCase):
    def kickstart(self, returncode=0, finished_offset=1):
        def run(command, **kwargs):
            if returncode == 0:
                # Simulates the on-demand job finishing and writing its result.
                self.store.write("trigger", {"finished_at": jobs.time.time() + finished_offset,
                                             "ok": True, "message": "Synced: 3 songs."})
            return subprocess.CompletedProcess(command, returncode, "", "error")
        return patch("duet.jobs.subprocess.run", side_effect=run)

    def test_trigger_starts_the_on_demand_job_and_returns_its_result(self):
        with self.kickstart() as run:
            self.assertEqual(jobs.trigger(self.store, poll=0)["message"], "Synced: 3 songs.")
        command = run.call_args[0][0]
        self.assertEqual(command[:2], ["/bin/launchctl", "kickstart"])
        self.assertTrue(command[2].endswith("/" + jobs.ON_DEMAND))

    def test_trigger_ignores_an_older_result(self):
        with self.kickstart(finished_offset=-60):
            with self.assertRaisesRegex(SyncError, "Still syncing"):
                jobs.trigger(self.store, timeout=0.05, poll=0.01)

    def test_trigger_explains_when_the_job_is_not_installed(self):
        with self.kickstart(returncode=113):
            with self.assertRaisesRegex(SyncError, "logged in"):
                jobs.trigger(self.store, poll=0)

    def test_waiting_lock_times_out_with_a_clear_message(self):
        with patch("duet.storage.time.sleep"):
            with self.store.lock():
                with self.assertRaisesRegex(SyncError, "already running"):
                    with self.store.lock(wait=0):
                        pass


class SSHKeyTests(JobTestCase):
    KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample iPhone"

    def test_key_is_restricted_to_trigger(self):
        line = jobs.ssh_key_line(self.store, self.KEY)
        self.assertTrue(line.startswith('restrict,command="cd '))
        self.assertTrue(line.endswith(self.KEY))
        self.assertIn(" trigger\" ", line)

    def test_quotes_in_paths_cannot_break_out_of_the_command(self):
        store = Store(Path(self.temp.name) / 'we"ird home')
        line = jobs.ssh_key_line(store, self.KEY)
        option = line[len('restrict,command="'):line.rindex('" ssh-ed25519')]
        self.assertNotIn('"', option.replace('\\"', ""))

    def test_rejects_things_that_are_not_keys(self):
        for value in ("", "hello", "-----BEGIN OPENSSH PRIVATE KEY-----"):
            with self.assertRaises(SyncError):
                jobs.ssh_key_line(self.store, value)


class InstallTests(JobTestCase):
    def setUp(self):
        super().setUp()
        agents = Path(self.temp.name) / "LaunchAgents"
        for target, value in (("duet.jobs.AGENTS", agents),
                              ("duet.jobs.subprocess.run",
                               lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", ""))):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.agents = agents

    def job(self, label):
        with (self.agents / (label + ".plist")).open("rb") as f:
            return plistlib.load(f)

    def test_requires_a_successful_manual_sync_first(self):
        with self.assertRaises(SyncError):
            jobs.install(self.store)

    def test_nightly_job_runs_only_at_midnight_and_login(self):
        self.store.write("state", {"last_success": "2026-09-28T00:00:00+00:00"})
        jobs.install(self.store)
        nightly = self.job(jobs.NIGHTLY)
        self.assertEqual(nightly["StartCalendarInterval"], {"Hour": 0, "Minute": 0})
        self.assertTrue(nightly["RunAtLoad"])
        self.assertNotIn("StartInterval", nightly)
        self.assertEqual(nightly["ProgramArguments"][-2:], ["sync", "--due"])

    def test_on_demand_job_is_never_scheduled(self):
        self.store.write("state", {"last_success": "2026-09-28T00:00:00+00:00"})
        jobs.install(self.store)
        button = self.job(jobs.ON_DEMAND)
        for key in ("RunAtLoad", "StartCalendarInterval", "StartInterval", "KeepAlive"):
            self.assertNotIn(key, button)
        self.assertEqual(button["ProgramArguments"][-2:], ["sync", "--on-demand"])

    def test_uninstall_removes_both_jobs(self):
        self.store.write("state", {"last_success": "2026-09-28T00:00:00+00:00"})
        jobs.install(self.store)
        jobs.uninstall()
        self.assertEqual(list(self.agents.iterdir()), [])


class SummaryTests(unittest.TestCase):
    def result(self, **sides):
        empty = {"add": [], "remove": [], "rename": None, "not_synced": []}
        return {"name": "Road trip", "songs": 3,
                **{side: dict(empty, **sides.get(side, {})) for side in ("apple", "spotify")}}

    def test_no_changes(self):
        self.assertEqual(summarize(self.result()), "Already in sync: 3 songs.")

    def test_changes_rename_and_skipped(self):
        text = summarize(self.result(apple={"add": ["A", "B"], "rename": "Road trip"},
                                     spotify={"remove": ["C"], "not_synced": ["D — X"]}))
        self.assertEqual(text.splitlines(), ["Synced: 3 songs.", "Added to Apple Music: A, B",
                                             "Removed from Spotify: C", 'Renamed to "Road trip"',
                                             "1 song only on one service (run duet status)"])

    def test_only_skipped_is_still_in_sync(self):
        text = summarize(self.result(spotify={"not_synced": ["D — X"]}))
        self.assertTrue(text.startswith("Already in sync"))


if __name__ == "__main__":
    unittest.main()
