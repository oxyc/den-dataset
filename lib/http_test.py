#!/usr/bin/env python3
"""The one outbound request, driven against a real local server.

Every rule here is one the stdlib does not give by default, and each one fails without a sound: a 404
retried is the same answer three times more slowly, a `Retry-After` ignored is how a rate limit becomes a
ban, and a request with no User-Agent is refused by the Wikimedia APIs with a 403 that reads exactly like a
dead article. None of that is visible from a stub of `request` itself, so this serves real HTTP.
"""
import http.client
import http.server
import threading
import unittest
from unittest import mock

from . import http as transport

HOST = "example.org"


class Server(http.server.BaseHTTPRequestHandler):
    """Answers from `plan` in order — `(status, headers)` — and records every request it saw."""

    protocol_version = "HTTP/1.1"
    plan = []
    seen = []

    def do_GET(self):
        type(self).seen.append((self.path, dict(self.headers)))
        status, headers = type(self).plan.pop(0) if type(self).plan else (200, {})
        # A 304 carries no body, and a client reading one would take it as the start of the next answer.
        body = b"" if status == 304 else b'{"ok":1}'
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class Request(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Server)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Server.plan, Server.seen = [], []
        self.sleeps, self.timeouts = [], []
        port = self.server.server_address[1]

        def connect(host, timeout):
            # Plain HTTP to the local server, standing in for the HTTPS the module opens.
            self.timeouts.append(timeout)
            return http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)

        transport._connections.drop(HOST)
        patches = (mock.patch.object(transport.http.client, "HTTPSConnection", connect),
                   mock.patch.object(transport.time, "sleep", self.sleeps.append),
                   # The lower bound unless a test says otherwise: the schedule, jitter-free.
                   mock.patch.object(transport.random, "uniform", lambda low, high: low))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(transport._connections.drop, HOST)

    def ask(self, *plan):
        Server.plan = list(plan)
        return transport.request(HOST, "/w/api.php", {"page": "Knife+Heart"})

    def test_a_definitive_answer_is_asked_once(self):
        """A 404 on a stale sitelink is the server's answer. Retrying it just fails more slowly."""
        for status in (404, 400, 413):
            Server.seen = []
            with self.assertRaises(transport.HTTPError) as refused:
                self.ask((status, {}))
            self.assertEqual(refused.exception.status, status)
            self.assertEqual(len(Server.seen), 1, f"{status} was asked again")
        self.assertEqual(self.sleeps, [])

    def test_a_transient_failure_is_asked_again(self):
        self.assertEqual(self.ask((503, {}), (200, {})), b'{"ok":1}')
        self.assertEqual(len(Server.seen), 2)

    def test_the_wait_is_the_swift_schedule_at_least(self):
        """0.5 + 1 + 2 = 3.5s before the last attempt, the schedule the Swift passes ran every scrape on.
        Jitter drawn from zero halved it; a blip that outlasts the shorter wait drops the title."""
        with self.assertRaises(transport.HTTPError):
            self.ask(*[(503, {})] * 4)
        self.assertEqual(len(Server.seen), 4)
        self.assertEqual(self.sleeps, [0.5, 1.0, 2.0])

    def test_jitter_is_added_on_top_of_the_schedule(self):
        """Workers that back off on one curve re-collide, so each wait is spread — upward only."""
        with mock.patch.object(transport.random, "uniform", lambda low, high: high):
            with self.assertRaises(transport.HTTPError):
                self.ask(*[(429, {})] * 4)
        self.assertEqual(self.sleeps, [0.75, 1.5, 3.0])

    def test_the_servers_own_wait_is_honoured(self):
        """Backing off on our own curve against a server that has said how long to wait is how a rate
        limit turns into a ban."""
        self.ask((429, {"Retry-After": "7"}), (200, {}))
        self.assertEqual(self.sleeps, [7.0])

    def test_a_wait_past_the_cap_gives_up_and_says_why(self):
        """Neither sleeping an hour nor retrying sooner than asked: the request stops, and the error names
        the wait it refused rather than reading like any other 429."""
        with self.assertRaises(transport.HTTPError) as refused:
            self.ask(*[(429, {"Retry-After": "3600"})] * 4)
        self.assertEqual(len(Server.seen), 1)
        self.assertEqual(self.sleeps, [])
        self.assertIn("3600s", str(refused.exception))
        self.assertIn(f"{transport.MAX_RETRY_AFTER}s", str(refused.exception))

    def test_every_request_identifies_itself_and_has_a_timeout(self):
        """The Wikimedia APIs refuse unidentified traffic with a 403 that reads like a dead article, and
        `urlopen`'s default of no timeout lets one hung socket stall a twelve-hour drain."""
        self.ask((200, {}))
        _path, headers = Server.seen[0]
        self.assertEqual(headers.get("User-Agent"), transport.USER_AGENT)
        self.assertEqual(self.timeouts, [transport.TIMEOUT])

    def test_a_plus_in_a_title_stays_a_plus(self):
        """Form encoding turns `+` into a space, and "Knife+Heart" came back `missingtitle`."""
        self.ask((200, {}))
        path, _headers = Server.seen[0]
        self.assertEqual(path, "/w/api.php?page=Knife%2BHeart")

    def test_a_conditional_get_answered_304_is_unchanged_not_an_error(self):
        """The IMDb dump is refreshed by asking whether it changed; the usual answer is 304, and the caller
        needs the validators of a 200 to ask that next time."""
        Server.plan = [(200, {"ETag": '"v1"', "Last-Modified": "Mon, 21 Sep 2026 00:00:00 GMT"})]
        received = {}
        self.assertEqual(transport.request(HOST, "/dump", received=received), b'{"ok":1}')
        self.assertEqual(received, {"status": 200, "etag": '"v1"',
                                    "last-modified": "Mon, 21 Sep 2026 00:00:00 GMT"})
        Server.plan = [(304, {})]
        received = {}
        self.assertEqual(transport.request(HOST, "/dump", headers={"If-None-Match": '"v1"'},
                                           received=received), b"")
        self.assertEqual(received["status"], 304)
        self.assertEqual(Server.seen[-1][1].get("If-None-Match"), '"v1"')

    def test_a_304_nobody_asked_for_is_still_an_error(self):
        """Without a validator sent, a 304 says nothing about a copy the caller holds — returning its empty
        body would read as an empty file."""
        with self.assertRaises(transport.HTTPError) as refused:
            self.ask((304, {}))
        self.assertEqual(refused.exception.status, 304)

    def test_a_connection_that_cannot_be_made_is_retried_then_refused(self):
        def refuse(host, timeout):
            raise ConnectionRefusedError("nothing listening")

        with mock.patch.object(transport.http.client, "HTTPSConnection", refuse):
            with self.assertRaises(transport.HTTPError) as refused:
                transport.request(HOST, "/x")
        self.assertEqual(refused.exception.status, 0)
        self.assertEqual(self.sleeps, [0.5, 1.0, 2.0])


if __name__ == "__main__":
    unittest.main()
