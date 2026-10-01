import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import secrets as random_secrets
import subprocess
import time
from urllib.parse import parse_qs, urlencode, urlparse
import webbrowser

from .core import SyncError
from .providers import request_json

PORT = 8765
ORIGIN = "http://127.0.0.1:%d" % PORT
REDIRECT = ORIGIN + "/callback"
_token_cache = {}


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def der_to_raw(signature):
    """OpenSSL DER ECDSA signature -> the fixed-width ES256 JWT format."""
    offset = 0

    def take(tag):
        nonlocal offset
        if signature[offset] != tag:
            raise SyncError("Unexpected Apple signing-key format.")
        offset += 1
        size = signature[offset]
        offset += 1
        if size & 128:
            count = size & 127
            size = int.from_bytes(signature[offset:offset + count], "big")
            offset += count
        return size

    sequence_length = take(0x30)
    if sequence_length != len(signature) - offset:
        raise SyncError("Invalid ECDSA signature.")
    numbers = []
    for _ in range(2):
        length = take(0x02)
        number = int.from_bytes(signature[offset:offset + length], "big")
        offset += length
        numbers.append(number.to_bytes(32, "big"))
    if offset != len(signature):
        raise SyncError("Invalid ECDSA signature length.")
    return b"".join(numbers)


def developer_token(config):
    identity = tuple(config.get(k) for k in ("apple_team_id", "apple_key_id", "apple_key_path"))
    if not all(identity):
        raise SyncError("Configure the Apple Music developer key first.")
    cached = _token_cache.get(identity)
    if cached and cached[0] > time.time() + 60:
        return cached[1]
    now = int(time.time())
    header = b64(json.dumps({"alg": "ES256", "kid": identity[1]}).encode())
    payload = b64(json.dumps({"iss": identity[0], "iat": now, "exp": now + 3600}).encode())
    message = (header + "." + payload).encode()
    result = subprocess.run(["/usr/bin/openssl", "dgst", "-sha256", "-sign",
                             str(Path(identity[2]).expanduser())], input=message, capture_output=True)
    if result.returncode:
        raise SyncError("Could not sign the Apple developer token. Check the configured MusicKit .p8 key.")
    token = message.decode() + "." + b64(der_to_raw(result.stdout))
    _token_cache[identity] = (now + 3600, token)
    return token


def connect(provider, config, store):
    state = random_secrets.token_urlsafe(32)
    verifier = random_secrets.token_urlsafe(64)
    result = {}
    apple_token = developer_token(config) if provider == "apple" else None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # callback URLs and credentials must not appear in logs

        def respond(self, status, body, kind="text/html; charset=utf-8", referrer="no-referrer"):
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", referrer)
            self.end_headers()
            self.wfile.write(body.encode())

        def trusted_host(self):
            return self.headers.get("Host") == "127.0.0.1:%d" % PORT

        def do_GET(self):
            if not self.trusted_host():
                return self.respond(403, "Unexpected host")
            url = urlparse(self.path)
            if provider == "spotify" and url.path == "/callback":
                query = parse_qs(url.query)
                if not random_secrets.compare_digest(query.get("state", [""])[0], state):
                    return self.respond(403, "Invalid connection state")
                if "code" not in query:
                    result["error"] = "Spotify connection was declined."
                    return self.respond(400, "Connection declined. You can close this window.")
                result["code"] = query["code"][0]
                return self.respond(200, "<h1>Spotify connected</h1><p>You can close this window and return to Duet.</p>")
            if provider == "apple" and url.path == "/":
                html = '''<!doctype html><html lang="en"><meta charset="utf-8"><title>Connect Apple Music · Duet</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><style>
body{font:18px system-ui;max-width:36rem;margin:15vh auto;padding:24px;background:#faf7f3;color:#292424}
button{font:inherit;background:#a73147;color:white;border:0;border-radius:12px;padding:14px 22px;cursor:pointer}
</style><h1>One playlist, together.</h1><p>Connect your Apple Music account to Duet.</p>
<button id="connect" disabled>Loading Apple Music…</button><p id="status"></p>
<script>
document.addEventListener('musickitloaded',async()=>{
try{await MusicKit.configure({developerToken:APPLE_TOKEN,app:{name:'Duet',build:'1.0'}});
const button=document.getElementById('connect');button.disabled=false;button.textContent='Connect Apple Music';
button.onclick=async()=>{try{button.disabled=true;const token=await MusicKit.getInstance().authorize();
const response=await fetch('/apple-token',{method:'POST',headers:{'Content-Type':'application/json','X-Duet-State':CSRF_STATE},body:JSON.stringify({token})});
if(!response.ok)throw new Error('Could not save connection');
document.getElementById('status').textContent='Connected. Return to Duet; you can close this window.';
}catch(e){document.getElementById('status').textContent=e.message+(/unauthorized/i.test(e.message)?
' Make sure this Apple Account has an active Apple Music subscription.':'');button.disabled=false;}};
}catch(e){document.getElementById('status').textContent=e.message;}});
</script><script src="https://js-cdn.music.apple.com/musickit/v3/musickit.js"></script></html>'''
                # MusicKit's sign-in rejects requests that don't name the page's
                # origin ("Unauthorized" after Allow), so this page must not use
                # no-referrer. Its URL holds nothing private; only the origin is sent.
                return self.respond(200, html.replace("APPLE_TOKEN", json.dumps(apple_token))
                                    .replace("CSRF_STATE", json.dumps(state)), referrer="origin")
            self.respond(404, "Not found")

        def do_POST(self):
            if (not self.trusted_host() or provider != "apple" or self.path != "/apple-token"
                    or self.headers.get("Origin") != ORIGIN
                    or not random_secrets.compare_digest(self.headers.get("X-Duet-State", ""), state)):
                return self.respond(403, "Invalid request")
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 32000:
                    raise ValueError()
                body = json.loads(self.rfile.read(size))
                if not isinstance(body.get("token"), str) or not body["token"]:
                    raise ValueError()
            except (ValueError, KeyError):
                return self.respond(400, "Invalid token")
            result["apple_user_token"] = body["token"]
            self.respond(200, "Saved")

    with HTTPServer(("127.0.0.1", PORT), Handler) as server:
        server.timeout = 1
        if provider == "spotify":
            url = "https://accounts.spotify.com/authorize?" + urlencode({
                "client_id": config["spotify_client_id"], "response_type": "code",
                "redirect_uri": REDIRECT, "state": state, "show_dialog": "true",
                "code_challenge_method": "S256", "code_challenge": b64(hashlib.sha256(verifier.encode()).digest()),
                "scope": "playlist-read-private playlist-read-collaborative playlist-modify-private playlist-modify-public"})
        else:
            url = ORIGIN
        print("Opening %s authorization in your browser. Waiting up to five minutes…" % provider, flush=True)
        webbrowser.open(url)
        deadline = time.monotonic() + 300
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if not result:
        raise SyncError("Connection timed out; run the command again.")
    if "error" in result:
        raise SyncError(result["error"])
    secrets = store.read("secrets", {})
    if provider == "spotify":
        token = request_json("https://accounts.spotify.com/api/token", method="POST", form={
            "grant_type": "authorization_code", "code": result["code"], "redirect_uri": REDIRECT,
            "client_id": config["spotify_client_id"], "code_verifier": verifier})
        token["expires_at"] = time.time() + token["expires_in"]
        secrets["spotify"] = token
    else:
        secrets["apple_user_token"] = result["apple_user_token"]
    store.write("secrets", secrets)
