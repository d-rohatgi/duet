"""Post each sync's changes to Messages, e.g. "🎶 lil bug added Opalite"."""
import subprocess

from .core import SyncError

LIMIT = 5  # song titles listed per line before "and N more"

# The destination and message arrive as arguments, never interpolated into AppleScript.
TO_CHAT = ["on run argv",
           'tell application "Messages"',
           "set matches to (chats whose name is (item 1 of argv))",
           'if (count of matches) is not 1 then error "Expected exactly one Messages chat named " & (item 1 of argv)',
           "send (item 2 of argv) to (item 1 of matches)",
           "end tell",
           "end run"]
# A one-on-one conversation, addressed by the person's phone number or email.
TO_PERSON = ["on run argv",
             'tell application "Messages"',
             "set chatAccount to 1st account whose service type = iMessage",
             "send (item 2 of argv) to participant (item 1 of argv) of chatAccount",
             "end tell",
             "end run"]


def send(destination, message):
    """destination is {"chat": group name} or {"to": phone number or email}."""
    script, target = (TO_CHAT, destination["chat"]) if destination.get("chat") else (TO_PERSON, destination["to"])
    command = ["/usr/bin/osascript"]
    for line in script:
        command += ["-e", line]
    try:
        result = subprocess.run(command + [target, message], capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        # Don't echo the command: it holds the recipient's number and the message.
        raise SyncError("Messages didn't respond. macOS is probably waiting for permission; "
                        "run python3 -m duet permissions at the Mac.") from None
    if result.returncode:
        raise SyncError("Couldn't post to the Messages chat: " + result.stderr.strip())


def listing(titles):
    shown = titles[:LIMIT]
    if len(titles) > LIMIT:
        return "%s and %d more" % (", ".join(shown), len(titles) - LIMIT)
    return shown[0] if len(shown) == 1 else "%s and %s" % (", ".join(shown[:-1]), shown[-1])


def compose(result, names, removals=True):
    """Turn a sync result into one message, credited to whoever made each change.

    Songs added to Apple Music came from the Spotify listener, and vice versa.
    """
    lines = []
    for person, changed in (("apple", "spotify"), ("spotify", "apple")):
        side = result[changed]
        if side["add"]:
            lines.append("🎶 %s added %s" % (names[person], listing(side["add"])))
        if removals and side["remove"]:
            lines.append("🗑️ %s removed %s" % (names[person], listing(side["remove"])))
        if side["rename"]:
            lines.append('✏️ %s renamed the playlist to "%s"' % (names[person], side["rename"]))
    return "\n".join(lines) or None


def announce(store, result):
    texts = store.read("config", {}).get("texts")
    message = texts and result and compose(result, texts["names"], texts.get("removals", True))
    if message:
        send(texts, message)
    return message
