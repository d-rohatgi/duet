# Duet

Keep one playlist in sync between a Spotify account and an Apple Music account.

Two people, one on each service, edit their own copy of the playlist in the app they already use. Every night, Duet copies additions, removals, and renames to the other side. It runs on a Mac.

> **Status: experimental.** The sync logic has an automated test suite, but Duet has not yet been run end to end against real Spotify and Apple Music accounts. Start with a playlist of throwaway songs (see [step 6](#6-first-run)). Bug reports and fixes are welcome.

## What it does

| Since the last sync… | Duet… |
| --- | --- |
| Someone adds a song | Adds the same recording to the other playlist |
| Someone removes a synced song | Removes it from the other playlist (never from anyone's library) |
| Both people add different songs | Keeps both |
| Someone renames the playlist | Copies the new name to the other playlist |
| Both rename it differently | Uses the Apple Music name (configurable) |
| A song isn't on the other service | Leaves it on its original side only, syncs everything else, and notifies you once |
| A service is down or returns something unexpected | Changes nothing, retries, and notifies you if it keeps failing |

Duet only touches the two playlists it creates. It never edits any other playlist or your libraries.

## What you need

- **A Mac** signed in to the Apple Music account, with Python 3.9 or newer. The `python3` that comes with Xcode Command Line Tools or Homebrew works. Apple's web API can't remove songs from a playlist, so Duet makes removals and renames through the Music app. The Mac doesn't need to be awake at midnight; Duet catches up when it wakes.
- **An [Apple Developer Program](https://developer.apple.com/programs/) membership** (US$99/year) for the Apple Music listener. Apple requires one for any Apple Music API access.
- **Spotify Premium** on the account that creates the Spotify developer app. Spotify [requires it for development-mode apps](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide). Usually this is the Spotify listener's own account.
- **About 30 minutes,** with both people available for the one-time logins.

Duet has no third-party dependencies. It uses the Python standard library and the `osascript` and `openssl` tools built into macOS.

## Setup

### 1. Get the code

```bash
git clone https://github.com/d-rohatgi/duet.git
cd duet
python3 -m unittest discover -s tests
```

Run every later command from this folder.

### 2. Create a Spotify app (Spotify listener)

1. Sign in to the [Spotify developer dashboard](https://developer.spotify.com/dashboard) with the Premium account and click **Create app**.
2. Use any name and description. Set **Redirect URI** to exactly `http://127.0.0.1:8765/callback`, select **Web API**, accept the terms, and save.
3. Open the app's **Settings** and copy the **Client ID**. You don't need the client secret, because Duet uses PKCE.
4. If the Spotify account that will own the playlist didn't create the app, add it under **Settings → User Management**.

### 3. Create an Apple Music key (Apple Music listener)

Apple's official version of these steps: [Create a media identifier and private key](https://developer.apple.com/help/account/capabilities/create-a-media-identifier-and-private-key).

1. Enroll in the [Apple Developer Program](https://developer.apple.com/programs/enroll/).
2. In [Certificates, Identifiers & Profiles](https://developer.apple.com/account/resources), go to **Identifiers → + → Media IDs**.
   - Enter a description such as `Duet`. It appears on Apple's permission screen.
   - Enter an identifier such as `com.yourname.duet`.
   - Enable **MusicKit**, then register.
3. Go to **Keys → +**. Name the key, enable **Media Services**, click **Configure**, and choose the Media ID you just made. Continue, register, then **download** the `.p8` file.
   - The file can only be downloaded once. Keep it outside this folder, for example `~/.keys/AuthKey_ABC123DEFG.p8`.
   - Note the **Key ID**, which is shown on the key's page and in the filename.
4. Find your **Team ID** under **Membership details** in your [developer account](https://developer.apple.com/account).

### 4. Prepare the Music app

On the Mac, open **Music**, sign in with the Apple Music account, and turn on **Settings → General → Sync Library**.

### 5. Configure and connect

```bash
python3 -m duet configure \
  --spotify-client-id YOUR_SPOTIFY_CLIENT_ID \
  --apple-team-id YOUR_TEAM_ID \
  --apple-key-id YOUR_KEY_ID \
  --apple-key ~/.keys/AuthKey_YOUR_KEY_ID.p8 \
  --name "Our Playlist" \
  --timezone America/New_York
```

`--name` is the shared playlist's starting name. `--timezone` is an [IANA time zone name](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones); it decides when a new sync day begins.

Next, connect both accounts and create the playlists. Each `connect` command opens a browser window on the Mac:

```bash
python3 -m duet connect-spotify
python3 -m duet connect-apple
python3 -m duet create
```

- **`connect-spotify`:** the Spotify listener signs in with their Spotify account. They can sign out of the browser afterward, because Duet keeps its own authorization.
- **`connect-apple`:** sign in with the same Apple Music account the Music app uses.
- **`create`:** makes a new, empty playlist on each service. Both are tagged with a marker in the description, so leave the description as it is.
  - If the Apple playlist hasn't appeared in the Music app yet, let iCloud catch up and run `create` again. It picks up where it left off.
  - When macOS asks whether Terminal may control Music, allow it.

### 6. First run

Add a few throwaway songs to each copy of the playlist, then run:

```bash
python3 -m duet preview
python3 -m duet sync
python3 -m duet status
```

`preview` is read-only and shows what would change. After `sync`, check that:

- songs added on each side now appear on the other;
- when you remove a synced song on one side and sync again, it disappears from the other playlist but stays in both libraries;
- a rename carries over.

### 7. Turn on the nightly sync

```bash
python3 -m duet install
```

This installs a macOS LaunchAgent. It syncs at midnight in your configured time zone, and checks every 15 minutes while the Mac is awake, so a missed night catches up after wake or login. After a successful sync, the day's remaining checks do nothing.

The first background run may ask for Automation permission again. The morning after installing, check `~/Library/Application Support/Duet/error.log`.

To stop the schedule and keep everything else:

```bash
python3 -m duet uninstall
```

## Notifications

Scheduled runs post a Mac notification when:

- syncing has failed for over an hour, or nothing has synced for two days (at most once a day, with the error);
- syncing recovers after one of those alerts;
- a newly added song couldn't be found on the other service.

Short outages stay quiet, such as the first few minutes after the Mac wakes without a network connection. macOS shows these notifications as coming from **Script Editor**. If none appear, allow notifications for Script Editor in **System Settings → Notifications**. `python3 -m duet status` also shows the most recent error and any songs that aren't synced.

## How syncing works

- **The previous sync is the reference point.** Each run compares both playlists with the last successful sync. New songs from either side are combined, and a removal on either side wins.
- **Timing within a day isn't tracked.** A song removed and re-added between two runs looks unchanged.
- **Renames:** a rename on one side is copied to the other. If both sides are renamed differently, the Apple Music name wins. To change this, set `"rename_priority": "spotify"` in `config.json`.
- **Song order isn't synced.** Each app keeps its own order, and new songs are added at the end.
- **Matching is strict.** Duet matches songs by ISRC, the standard recording code, when it has one. Otherwise it requires the same title, artist, duration (within 2.5 seconds), and explicit rating. It never substitutes a live, remix, or other edition.
- **Unmatched songs are skipped, not blocking.** If a song has no single clear match on the other service, it stays in its original playlist only and is never copied or deleted. Duet retries it every night, so it syncs automatically if the other service adds it later.
- **Nothing is committed until both services confirm.** Duet saves its plan before the first write, and only records a sync as done once both services show the result. If a run is interrupted, the next run resumes the plan without adding songs twice. If someone edits the playlist while a plan is pending, Duet stops and asks for review instead of overwriting the edit.

## Limitations

- **Some playlist contents stop the sync** until someone removes them: duplicate songs within one playlist, local files in Spotify, podcast episodes, and videos.
- **The Mac is required,** as explained in [Why a Mac](#why-a-mac).
- **Authorizations can expire.** When a notification or `status` reports an authorization error, rerun `connect-spotify` or `connect-apple`.
- **Logs never rotate.** `sync.log` and `error.log` keep growing.

## Files and privacy

Duet stores everything outside this folder, in `~/Library/Application Support/Duet/`. The folder is private and each file is readable only by you (`0600`).

| File | Contents |
| --- | --- |
| `config.json` | App IDs, playlist IDs, time zone, and the path to your `.p8` key |
| `secrets.json` | Spotify and Apple Music authorization tokens |
| `state.json` | The last successful sync and how songs are matched across services |
| `pending.json` | A sync plan that hasn't finished yet |
| `alert.json` | The current failure streak, cleared after a successful sync |
| `sync.log`, `error.log` | Output from scheduled runs; tokens are never logged |

- **Where your data goes:** Duet talks only to Spotify's and Apple's APIs and the Music app on your Mac. There is no server and no telemetry.
- **Your key stays put.** Duet reads the `.p8` key from where you saved it and never copies it. `.gitignore` excludes key and state files, but keep your key outside the repository anyway.
- **Tokens are stored in plain files,** protected by file permissions, not in the macOS Keychain.

**To remove Duet completely:**

1. Run `python3 -m duet uninstall`.
2. Delete `~/Library/Application Support/Duet/`.
3. Delete the two playlists, if you don't want them.
4. Remove the app's access at [spotify.com/account/apps](https://www.spotify.com/account/apps/).
5. Revoke the key in your Apple developer account.

## Troubleshooting

- **"Waiting for Apple cloud and Mac Music to converge":** iCloud hasn't caught up yet. The plan is saved; wait a few minutes and run `python3 -m duet sync` again. The schedule retries on its own.
- **"Playlist changed outside the pending sync":** someone edited a playlist while a sync was unfinished. Look at `pending.json` and both playlists before doing anything. Don't delete state files to clear an error, because Duet would lose track of which songs were deleted.
- **An authorization error:** rerun the matching `connect-*` command.

## Why a Mac

Apple's Music API can [create playlists and add songs](https://developer.apple.com/documentation/applemusicapi/playlists-api), but it can't remove songs or rename a playlist. The MusicKit framework's playlist-editing methods are unavailable on macOS. So Duet uses Apple's API for reading and adding, and the Music app's scripting interface (`duet/bridge.js`) for removals and renames.

Before each removal, Duet checks that the cloud copy and the Mac's copy of the playlist agree. It then matches each song to its local track and checks again immediately before editing. The script only ever removes tracks from the one playlist Duet created; it never deletes a playlist or anything from your library.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m duet demo
```

`demo` shows a sample merge using made-up songs. It never contacts either service.

| File | Purpose |
| --- | --- |
| `duet/core.py` | Merge rules, song matching, and recovery checks |
| `duet/engine.py` | Planning, the saved plan, writes, and final verification |
| `duet/providers.py` | Spotify and Apple Music API clients and the Music app bridge |
| `duet/bridge.js` | Music app scripting, limited to the paired playlist |
| `duet/auth.py` | Spotify and Apple Music browser logins and Apple token signing |
| `duet/alerts.py` | Mac notifications |
| `duet/__main__.py` | Command-line commands and the LaunchAgent |
| `tests/` | Merge, recovery, adapter, and notification tests |

## License

[MIT](LICENSE)
