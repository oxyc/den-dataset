#!/usr/bin/env python3
"""den-embed's client and the known-answer canary, against a service on a local port.

A real socket rather than a patched function, because the part most likely to be wrong is the part a patch
would skip: den-embed speaks plain HTTP on a port, which nothing else `lib/http.py` talks to does.
"""
import base64
import hashlib
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import denembed

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMMITTED = os.path.join(REPO, "data", "embed-canary.json")


def vector(text, dims=8):
    return [b - 256 if b > 127 else b for b in hashlib.sha256(text.encode()).digest()[:dims]]


class Service:
    """A den-embed stand-in: `/health` as configured, `/embed/batch` from `vector`, and every request
    recorded."""

    def __init__(self, health=None, status=200):
        self.health = health or {"model": "bge-m3", "dims": 8, "vector_epoch": 1, "runtime": "den-embed/9",
                                 "max_tokens": 1024}
        self.status, self.requests, self.override = status, [], {}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                owner.requests.append(("GET", self.path, None))
                self.reply(200, owner.health)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(("POST", self.path, body))
                if owner.status != 200:
                    self.reply(owner.status, {"error": "no"})
                    return
                self.reply(200, {"vectors": [owner.override.get(t, vector(t)) for t in body["texts"]]})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def canary_for(service, path, cases=(("a", "why", "first text"), ("b", "why", "second text"))):
    """A canary file whose answers are this service's, with digests computed the way the committed one's
    are — so a test can move one byte and watch it refuse."""
    rows = [{"id": i, "why": w, "text": t, "v": base64.b64encode(bytes(x & 0xFF for x in vector(t))).decode()}
            for i, w, t in cases]
    doc = {"canarySet": "test-v1", "dims": 8, "cases": rows, "textsSha256": denembed.texts_sha256(rows),
           "spaceId": denembed.space_id("test-v1", rows),
           "embedder": {"max_tokens": service.health["max_tokens"]}}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    return doc


class Served(unittest.TestCase):
    def setUp(self):
        self.service = Service()
        self.addCleanup(self.service.close)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.canary = os.path.join(self.directory.name, "canary.json")


class Client(Served):
    def test_identity_reads_the_snake_cased_fields(self):
        """`max_tokens` is the one whose JSON name differs, and read as absent it disables the fit check."""
        found = denembed.identity(self.service.url)
        self.assertEqual(found, {"model": "bge-m3", "dims": 8, "vectorEpoch": 1, "runtime": "den-embed/9",
                                 "maxTokens": 1024})

    def test_a_service_predating_the_fields_is_the_python_generation(self):
        self.service.health = {"model": "bge-m3", "dims": 1024}
        found = denembed.identity(self.service.url)
        self.assertEqual((found["runtime"], found["vectorEpoch"], found["maxTokens"]), ("pre-3.0.0", 0, 0))

    def test_vectors_come_back_verbatim_and_clamped_to_int8(self):
        self.service.override = {"x": [127, -128, 300, -300, 0, 1, 2, 3]}
        self.assertEqual(denembed.embed_many(self.service.url, ["x", "y"]),
                         [[127, -128, 127, -128, 0, 1, 2, 3], vector("y")])

    def test_a_413_is_not_retried(self):
        """It is the service's answer — the request was over its token budget — and asking again fails the
        same way, four times slower."""
        self.service.status = 413
        with self.assertRaises(denembed.http.HTTPError):
            denembed.embed_many(self.service.url, ["x"])
        self.assertEqual(len([r for r in self.service.requests if r[0] == "POST"]), 1)


class Identity(unittest.TestCase):
    def test_the_build_string_is_not_part_of_equality(self):
        """A release that changes nothing about the numbers must not invalidate a 47k-title corpus."""
        a = {"model": "bge-m3", "dims": 1024, "vectorEpoch": 1, "runtime": "den-embed/3.1.0", "maxTokens": 1024}
        self.assertTrue(denembed.same_embedder(a, dict(a, runtime="den-embed/3.4.2")))
        for field, value in (("vectorEpoch", 2), ("maxTokens", 512), ("dims", 384), ("model", "e5")):
            self.assertFalse(denembed.same_embedder(a, dict(a, **{field: value})), field)

    def test_a_record_written_before_the_epoch_existed_still_reads(self):
        self.assertEqual(denembed.stored_identity(
            {"model": "bge-m3", "dims": 1024, "runtime": "pre-3.0.0", "maxTokens": 0})["vectorEpoch"], 0)
        self.assertIsNone(denembed.stored_identity({"model": "bge-m3"}))


