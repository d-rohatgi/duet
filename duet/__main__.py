import argparse
from datetime import datetime
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

from . import alerts
from .auth import connect
from .core import SyncError
from .engine import sync
from .providers import Apple, Spotify, music_bridge, pages
from .storage import Store, DEFAULT_HOME

LABEL = "local.duet.playlist-sync"


def parser():
    p = argparse.ArgumentParser(description="Duet: a shared Spotify / Apple Music playlist on your Mac.")
    p.add_argument("--home", type=Path, default=DEFAULT_HOME, help="configuration/state folder")
    commands = p.add_subparsers(dest="command", required=True)
    configure = commands.add_parser("configure", help="save app credentials (not account passwords)")
    configure.add_argument("--spotify-client-id", required=True)
    configure.add_argument("--apple-team-id", required=True)
    configure.add_argument("--apple-key-id", required=True)
    configure.add_argument("--apple-key", required=True, type=Path)
    configure.add_argument("--name", default="Both of Us")
    configure.add_argument("--timezone", default="America/New_York")
    commands.add_parser("connect-spotify", help="one-time browser login for the Spotify account")
    commands.add_parser("connect-apple", help="one-time browser login for the Apple Music account")
    commands.add_parser("create", help="create a dedicated playlist on each service")
    commands.add_parser("preview", help="read-only preview of the next sync")
    run = commands.add_parser("sync", help="reconcile the playlist pair")
    run.add_argument("--due", action="store_true", help="run only if no successful sync today, or a sync is pending")
    commands.add_parser("status", help="show local configuration and last sync (no tokens)")
    commands.add_parser("install", help="schedule daily sync; catches up when awake")
    commands.add_parser("uninstall", help="remove the scheduled job; keep playlists and state")
    commands.add_parser("demo", help="run a credential-free sync example in memory")
    return p


def setup_playlists(config, store):
    spotify, apple = Spotify(config, store), Apple(config, store)
    name = config["name"]
    # Store each created playlist ID immediately. A rerun resumes setup.
    # When creation timed out, look for its unique setup marker before retrying.
    marker = "Duet pair: " + config["pair_id"]
    if not config.get("storefront"):
        config["storefront"] = apple.api("me/storefront")["data"][0]["id"]
        store.write("config", config)
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
        local = music_bridge({"action": "snapshot", "name": name, "marker": marker})
        config["apple_local_id"] = local["id"]
        store.write("config", config)
    # Verify that the local and remote copies really agree before linking.
    Apple(config, store).preflight(Apple(config, store).snapshot())
    print("Playlist pair ready: " + name)
    print("Add songs in either app, then run: python3 -m duet preview")


def schedule(store, install):
    import os
    path = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
    domain = "gui/%d" % os.getuid()
    if not install:
        subprocess.run(["/bin/launchctl", "bootout", domain + "/" + LABEL], capture_output=True)
        path.unlink(missing_ok=True)
        print("Daily sync removed. Your playlists and local state are unchanged.")
        return
    if not store.read("state", {}).get("last_success"):
        raise SyncError("Complete one successful manual sync before installing the schedule.")
    root = Path(__file__).resolve().parent.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    job = {"Label": LABEL, "ProgramArguments": [sys.executable, "-m", "duet", "--home", str(store.root), "sync", "--due"],
           "WorkingDirectory": str(root), "RunAtLoad": True,
           "StartCalendarInterval": {"Hour": 0, "Minute": 0},
           "StartInterval": 900, "ProcessType": "Background",
           "StandardOutPath": str(store.root / "sync.log"),
           "StandardErrorPath": str(store.root / "error.log")}
    with path.open("wb") as f:
        plistlib.dump(job, f)
    subprocess.run(["/bin/launchctl", "bootout", domain + "/" + LABEL], capture_output=True)
    result = subprocess.run(["/bin/launchctl", "bootstrap", domain, str(path)], capture_output=True, text=True)
    if result.returncode:
        raise SyncError("Could not load the scheduled sync: " + result.stderr.strip())
    print("Daily sync installed. Midnight when awake; otherwise catches up after waking/logging in.")


