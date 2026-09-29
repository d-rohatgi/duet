"""Background jobs: the nightly LaunchAgent, its retries, and the phone button."""
from datetime import datetime
import os
from pathlib import Path
import plistlib
import shlex
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

from . import alerts
from .core import SyncError
from .engine import summarize, sync
from .providers import Apple, Spotify

NIGHTLY = "local.duet.playlist-sync"
ON_DEMAND = "local.duet.sync-now"
AGENTS = Path.home() / "Library/LaunchAgents"
ROOT = Path(__file__).resolve().parent.parent
EXPECTED = (SyncError, ValueError, KeyError, OSError, subprocess.TimeoutExpired)
RETRY_DELAYS = (60, 180, 360, 600)  # the nightly run keeps trying for about 20 minutes
LOCK_WAIT = 120                     # a button press waits this long for a running sync
TRIGGER_TIMEOUT = 240


def log(message):
    print(datetime.now().isoformat(timespec="seconds"), message, flush=True)


def synced_today(store, config):
    last = store.read("state", {}).get("last_success")
    zone = ZoneInfo(config["timezone"])
    return bool(last) and datetime.fromisoformat(last).astimezone(zone).date() == datetime.now(zone).date()


def sync_once(store, command, mode):
    # Hold the lock only while syncing, so a nightly retry that is waiting
    # between attempts never blocks the phone button.
    with store.lock(wait=LOCK_WAIT if mode == "button" else 0):
        config = store.read("config", {})
        if not config:
            raise SyncError("Run configure first. See README.md for the one-time developer app setup.")
        if not all(config.get(k) for k in ("spotify_playlist", "apple_playlist", "apple_local_id", "storefront")):
            raise SyncError("Finish creating the playlist pair: python3 -m duet create")
        if mode == "nightly" and not store.read("pending") and synced_today(store, config):
            return None
        providers = {"spotify": Spotify(config, store), "apple": Apple(config, store)}
        skipped_before = store.read("state", {}).get("skipped", {})
        result = sync(config, store, providers, preview=command == "preview")
        if command == "sync":
            alerts.succeeded(store, skipped_before, store.read("state", {}).get("skipped", {}),
                             scheduled=mode == "nightly")
        return result


def run_sync(store, command, mode):
    """mode is 'manual' (Terminal), 'nightly' (schedule), or 'button' (phone)."""
    delays = list(RETRY_DELAYS) if mode == "nightly" else []
    while True:
        try:
            result = sync_once(store, command, mode)
            break
        except Exception as error:
            if delays and isinstance(error, EXPECTED):
                log("Sync failed; retrying in %d min: %s" % (delays[0] // 60, error))
                time.sleep(delays.pop(0))
                continue
            if command == "sync":
                alerts.failed(store, str(error), notify_user=mode == "nightly")
            if mode == "button":
                report(store, False, "Didn't sync: %s" % error)
            raise
    if result is not None and mode != "manual":
        log(summarize(result))
        if mode == "button":
            report(store, True, summarize(result))
    return result


def report(store, ok, message):
    store.write("trigger", {"finished_at": time.time(), "ok": ok, "message": message})


def trigger(store, label=ON_DEMAND, timeout=TRIGGER_TIMEOUT, poll=1):
    """Run the on-demand job in the logged-in session and wait for its result.

    SSH sessions can't get macOS permission to control the Music app, but the
    LaunchAgent runs inside the user's session, where that permission works.
    """
    requested = time.time()
    started = subprocess.run(["/bin/launchctl", "kickstart", domain() + "/" + label],
                             capture_output=True, text=True)
    if started.returncode:
        raise SyncError("Couldn't start a sync. Make sure the Mac is logged in and "
                        "`python3 -m duet install` has been run.")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(poll)
        outcome = store.read("trigger") or {}
        if outcome.get("finished_at", 0) >= requested:
            return outcome
    raise SyncError("Still syncing. Check again in a few minutes.")


def ssh_key_line(store, public_key):
    """An authorized_keys line that lets a phone key run the sync and nothing else."""
    parts = public_key.split()
    if len(parts) < 2 or not parts[0].startswith(("ssh-", "ecdsa-", "sk-")):
        raise SyncError("That doesn't look like an SSH public key. Copy it from the Shortcut's "
                        "Run Script Over SSH action.")
    run = "cd %s && exec %s" % (shlex.quote(str(ROOT)), shlex.join(command_line(store, "trigger")))
    return 'restrict,command="%s" %s' % (run.replace("\\", "\\\\").replace('"', '\\"'), " ".join(parts))


def domain():
    return "gui/%d" % os.getuid()


def command_line(store, *args):
    return [sys.executable, "-m", "duet", "--home", str(store.root), *args]


def install(store):
    if not store.read("state", {}).get("last_success"):
        raise SyncError("Complete one successful manual sync before installing the schedule.")
    common = {"WorkingDirectory": str(ROOT),
              "StandardOutPath": str(store.root / "sync.log"),
              "StandardErrorPath": str(store.root / "error.log")}
    jobs = {
        # Midnight, or on wake if the Mac slept through it. RunAtLoad covers login.
        NIGHTLY: dict(common, ProgramArguments=command_line(store, "sync", "--due"), RunAtLoad=True,
                      StartCalendarInterval={"Hour": 0, "Minute": 0}, ProcessType="Background"),
        # Never scheduled; started only by `duet trigger` (the phone button).
        ON_DEMAND: dict(common, ProgramArguments=command_line(store, "sync", "--on-demand"),
                        ProcessType="Interactive"),
    }
    AGENTS.mkdir(parents=True, exist_ok=True)
    for label, job in jobs.items():
        path = AGENTS / (label + ".plist")
        with path.open("wb") as f:
            plistlib.dump(dict(job, Label=label), f)
        subprocess.run(["/bin/launchctl", "bootout", domain() + "/" + label], capture_output=True)
        loaded = subprocess.run(["/bin/launchctl", "bootstrap", domain(), str(path)],
                                capture_output=True, text=True)
        if loaded.returncode:
            raise SyncError("Could not load %s: %s" % (label, loaded.stderr.strip()))


def uninstall():
    for label in (NIGHTLY, ON_DEMAND):
        subprocess.run(["/bin/launchctl", "bootout", domain() + "/" + label], capture_output=True)
        (AGENTS / (label + ".plist")).unlink(missing_ok=True)