class Canary(Served):
    def test_a_service_that_reproduces_every_answer_is_stamped(self):
        doc = canary_for(self.service, self.canary)
        stamp = denembed.verify(self.canary, self.service.url, lambda line: None)
        self.assertEqual((stamp["spaceId"], stamp["canarySet"], stamp["url"]),
                         (doc["spaceId"], "test-v1", self.service.url))

    def test_one_differing_byte_refuses(self):
        """Byte-identical is the bar: int8 dot products have no tolerance for a rounding difference."""
        canary_for(self.service, self.canary)
        drifted = vector("second text")
        drifted[3] += 1
        self.service.override = {"second text": drifted}
        with self.assertRaises(denembed.CanaryFailure) as refused:
            denembed.verify(self.canary, self.service.url, lambda line: None)
        self.assertIn("1 of 2 cases (b)", str(refused.exception))

    def test_a_vector_of_another_length_refuses_however_its_prefix_reads(self):
        """No vector at all, a truncated one, one with an extra dimension: none is comparable dimension by
        dimension, and each counts as every dimension differing rather than as a near miss."""
        canary_for(self.service, self.canary)
        for returned in ([], vector("second text")[:4], vector("second text") + [0]):
            with self.subTest(length=len(returned)):
                self.service.override = {"second text": returned}
                with self.assertRaises(denembed.CanaryFailure) as refused:
                    denembed.verify(self.canary, self.service.url, lambda line: None)
                self.assertIn("1 of 2 cases (b)", str(refused.exception))

    def test_a_different_token_cap_refuses_before_embedding_anything(self):
        canary_for(self.service, self.canary)
        self.service.health = dict(self.service.health, max_tokens=512)
        with self.assertRaises(denembed.CanaryFailure) as refused:
            denembed.verify(self.canary, self.service.url, lambda line: None)
        self.assertIn("MAX_TOKENS=1024", str(refused.exception))
        self.assertFalse([r for r in self.service.requests if r[0] == "POST"])

    def test_an_edited_text_or_a_hand_edited_digest_refuses(self):
        doc = canary_for(self.service, self.canary)
        for broken, expected in ((dict(doc, cases=[dict(doc["cases"][0], text="edited"), doc["cases"][1]]),
                                  "texts were edited"),
                                 (dict(doc, spaceId="test-v1:" + "0" * 64), "hand-edited")):
            with open(self.canary, "w", encoding="utf-8") as fh:
                json.dump(broken, fh)
            with self.assertRaises(denembed.CanaryFailure) as refused:
                denembed.verify(self.canary, self.service.url, lambda line: None)
            self.assertIn(expected, str(refused.exception))

    def test_a_file_that_is_not_a_canary_says_so(self):
        """Not "den-embed could not be asked": the service was never reached, and the fix is the file."""
        doc = canary_for(self.service, self.canary)
        for body in ("{torn", json.dumps(dict(doc, cases=None)), json.dumps({k: v for k, v in doc.items() if k != "dims"}),
                     json.dumps(dict(doc, cases=[{k: v for k, v in doc["cases"][0].items() if k != "why"}]))):
            with self.subTest(body=body[:40]):
                with open(self.canary, "w", encoding="utf-8") as fh:
                    fh.write(body)
                with self.assertRaises(denembed.CanaryFailure) as refused:
                    denembed.verify(self.canary, self.service.url, lambda line: None)
                self.assertIn("not a readable embedding canary", str(refused.exception))
        self.assertEqual(self.service.requests, [])

    def test_a_vector_whose_base64_does_not_decode_strictly_is_refused(self):
        """`b64decode` without `validate` skips the stray `!` and decodes the rest into the right vector —
        which the Swift refused, and which a hand-mangled file should not get past."""
        rows = [{"id": "a", "why": "w", "text": "first text",
                 "v": base64.b64encode(bytes(x & 0xFF for x in vector("first text"))).decode()}]
        rows[0]["v"] = rows[0]["v"][:4] + "!" + rows[0]["v"][4:]
        doc = {"canarySet": "test-v1", "dims": 8, "cases": rows, "textsSha256": denembed.texts_sha256(rows),
               "spaceId": denembed.space_id("test-v1", rows)}
        with open(self.canary, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        with self.assertRaises(denembed.CanaryFailure) as refused:
            denembed.verify(self.canary, self.service.url, lambda line: None)
        self.assertIn("case a has an unreadable base64 vector", str(refused.exception))

    def test_a_missing_canary_refuses(self):
        with self.assertRaises(denembed.CanaryFailure):
            denembed.verify(os.path.join(self.directory.name, "absent.json"), self.service.url, print)


class Committed(unittest.TestCase):
    def test_the_committed_canarys_digests_are_the_ones_this_computes(self):
        """The same two digests `pipeline/embed_canary.py` computes, so the file one path regenerates is
        the file the other verifies."""
        with open(COMMITTED, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(denembed.texts_sha256(doc["cases"]), doc["textsSha256"])
        self.assertEqual(denembed.space_id(doc["canarySet"], doc["cases"]), doc["spaceId"])


if __name__ == "__main__":
    unittest.main()
