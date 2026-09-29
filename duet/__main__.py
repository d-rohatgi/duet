import argparse
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from . import jobs
from .auth import connect
from .core import SyncError
from .providers import Apple, Spotify, music_bridge, pages
from .storage import Store, DEFAULT_HOME


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
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--due", action="store_true",
                      help="nightly job: skip if already synced today, retry for ~20 minutes, notify on failure")
    mode.add_argument("--on-demand", action="store_true", help=argparse.SUPPRESS)
    commands.add_parser("status", help="show local configuration and last sync (no tokens)")
    commands.add_parser("install", help="schedule the nightly sync and enable the phone button")
    commands.add_parser("uninstall", help="remove the scheduled jobs; keep playlists and state")
    commands.add_parser("trigger", help="sync now through the background job and print a summary (phone button)")
    key = commands.add_parser("ssh-key-line", help="print an authorized_keys line limiting a phone's key to trigger")
    key.add_argument("public_key", help="the phone's SSH public key, in quotes")
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
    if args.command in ("sync", "preview"):
        mode = "nightly" if getattr(args, "due", False) else "button" if getattr(args, "on_demand", False) else "manual"
        result = jobs.run_sync(store, args.command, mode)
        if mode == "manual":
            print(json.dumps(result, indent=2))
        return
    if args.command == "trigger":
        # Always print one readable line and exit 0, so a Shortcut can show it.
        try:
            print(jobs.trigger(store)["message"])
        except SyncError as error:
            print("Didn't sync: %s" % error)
        return
    if args.command == "ssh-key-line":
        print(jobs.ssh_key_line(store, args.public_key))
        return
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
                              "last_button_sync": (store.read("trigger") or {}).get("message"),
                              "not_synced": {side: ["%s — %s" % (t["title"], t["artist"]) for t in tracks]
                                             for side, tracks in state.get("skipped", {}).items()},
                              "pending": bool(store.read("pending")), "state_folder": str(store.root)}, indent=2))
        elif args.command == "uninstall":
            jobs.uninstall()
            print("Nightly sync and phone button removed. Your playlists and local state are unchanged.")
        elif not config:
            raise SyncError("Run configure first. See README.md for the one-time developer app setup.")
        elif args.command.startswith("connect-"):
            connect(args.command.removeprefix("connect-"), config, store)
            print("Connection saved locally.")
        elif args.command == "create":
            setup_playlists(config, store)
        elif args.command == "install":
            jobs.install(store)
            print("Nightly sync installed: midnight, or when the Mac next wakes or logs in.")
            print("Phone button ready: python3 -m duet trigger")


if __name__ == "__main__":
    try:
        main()
    except jobs.EXPECTED as error:
        print("Duet: " + str(error), file=sys.stderr)
        sys.exit(1)
