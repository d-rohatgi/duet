import json
import re
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from duet.auth import connect
from duet.storage import Store


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ConnectPageTests(unittest.TestCase):
    """Runs the real local sign-in server on a spare port, playing the browser's part."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.port = free_port()
        self.origin = "http://127.0.0.1:%d" % self.port
        self.seen = {}
        for patcher in (patch.multiple("duet.auth", PORT=self.port, ORIGIN=self.origin),
                        patch("builtins.print")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_connect(self, provider, config, browser):
        def open_in_background(url):
            threading.Thread(target=browser, args=(url,), daemon=True).start()
        with patch("duet.auth.webbrowser.open", side_effect=open_in_background):
            connect(provider, config, self.store)

    def test_apple_page_names_its_origin_and_saves_the_token(self):
        def browser(url):
            with urlopen(url, timeout=5) as page:
                self.seen["referrer"] = page.headers["Referrer-Policy"]
                html = page.read().decode()
            state = json.loads(re.search(r"'X-Duet-State':(\"[^\"]+\")", html).group(1))
            urlopen(Request(self.origin + "/apple-token", method="POST",
                            data=json.dumps({"token": "music-user-token"}).encode(),
                            headers={"Content-Type": "application/json", "Origin": self.origin,
                                     "X-Duet-State": state}), timeout=5).read()

        with patch("duet.auth.developer_token", return_value="developer-token"):
            self.run_connect("apple", {}, browser)
        # MusicKit's sign-in fails with "Unauthorized" under no-referrer.
        self.assertEqual(self.seen["referrer"], "origin")
        self.assertEqual(self.store.read("secrets")["apple_user_token"], "music-user-token")

    def test_spotify_callback_keeps_no_referrer(self):
        # Its URL carries Spotify's one-time code, so it must not leak via Referer.
        def browser(url):
            state = parse_qs(urlparse(url).query)["state"][0]
            with urlopen("%s/callback?code=one-time-code&state=%s" % (self.origin, state), timeout=5) as page:
                self.seen["referrer"] = page.headers["Referrer-Policy"]

        token = {"access_token": "a", "refresh_token": "r", "expires_in": 3600}
        with patch("duet.auth.request_json", return_value=token) as exchange:
            self.run_connect("spotify", {"spotify_client_id": "client"}, browser)
        self.assertEqual(self.seen["referrer"], "no-referrer")
        self.assertEqual(exchange.call_args.kwargs["form"]["code"], "one-time-code")
        self.assertEqual(self.store.read("secrets")["spotify"]["refresh_token"], "r")


if __name__ == "__main__":
    unittest.main()
