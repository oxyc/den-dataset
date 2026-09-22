#!/usr/bin/env python3
"""The Jev client against a local server that misbehaves the way the network does.

A paid run died at 24k of 47.5k titles on `http.client.RemoteDisconnected` raised out of `urlopen`: the
client caught `URLError`, which `urlopen` only raises for a failure while SENDING. These run the real
client over a real socket, so the exceptions are the ones `urllib` actually raises, not ones a mock
guessed at. Backoff sleeps are patched out and recorded.
"""
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import typesafe_client

KEY = "test-key-never-printed"
PAYLOAD = {"answers": {"q": {"type": "noul", "noul": 0.8125}}, "model": "jev-1.13.0",
           "usage": {"input_tokens": 1234, "output_tokens": 5}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.server.requests.append(self.headers.get("Authorization"))
        step = self.server.script.pop(0) if self.server.script else self.server.default
        if step == "drop":
            # Read the request, answer nothing, close: what the far end did to the paid run.
            self.close_connection = True
            return
        if step == "truncate":
            body = json.dumps(PAYLOAD).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body[:len(body) // 2])
            self.close_connection = True
            return
        status, headers = step if isinstance(step, tuple) else (step, {})
        body = json.dumps(PAYLOAD if status == 200 else {"error": "no"}).encode()
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ClientTransportTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.requests, self.server.script, self.server.default = [], [], 200
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.client = typesafe_client.TypeSafe(
            key=KEY, timeout=5, endpoint=f"http://127.0.0.1:{self.server.server_address[1]}/v1/systemone")
        sleep = mock.patch.object(typesafe_client.time, "sleep")
        self.sleeps = sleep.start()
        self.addCleanup(sleep.stop)

    def ask(self):
        return self.client.ask_with_metadata({"article": "x"}, {"q": {"type": "noul", "instructions": "?"}})

    def test_one_dropped_connection_then_an_answer_is_answered(self):
        self.server.script = ["drop"]
        answers, metadata = self.ask()
        self.assertEqual(answers, PAYLOAD["answers"])
        self.assertEqual(metadata, {"model": "jev-1.13.0",
                                    "usage": {"input_tokens": 1234, "output_tokens": 5}})
        self.assertEqual(len(self.server.requests), 2)
        # Only the answered call is counted as spend.
        self.assertEqual((self.client.calls, self.client.input_tokens), (1, 1234))

    def test_a_body_cut_short_is_asked_again(self):
        self.server.script = ["truncate"]
        answers, _ = self.ask()
        self.assertEqual(answers, PAYLOAD["answers"])
        self.assertEqual(len(self.server.requests), 2)

    def test_persistent_drops_end_as_the_clients_own_error_after_every_attempt(self):
        self.server.default = "drop"
        with self.assertRaises(typesafe_client.TypeSafeError) as caught:
            self.ask()
        self.assertEqual(len(self.server.requests), typesafe_client.RETRY_STATUS_MAX)
        self.assertEqual(self.sleeps.call_count, typesafe_client.RETRY_STATUS_MAX - 1)
        self.assertIn("RemoteDisconnected", str(caught.exception))
        self.assertNotIn(KEY, str(caught.exception))
        # `from None`: nothing below the client's error is chained on for a traceback to print.
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertEqual(self.client.calls, 0)

    def test_a_4xx_is_not_retried(self):
        self.server.script = [422]
        with self.assertRaisesRegex(typesafe_client.TypeSafeError, "HTTP 422"):
            self.ask()
        self.assertEqual(len(self.server.requests), 1)
        self.sleeps.assert_not_called()

    def test_retry_after_is_obeyed(self):
        self.server.script = [(429, {"Retry-After": "7"}), (529, {})]
        answers, _ = self.ask()
        self.assertEqual(answers, PAYLOAD["answers"])
        self.assertEqual(len(self.server.requests), 3)
        self.assertEqual(self.sleeps.call_args_list[0], mock.call(7.0))

    def test_a_retry_after_past_the_cap_gives_up_rather_than_retrying_early(self):
        self.server.script = [(429, {"Retry-After": "3600"})]
        with self.assertRaisesRegex(typesafe_client.TypeSafeError, "3600s"):
            self.ask()
        self.assertEqual(len(self.server.requests), 1)
        self.sleeps.assert_not_called()


if __name__ == "__main__":
    unittest.main()
