"""The canary is a hard gate on every vector this repo writes, so it is tested against a stub service.

A gate that silently passed would be indistinguishable from a healthy run — which is the failure mode the
canary exists to remove, and the one it could most easily reproduce.
"""
import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import embed_canary  # noqa: E402

HEALTH = {"status": "ok", "model": "bge-m3", "dims": 1024, "vector_epoch": 1,
          "runtime": "den-embed/5.1.2", "max_tokens": 1024}


class Service:
    """A den-embed that answers with whatever the test says it answers with.

    Installed over `embed_canary.request`, which is the single seam every call goes through — so a test
    cannot accidentally reach a real service and a real run cannot accidentally reach this.
    """

    def __init__(self, answers, health=None):
        self.answers = answers          # text -> int8 list
        self.health = health or HEALTH
        self.embedded = []

    def __call__(self, url, path, payload=None, **kwargs):
        if path == "/health":
            return self.health
        text = payload["texts"][0]
        self.embedded.append(text)
        return {"vectors": [self.answers[text]]}


class CanaryTests(unittest.TestCase):
    def setUp(self):
        self.doc = embed_canary.load(embed_canary.DEFAULT_CANARY)
        self.truth = {c["text"]: embed_canary.decode_vector(c["v"]) for c in self.doc["cases"]}
        self._request = embed_canary.request
        self.addCleanup(lambda: setattr(embed_canary, "request", self._request))

    def install(self, service):
        embed_canary.request = service
        return service

    # --- the committed file ------------------------------------------------

    def test_the_committed_file_is_self_consistent(self):
        """A hand-edited canary — a pasted vector, a digest corrected by hand — must not be publishable as
        the name of a space."""
        self.assertEqual(embed_canary.texts_sha256(self.doc), self.doc["textsSha256"])
        self.assertEqual(embed_canary.space_id(self.doc["canarySet"], self.doc["cases"]),
                         self.doc["spaceId"])
        self.assertTrue(self.doc["spaceId"].startswith(self.doc["canarySet"] + ":"))
        for case in self.doc["cases"]:
            self.assertEqual(len(embed_canary.decode_vector(case["v"])), self.doc["dims"], case["id"])

    def test_the_cases_exercise_what_varies(self):
        """The set is small enough to read, so what it covers is worth pinning: a query-length text, a
        non-Latin one, one in the corpus's own document shape, and one past the token cap."""
        ids = [c["id"] for c in self.doc["cases"]]
        for required in ("one-sentence", "cjk", "corpus-shaped", "past-token-cap"):
            self.assertIn(required, ids)
        longest = max(len(c["text"]) for c in self.doc["cases"])
        self.assertGreater(longest, 4000, "no case is long enough to reach the token cap")

    # --- encoding ----------------------------------------------------------

    def test_int8_round_trips_including_the_negative_end(self):
        values = [-128, -127, -1, 0, 1, 126, 127]
        self.assertEqual(embed_canary.decode_vector(embed_canary.encode_vector(values)), values)

    # --- the identity ------------------------------------------------------

    def test_a_changed_vector_byte_renames_the_space(self):
        edited = copy.deepcopy(self.doc)
        v = embed_canary.decode_vector(edited["cases"][0]["v"])
        v[0] = v[0] + 1 if v[0] < 127 else 0
        edited["cases"][0]["v"] = embed_canary.encode_vector(v)
        self.assertNotEqual(embed_canary.space_id(edited["canarySet"], edited["cases"]),
                            self.doc["spaceId"])

    def test_a_changed_text_moves_only_the_text_digest(self):
        """The texts are the instrument; `canarySet` names the instrument. A reworded probe must not
        rename a space it did not change — but it must not go unnoticed either."""
        edited = copy.deepcopy(self.doc)
        edited["cases"][0]["text"] += " and one more clause"
        self.assertEqual(embed_canary.space_id(edited["canarySet"], edited["cases"]), self.doc["spaceId"])
        self.assertNotEqual(embed_canary.texts_sha256(edited), self.doc["textsSha256"])

    def test_renaming_the_set_renames_the_space(self):
        self.assertNotEqual(embed_canary.space_id("canary-v2", self.doc["cases"]), self.doc["spaceId"])

    # --- verify ------------------------------------------------------------

    def test_the_right_answers_pass(self):
        service = self.install(Service(self.truth))
        ok, report = embed_canary.verify(self.doc, "http://stub")
        self.assertTrue(ok, report)
        self.assertEqual(len(service.embedded), len(self.doc["cases"]))
        self.assertTrue(all(c["ok"] for c in report["cases"]))

    def test_one_dimension_off_by_one_fails(self):
        """Byte-identical is the bar. This is the smallest possible difference, it rounds to cosine
        1.000000, and it still fails — which is why cosine is reported and never consulted."""
        answers = dict(self.truth)
        text = self.doc["cases"][0]["text"]
        bumped = list(answers[text])
        bumped[7] = bumped[7] + 1 if bumped[7] < 127 else bumped[7] - 1
        answers[text] = bumped

        self.install(Service(answers))
        ok, report = embed_canary.verify(self.doc, "http://stub")
        self.assertFalse(ok)
        failed = [c for c in report["cases"] if not c["ok"]]
        self.assertEqual([c["id"] for c in failed], [self.doc["cases"][0]["id"]])
        self.assertEqual(failed[0]["dimsDiffering"], 1)
        self.assertEqual(failed[0]["maxAbsDelta"], 1)
        self.assertGreaterEqual(failed[0]["cosine"], 0.9999)
        self.assertIn("different arithmetic", embed_canary.verdict(report))

    def test_a_different_space_is_named_as_one(self):
        """Every case answered by a different generation. The verdict has to distinguish this from the
        off-by-one above, because the two send a reader to entirely different places."""
        answers = {t: [(x * 3 + 11) % 127 - 63 for x in v] for t, v in self.truth.items()}
        self.install(Service(answers))
        ok, report = embed_canary.verify(self.doc, "http://stub")
        self.assertFalse(ok)
        self.assertTrue(all(not c["ok"] for c in report["cases"]))
        self.assertIn("different embedding space", embed_canary.verdict(report))

    def test_a_different_token_cap_refuses_before_embedding_anything(self):
        """At a different cap the long cases embed a different text, so their bytes cannot mean anything.
        Failing on exactly those reads as a mysterious partial failure rather than as a setting."""
        service = self.install(Service(self.truth, health=dict(HEALTH, max_tokens=512)))
        ok, report = embed_canary.verify(self.doc, "http://stub")
        self.assertFalse(ok)
        self.assertEqual(service.embedded, [], "nothing should be embedded once the cap disagrees")
        self.assertIn("truncates at 512 tokens", embed_canary.verdict(report))

    def test_a_text_edited_without_regenerating_is_named_as_that(self):
        edited = copy.deepcopy(self.doc)
        edited["cases"][0]["text"] += " and one more clause"
        service = self.install(Service(self.truth))
        ok, report = embed_canary.verify(edited, "http://stub")
        self.assertFalse(ok)
        self.assertEqual(service.embedded, [])
        self.assertIn("edited without regenerating", embed_canary.verdict(report))

    def test_a_hand_corrected_space_id_is_caught(self):
        edited = copy.deepcopy(self.doc)
        edited["spaceId"] = "canary-v1:" + "0" * 64
        self.install(Service(self.truth))
        ok, report = embed_canary.verify(edited, "http://stub")
        self.assertFalse(ok)
        self.assertIn("hand-edited", embed_canary.verdict(report))

    def test_a_service_reporting_other_dimensions_is_not_a_drift(self):
        answers = {t: v[:384] for t, v in self.truth.items()}
        self.install(Service(answers))
        ok, report = embed_canary.verify(self.doc, "http://stub")
        self.assertFalse(ok)
        self.assertIn("different number of dimensions", embed_canary.verdict(report))

    # --- the gate ----------------------------------------------------------

    def test_the_gate_exits_rather_than_returning_on_failure(self):
        """`gate` is what the writers call. It has no tolerance argument and no way to downgrade a failure
        to a warning, because a caller that wanted one would be asking to publish a corpus in a space it
        cannot name."""
        answers = {t: [0] * len(v) for t, v in self.truth.items()}
        self.install(Service(answers))
        with open(os.devnull, "w", encoding="utf-8") as sink:
            with self.assertRaises(SystemExit):
                embed_canary.gate("http://stub", out=sink)

    def test_the_gate_records_the_verified_space(self):
        import tempfile
        self.install(Service(self.truth))
        with tempfile.TemporaryDirectory() as tmp:
            record = os.path.join(tmp, "index", "embedding-space.json")
            with open(os.devnull, "w", encoding="utf-8") as sink:
                space = embed_canary.gate("http://stub", record=record, out=sink)
            self.assertEqual(space, self.doc["spaceId"])
            with open(record, encoding="utf-8") as fh:
                stamp = json.load(fh)
            self.assertEqual(stamp["spaceId"], self.doc["spaceId"])
            self.assertEqual(stamp["canarySet"], self.doc["canarySet"])


if __name__ == "__main__":
    unittest.main()
