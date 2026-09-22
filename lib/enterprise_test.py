#!/usr/bin/env python3
"""When Wikimedia Enterprise is asked, against a local server standing in for all three endpoints.

Everything goes through the REAL `lib/http` retry loop; only the connection is pointed at localhost. The
server answers `get-user` with the account's on-demand counters (and, as the real one does, the account's
name and APIs — which must never reach a log), Enterprise as each test says, and the action API every time.
"""
import contextlib
import io
import json
import threading
import time
import unittest
from http import client as http_client
from http import server as http_server
from unittest import mock

from . import enterprise
from . import http
from . import plot

PAYLOAD = [{"sections": [{"name": "Plot", "has_parts": [{"type": "paragraph", "value": "McNulty."}]}]}]
ACTION = {"parse": {"title": "The Wire", "revid": 99, "wikitext": "== Plot ==\nThe action API's plot."}}
BEARER = "secret-bearer-4b1d"
ACCOUNT = {"username": "secret-user-7c2e", "apis": ["secret-api-9f3a"]}


class Account(unittest.TestCase):
    def setUp(self):
        self.enterprise, self.usage, self.usage_status = (200, ()), 100, 200
        self.hits, self.usage_bodies = [], []
        test = self

        class Handler(http_server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, status, body, headers=()):
                data = json.dumps(body).encode()
                self.send_response(status)
                for name, value in headers:
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path == enterprise.USAGE_PATH:
                    test.hits.append("usage")
                    test.usage_bodies.append(json.loads(body))
                    answer = dict(ACCOUNT, ondemand_requests_count=test.usage, ondemand_limit=50000)
                    self.send(test.usage_status, answer if test.usage_status == 200 else ACCOUNT)
                    return
                test.hits.append("enterprise")
                status, headers = test.enterprise
                self.send(status, PAYLOAD if status == 200 else {"error": "no"}, headers)

            def do_GET(self):
                test.hits.append("action")
                self.send(200, ACTION)

        server = http_server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        self.stderr = io.StringIO()
        self.gate = enterprise.Gate(headroom=500)
        for patch in (mock.patch.object(http._connections, "get", lambda host, timeout:
                                        http_client.HTTPConnection("127.0.0.1", port, timeout=timeout)),
                      mock.patch.object(http._connections, "drop", lambda host: None),
                      mock.patch.object(enterprise, "gate", self.gate),
                      mock.patch("sys.stderr", self.stderr)):
            patch.start()
            self.addCleanup(patch.stop)

    def plot(self):
        return plot.plot("The Wire", token=BEARER)

    # -- the breaker ----------------------------------------------------------------------------------

    def test_an_answer_from_enterprise_says_it_served(self):
        found = self.plot()
        self.assertEqual((found["source"], found["text"], self.hits),
                         (plot.ENTERPRISE, "McNulty.", ["usage", "enterprise"]))

    def test_a_throttle_that_names_a_wait_costs_one_request_not_the_wait(self):
        """`Retry-After: 5` through the retry loop cost 15 seconds per candidate before the fallback, with the
        action API — same coverage — one request away."""
        self.enterprise = (429, [("Retry-After", "5")])
        start = time.monotonic()
        found = self.plot()
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual((found["source"], found["revId"]), (plot.ACTION_API, 99))
        self.assertEqual(self.hits.count("enterprise"), 1)

    def test_throttles_in_a_row_switch_enterprise_off_for_the_run_and_say_so_once(self):
        """With the account's count UNDER its limit: another machine can spend the month between two looks,
        and the throttle is what says so."""
        self.enterprise = (429, [("Retry-After", "5")])
        for _ in range(enterprise.THROTTLES + 3):
            self.assertEqual(self.plot()["source"], plot.ACTION_API)
        self.assertEqual(self.hits.count("enterprise"), enterprise.THROTTLES)
        self.assertEqual(enterprise.THROTTLES, 8, "twice the four grounding workers")
        self.assertEqual(self.stderr.getvalue().count("switched off"), 1)
        self.assertIn("429", self.gate.off)

    def test_refusals_already_in_flight_when_it_switches_off_announce_nothing_more(self):
        """Four workers are in flight when the breaker trips, and their 429s still come back."""
        def throttled():
            raise http.HTTPError(429, "https://api.enterprise.wikimedia.com/v2/structured-contents/X")
        for _ in range(enterprise.THROTTLES + 3):
            with self.assertRaises(http.HTTPError):
                self.gate.sent(throttled)
        self.assertEqual(self.stderr.getvalue().count("switched off"), 1)

    def test_an_answer_between_throttles_resets_the_count(self):
        for answer in [(429, ())] * (enterprise.THROTTLES - 1) + [(200, ())] + \
                [(429, ())] * (enterprise.THROTTLES - 1):
            self.enterprise = answer
            self.plot()
        self.assertIsNone(self.gate.off)

    def test_an_expired_bearer_switches_enterprise_off_at_once(self):
        """A 401 does not come back, and asking with it before every action-API fetch doubles the requests."""
        for status in (401, 403):
            with mock.patch.object(enterprise, "gate", enterprise.Gate(headroom=500)) as gate:
                self.enterprise, self.hits = (status, ()), []
                self.plot()
                found = self.plot()
                self.assertEqual((found["source"], self.hits),
                                 (plot.ACTION_API, ["usage", "enterprise", "action", "action"]))
                self.assertIn(str(status), gate.off)

    # -- the account's monthly count --------------------------------------------------------------------

    def test_a_spent_month_is_not_asked_once(self):
        """The count at the first look says the month is gone: not one on-demand request is sent."""
        self.usage = 50001
        for _ in range(3):
            self.assertEqual(self.plot()["source"], plot.ACTION_API)
        self.assertEqual(self.hits, ["usage", "action", "action", "action"])
        self.assertIn("50,001 of its 50,000", self.gate.off)
        self.assertEqual(self.stderr.getvalue().count("switched off"), 1)

    def test_the_reserve_is_held_back_for_the_other_machines(self):
        self.usage = 50000 - 500
        self.assertEqual(self.plot()["source"], plot.ACTION_API, "at limit - reserve")
        self.assertNotIn("enterprise", self.hits)
        with mock.patch.object(enterprise, "gate", enterprise.Gate(headroom=500)):
            self.usage, self.hits = 50000 - 501, []
            self.assertEqual(self.plot()["source"], plot.ENTERPRISE, "one under it")

    def test_the_reserve_is_read_from_the_environment_and_a_bad_one_refused(self):
        self.assertEqual(enterprise.reserve({}), enterprise.DEFAULT_RESERVE)
        self.assertEqual(enterprise.DEFAULT_RESERVE, 500)
        self.assertEqual(enterprise.reserve({"DEN_ENTERPRISE_RESERVE": "20"}), 20)
        for bad in ("lots", "-1"):
            with self.assertRaises(ValueError):
                enterprise.reserve({"DEN_ENTERPRISE_RESERVE": bad})

    def test_the_count_is_read_again_mid_run_and_a_crossing_stops_it(self):
        """Another machine's spending stops this run too, within `RECHECK_EVERY` requests."""
        with mock.patch.object(enterprise, "gate", enterprise.Gate(recheck_every=3, headroom=500)) as gate:
            for _ in range(3):
                self.assertEqual(self.plot()["source"], plot.ENTERPRISE)
            self.usage = 49600
            for _ in range(2):
                self.assertEqual(self.plot()["source"], plot.ACTION_API)
        self.assertEqual(self.hits.count("usage"), 2)
        self.assertEqual(self.hits.count("enterprise"), 3)
        self.assertEqual((gate.first, gate.latest), ((100, 50000), (49600, 50000)))
        self.assertEqual(enterprise.RECHECK_EVERY, 100)

    def test_a_failing_get_user_leaves_the_breaker_as_the_only_guard_and_says_so_once(self):
        for status, body_ok in ((401, True), (200, False)):
            with mock.patch.object(enterprise, "gate", enterprise.Gate(recheck_every=2, headroom=500)) as gate:
                self.hits = []
                self.stderr.seek(0)
                self.stderr.truncate()
                self.usage_status = status
                self.usage = 100 if body_ok else "not a count"
                self.enterprise = (429, ())
                for _ in range(enterprise.THROTTLES + 2):
                    self.plot()
                self.assertEqual(self.hits.count("usage"), 1, "not asked again once it has failed")
                self.assertEqual(self.hits.count("enterprise"), enterprise.THROTTLES, "the breaker still trips")
                self.assertIsNotNone(gate.unknown)
                self.assertEqual(self.stderr.getvalue().count("usage is unknown"), 1)

    def test_the_bearer_travels_in_the_body_and_nothing_secret_is_printed(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.plot()
            self.usage = 50001
            with mock.patch.object(enterprise, "gate", enterprise.Gate(headroom=500)):
                self.plot()
            self.usage = "not a count"
            with mock.patch.object(enterprise, "gate", enterprise.Gate(headroom=500)):
                self.plot()
            self.usage_status = 401
            with mock.patch.object(enterprise, "gate", enterprise.Gate(headroom=500)) as gate:
                self.enterprise = (401, ())
                self.plot()
                gate.refresh()
        self.assertEqual(self.usage_bodies[0], {"access_token": BEARER})
        printed = out.getvalue() + self.stderr.getvalue()
        self.assertIn("switched off", printed, "something was said")
        for secret in (BEARER, ACCOUNT["username"], ACCOUNT["apis"][0]):
            self.assertNotIn(secret, printed)

    def test_the_end_of_run_count_is_read_from_the_server(self):
        self.plot()
        self.usage = 250
        self.assertEqual(self.gate.refresh(), ((100, 50000), (250, 50000)))


if __name__ == "__main__":
    unittest.main()
