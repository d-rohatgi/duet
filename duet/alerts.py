"""Mac notifications for the scheduled job: persistent failures and skipped songs."""
from datetime import datetime, timedelta, timezone
import subprocess
from zoneinfo import ZoneInfo

ALERT_AFTER = timedelta(hours=1)       # a brief outage right after waking stays quiet
NEW_EPISODE = timedelta(minutes=45)    # a longer gap between failures means the Mac slept
STALE = timedelta(days=2)              # alert regardless once nothing has synced this long
SERVICE = {"spotify": "Spotify", "apple": "Apple Music"}


def notify(message, title="Duet"):
    # Text is passed as arguments, never interpolated into AppleScript source.
    try:
        subprocess.run(["/usr/bin/osascript", "-e", "on run argv",
                        "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
                        "-e", "end run", title, message], capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        pass  # A missed notification must never affect syncing.


def failed(store, config, message, scheduled, now=None):
    now = now or datetime.now(timezone.utc)
    alert = store.read("alert", {})
    last = alert.get("last_failure")
    if not last or now - datetime.fromisoformat(last) > NEW_EPISODE:
        alert["failing_since"] = now.isoformat()
    alert.update(last_failure=now.isoformat(), last_error=message)
    success = store.read("state", {}).get("last_success")
    stale = success and now - datetime.fromisoformat(success) > STALE
    persistent = now - datetime.fromisoformat(alert["failing_since"]) >= ALERT_AFTER
    today = now.astimezone(ZoneInfo(config["timezone"])).date().isoformat()
    if scheduled and (persistent or stale) and alert.get("notified_on") != today:
        notify("Playlists haven't synced. " + message)
        alert["notified_on"] = today
    store.write("alert", alert)


def succeeded(store, skipped_before, skipped_after, scheduled):
    alert = store.read("alert")
    if alert is not None:
        if scheduled and alert.get("notified_on"):
            notify("Playlists are syncing again.")
        store.remove("alert")
    message = skipped_message(skipped_before, skipped_after)
    if scheduled and message:
        notify(message)


def skipped_message(before, after):
    new = [(side, track) for side, tracks in after.items() for track in tracks
           if track["id"] not in {t["id"] for t in before.get(side, [])}]
    if len(new) == 1:
        side, track = new[0]
        other = "apple" if side == "spotify" else "spotify"
        return '"%s" by %s couldn\'t be found on %s, so it stays in the %s playlist only.' % (
            track["title"], track["artist"], SERVICE[other], SERVICE[side])
    if new:
        return ("%d new songs couldn't be found on the other service and weren't synced. "
                "Run: python3 -m duet status" % len(new))
    return None
