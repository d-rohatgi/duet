from copy import deepcopy
from pathlib import Path
import subprocess
import tempfile
import unittest
import unittest.mock
from unittest.mock import patch
from urllib.error import URLError

from duet.auth import developer_token, der_to_raw
from duet.core import SyncError
from duet.providers import Apple, HTTPFailure, Spotify, request_json
from test_sync import track


class AdapterTests(unittest.TestCase):
    def test_spotify_batches_removals_and_additions(self):
        provider = Spotify({"spotify_playlist": "test"}, None)
        current = {"name": "Old", "version": "v1", "tracks": [track(n, "spotify") for n in range(105)]}
        calls = []

        def api(path, method, body):
            calls.append((path, method, body))
            if method == "DELETE":
                ids = {x["uri"].split(":")[-1] for x in body["items"]}
                current["tracks"] = [t for t in current["tracks"] if t["id"] not in ids]
            elif method == "POST":
                current["tracks"].extend(track(int(x.split(":")[-1][1:]), "spotify") for x in body["uris"])
            else:
                current["name"] = body["name"]
            current["version"] += "x"

        provider.snapshot = lambda: deepcopy(current)
        provider.api = api
        provider.apply([track(n, "spotify") for n in range(105, 210)], "New", deepcopy(current))
        self.assertEqual([method for _, method, _ in calls], ["DELETE", "DELETE", "POST", "POST", "PUT"])
        self.assertEqual([len(body["items"]) for _, method, body in calls if method == "DELETE"], [100, 5])
        self.assertEqual(current["name"], "New")
        self.assertEqual(len(current["tracks"]), 105)
        self.assertTrue(all(path.endswith("/items") for path, method, _ in calls if method != "PUT"))

    def test_spotify_rechecks_before_writes(self):
        provider = Spotify({"spotify_playlist": "test"}, None)
        before = {"name": "Old", "tracks": []}
        provider.snapshot = lambda: {"name": "User edit", "tracks": []}
        with patch.object(provider, "api") as api:
            with self.assertRaises(SyncError):
                provider.apply([], "New", before)
            api.assert_not_called()

    def fake_response(self, body, encoding=None):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = body
        response.headers = {"Content-Encoding": encoding} if encoding else {}
        return response

    def test_gzipped_responses_are_decoded(self):
        import gzip
        packed = gzip.compress(b'{"data": [{"id": "p.1"}]}')
        # Apple's playlist creation reply arrived gzipped without being asked for.
        for encoding in ("gzip", None):
            with patch("duet.providers.urlopen", return_value=self.fake_response(packed, encoding)):
                self.assertEqual(request_json("https://api.music.apple.com/v1/x", "POST", {}),
                                 {"data": [{"id": "p.1"}]})

    def test_plain_and_empty_responses_are_unchanged(self):
        for body, expected in ((b'{"ok": true}', {"ok": True}), (b"", {})):
            with patch("duet.providers.urlopen", return_value=self.fake_response(body)):
                self.assertEqual(request_json("https://api.spotify.com/v1/x"), expected)

    def test_timeout_does_not_repeat_an_ambiguous_post(self):
        with patch("duet.providers.urlopen", side_effect=URLError("lost response")) as request:
            with self.assertRaises(SyncError):
                request_json("https://api.spotify.com/v1/example", "POST", {"x": 1})
            self.assertEqual(request.call_count, 1)

    def test_untrusted_pagination_url_is_rejected_before_auth(self):
        spotify = Spotify({}, None)
        with patch.object(spotify, "token") as token:
            with self.assertRaises(SyncError):
                spotify.api("https://example.com/steal")
            token.assert_not_called()

    def test_apple_waits_for_local_cloud_convergence(self):
        provider = Apple({}, None)
        provider.snapshot = lambda: {"name": "N", "tracks": []}
        with patch.object(provider, "preflight", side_effect=[SyncError("Not ready"), None]) as preflight:
            with patch("duet.providers.time.sleep"):
                provider.await_state("N", [])
            self.assertEqual(preflight.call_count, 2)

    def fake_apple(self, track_pages):
        provider = Apple({"apple_playlist": "p.1", "storefront": "us"}, None)
        playlist = {"data": [{"attributes": {"name": "N", "canEdit": True, "lastModifiedDate": "d"}}]}

        def api(path, method="GET", body=None):
            if "/tracks" not in path:
                return playlist
            page = track_pages.pop(0)
            if isinstance(page, Exception):
                raise page
            return page
        provider.api = api
        return provider

    def test_apple_empty_playlist_404_reads_as_empty(self):
        provider = self.fake_apple([HTTPFailure("not found", 404)])
        self.assertEqual(provider.snapshot(), {"name": "N", "tracks": []})

    def test_apple_404_after_first_page_is_not_empty(self):
        row = {"id": "i.1", "type": "library-songs", "attributes": {
            "name": "Song", "artistName": "Artist", "durationInMillis": 1234, "playParams": {}}}
        provider = self.fake_apple([{"data": [row], "next": "/v1/me/library/playlists/p.1/tracks?offset=100"}, HTTPFailure("not found", 404)])
        with self.assertRaises(HTTPFailure):
            provider.snapshot()

    def test_apple_other_errors_are_not_empty(self):
        provider = self.fake_apple([HTTPFailure("unauthorized", 401)])
        with self.assertRaises(HTTPFailure):
            provider.snapshot()

    def test_apple_convergence_allows_re_identified_songs(self):
        provider = Apple({}, None)
        provider.snapshot = lambda: {"name": "N", "tracks": [track(1, id="1001"), track(2)]}
        provider.preflight = lambda snapshot: None
        with patch("duet.providers.time.sleep") as sleep:
            provider.await_state("N", [track(1), track(2)])
        sleep.assert_not_called()

    def test_apple_local_cloud_mismatch_blocks_edits(self):
        provider = Apple({}, None)
        provider.local = lambda: {"name": "N", "tracks": []}
        with self.assertRaises(SyncError):
            provider.preflight({"name": "N", "tracks": [track(1)]})

    def test_real_openssl_signing_uses_es256_signature_format(self):
        import base64
        import json
        with tempfile.TemporaryDirectory() as folder:
            key = Path(folder) / "ephemeral.p8"
            subprocess.run(["/usr/bin/openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(key)],
                           check=True, capture_output=True)
            token = developer_token({"apple_team_id": "TESTTEAM", "apple_key_id": "TESTKEY", "apple_key_path": str(key)})
            header, claims, signature = token.split(".")
            decode = lambda x: base64.urlsafe_b64decode(x + "=" * (-len(x) % 4))
            self.assertEqual(json.loads(decode(header))["alg"], "ES256")
            self.assertEqual(json.loads(decode(claims))["iss"], "TESTTEAM")
            self.assertEqual(len(decode(signature)), 64)
            # Re-encode the JWT signature independently and verify it with
            # OpenSSL's public-key verifier, not our signing implementation.
            raw = decode(signature)
            integers = []
            for half in (raw[:32], raw[32:]):
                value = half.lstrip(b"\x00") or b"\x00"
                if value[0] & 128:
                    value = b"\x00" + value
                integers.append(b"\x02" + bytes([len(value)]) + value)
            inner = b"".join(integers)
            der = b"\x30" + bytes([len(inner)]) + inner
            signature_file = Path(folder) / "signature.der"
            signature_file.write_bytes(der)
            public_file = Path(folder) / "public.pem"
            public = subprocess.run(["/usr/bin/openssl", "pkey", "-in", str(key), "-pubout"],
                                    check=True, capture_output=True).stdout
            public_file.write_bytes(public)
            verified = subprocess.run(["/usr/bin/openssl", "dgst", "-sha256", "-verify", str(public_file),
                                       "-signature", str(signature_file)], input=(header + "." + claims).encode(),
                                      capture_output=True)
            self.assertEqual(verified.returncode, 0, verified.stderr)


if __name__ == "__main__":
    unittest.main()