def demo():
    from .core import merge
    baseline = {"name": "Both of Us", "keys": ["Song A", "Song B"]}
    current = {"spotify": {"name": "Both of Us", "keys": ["Song A", "Song B", "Her new song"]},
               "apple": {"name": "Our road trip", "keys": ["Song B", "My new song"]}}
    print(json.dumps({"last_sync": baseline, "today": current, "after_sync": merge(baseline, current, "Both of Us")}, indent=2))


def main():
    args = parser().parse_args()
    if args.command == "demo":
        demo()
        return
    store = Store(args.home)
    with store.lock():
        config = store.read("config", {})
        if args.command == "configure":
            import uuid
            if config.get("spotify_playlist") or config.get("apple_playlist"):
                raise SyncError("This pair is already configured. Edit config.json to rotate app credentials; keep playlist IDs.")
            if not args.apple_key.expanduser().is_file():
                raise SyncError("The Apple Music .p8 key file does not exist.")
            ZoneInfo(args.timezone)
            config = {"spotify_client_id": args.spotify_client_id, "apple_team_id": args.apple_team_id,
                      "apple_key_id": args.apple_key_id, "apple_key_path": str(args.apple_key.expanduser().resolve()),
                      "name": args.name, "timezone": args.timezone, "rename_priority": "apple",
                      "pair_id": config.get("pair_id", str(uuid.uuid4()))}
            store.write("config", config)
            print("Configuration saved. Next: python3 -m duet connect-spotify")
        elif args.command == "status":
            state = store.read("state", {})
            print(json.dumps({"name": config.get("name"), "timezone": config.get("timezone"),
                              "spotify_playlist": config.get("spotify_playlist"), "apple_playlist": config.get("apple_playlist"),
                              "last_success": state.get("last_success"),
                              "last_error": store.read("alert", {}).get("last_error"),
                              "not_synced": {side: ["%s — %s" % (t["title"], t["artist"]) for t in tracks]
                                             for side, tracks in state.get("skipped", {}).items()},
                              "pending": bool(store.read("pending")), "state_folder": str(store.root)}, indent=2))
        elif args.command == "uninstall":
            schedule(store, False)
        elif not config:
            raise SyncError("Run configure first. See README.md for the one-time developer app setup.")
        elif args.command.startswith("connect-"):
            connect(args.command.removeprefix("connect-"), config, store)
            print("Connection saved locally.")
        elif args.command == "create":
            setup_playlists(config, store)
        elif args.command == "install":
            schedule(store, True)
        else:
            if not all(config.get(k) for k in ("spotify_playlist", "apple_playlist", "apple_local_id", "storefront")):
                raise SyncError("Finish creating the playlist pair: python3 -m duet create")
            if args.command == "sync" and args.due and not store.read("pending"):
                last = store.read("state", {}).get("last_success")
                zone = ZoneInfo(config["timezone"])
                if last and datetime.fromisoformat(last).astimezone(zone).date() == datetime.now(zone).date():
                    return
            providers = {"spotify": Spotify(config, store), "apple": Apple(config, store)}
            skipped_before = store.read("state", {}).get("skipped", {})
            try:
                output = sync(config, store, providers, preview=args.command == "preview")
            except Exception as error:
                if args.command == "sync":
                    alerts.failed(store, config, str(error), scheduled=args.due)
                raise
            if args.command == "sync":
                alerts.succeeded(store, skipped_before, store.read("state", {}).get("skipped", {}), args.due)
            print(json.dumps(output, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (SyncError, ValueError, KeyError, OSError, subprocess.TimeoutExpired) as error:
        print("Duet: " + str(error), file=sys.stderr)
        sys.exit(1)
