"""Mac notifications for the nightly job: failures and skipped songs."""
from datetime import datetime, timezone
import subprocess
from zoneinfo import ZoneInfo

SERVICE = {"spotify": "Spotify", "apple": "Apple Music"}


def notify(message, title="Duet"):
    # Text is passed as arguments, never interpolated into AppleScript source.
    try:
        subprocess.run(["/usr/bin/osascript", "-e", "on run argv",
                        "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
                        "-e", "end run", title, message], capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        pass  # A missed notification must never affect syncing.


def failed(store, message, notify_user, now=None):
    """Record a failed sync. The nightly job calls this only after its retries."""
    now = now or datetime.now(timezone.utc)
    alert = store.read("alert", {})
    alert.setdefault("failing_since", now.isoformat())
    alert.update(last_failure=now.isoformat(), last_error=message)
    zone = store.read("config", {}).get("timezone")
    today = now.astimezone(ZoneInfo(zone) if zone else None).date().isoformat()
    if notify_user and alert.get("notified_on") != today:
        notify("Playlists didn't sync. " + message)
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
