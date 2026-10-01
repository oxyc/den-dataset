import json
import os
import tempfile
import unittest
from unittest import mock

from store import vector_blob

from . import artifacts, premise_daily


TAGS = ["identity-swap", "hidden-heir", "family-secret", "reluctant-alliance",
        "betrayal-revenge", "class-barrier", "secret-parentage", "race-against-time"]


class Response:
    def __init__(self, rows, input_tokens=100, output_tokens=40):
        self.payload = json.dumps({"content": [{"type": "text", "text": json.dumps(rows)}],
                                   "usage": {"input_tokens": input_tokens,
                                             "output_tokens": output_tokens}}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


class PremiseGeneration(unittest.TestCase):
    def phase(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        os.makedirs(os.path.join(root, "in"))
        row = {"key": "movie:7", "mediaType": "movie", "tmdbId": 7,
               "plot": "Two strangers exchange identities and uncover a family secret."}
        with open(os.path.join(root, "in", "batch-0000.json"), "w", encoding="utf-8") as handle:
            json.dump([row], handle)
        with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8") as handle:
            json.dump({"titles": 1, "batches": 1, "estimatedInputTokens": 100,
                       "estimatedOutputTokens": 100}, handle)
        return root

    def test_exact_key_batch_is_written_and_costed(self):
        phase = self.phase()
        result = premise_daily.generate(phase, "not-logged", 1.0,
                                        opener=lambda *_a, **_k: Response([{"key": "movie:7", "tags": TAGS}]))
        self.assertEqual((result["generated"], result["titles"]), (1, 1))
        self.assertAlmostEqual(result["costUSD"], .0003)
        with open(os.path.join(phase, "out", "batch-0000.json"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)[0]["tags"], TAGS)

    def test_foreign_key_refuses_without_a_checkpoint(self):
        phase = self.phase()
        with self.assertRaisesRegex(premise_daily.GenerationError, "invented keys") as caught:
            premise_daily.generate(phase, "not-logged", 1.0,
                                   opener=lambda *_a, **_k: Response([{"key": "movie:8", "tags": TAGS}]))
        self.assertAlmostEqual(caught.exception.cost_usd, .0003)
        self.assertFalse(os.path.exists(os.path.join(phase, "out", "batch-0000.json")))

    def test_a_row_below_the_floor_after_drops_is_repaired_alone(self):
        phase = self.phase()
        answers = iter((Response([{"key": "movie:7", "tags": TAGS[:6] + ["comedy", "character-arc"]}]),
                        Response([{"key": "movie:7", "tags": TAGS}])))
        result = premise_daily.generate(phase, "not-logged", 1.0,
                                        opener=lambda *_a, **_k: next(answers))
        self.assertEqual(result["repairedRows"], 1)
        with open(os.path.join(phase, "out", "batch-0000.json"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)[0]["tags"], TAGS)


class Ctx:
    """The two artifacts and the out-dir the correction step touches, in a temporary directory."""

    def __init__(self, root):
        self.out_dir = root

    def path(self, artifact):
        return os.path.join(self.out_dir, artifact.filename)


class CommittedCorrections(unittest.TestCase):
    """A run seeded from the bundle takes the committed rows that differ, and re-embeds only those (#184)."""

    DIMS = 4

    def setUp(self):
        self.root = self.enterContext(tempfile.TemporaryDirectory())
        self.ctx = Ctx(os.path.join(self.root, "out"))
        os.makedirs(os.path.join(self.root, "repo", "data"))
        os.makedirs(self.ctx.out_dir)
        self.enterContext(mock.patch.object(premise_daily, "REPO", os.path.join(self.root, "repo")))
        self.enterContext(mock.patch.object(premise_daily.embed_canary, "gate", lambda _url: None))
        self.embedded = []

        def embed(_url, texts):
            self.embedded.extend(texts)
            return [[len(text) % 100] * self.DIMS for text in texts]
        self.enterContext(mock.patch.object(premise_daily, "_embed", embed))

    def write_tags(self, committed, run):
        with open(os.path.join(self.root, "repo", "data", "premise-tags-v2.json"), "w", encoding="utf-8") as fh:
            json.dump({"count": len(committed), "tags": committed}, fh)
        with open(self.ctx.path(artifacts.PREMISE_TAGS), "w", encoding="utf-8") as fh:
            json.dump({"count": len(run), "dailyIncrements": [{"titles": 1}], "tags": run}, fh)

    def write_blob(self, keys):
        rows = b"".join(bytes([row + 1] * self.DIMS) for row in range(len(keys)))
        vector_blob.write(self.ctx.path(artifacts.PREMISE_VECTORS), keys, rows, self.DIMS)

    def blob(self):
        _count, _dims, keys, blob, base = vector_blob.read(self.ctx.path(artifacts.PREMISE_VECTORS))
        return keys, [blob[base + row * self.DIMS:base + (row + 1) * self.DIMS] for row in range(len(keys))]

    def test_only_shared_keys_whose_rows_differ_are_corrections(self):
        self.write_tags({"movie:1": ["a"], "movie:2": ["fixed-b"], "movie:3": ["committed-only"]},
                        {"movie:1": ["a"], "movie:2": ["wrong-b"], "tv:9": ["daily-title"]})
        self.assertEqual(premise_daily.committed_corrections(self.ctx), ["movie:2"])

    def test_no_run_copy_means_nothing_to_correct(self):
        self.write_tags({"movie:1": ["a"]}, {})
        os.remove(self.ctx.path(artifacts.PREMISE_TAGS))
        self.assertEqual(premise_daily.committed_corrections(self.ctx), [])

    def test_corrections_rewrite_those_rows_and_reembed_them_in_place(self):
        self.write_tags({"movie:1": ["a"], "movie:2": ["fixed-b"], "movie:3": ["fixed-c"]},
                        {"movie:1": ["a"], "movie:2": ["wrong-b"], "movie:3": ["wrong-c"], "tv:9": ["daily"]})
        self.write_blob(["movie:1", "movie:2", "movie:3", "tv:9"])
        before_keys, before = self.blob()
        keys = premise_daily.committed_corrections(self.ctx)
        result = premise_daily.apply_corrections(self.ctx, "http://embed.invalid", keys)

        self.assertEqual((result["corrected"], result["reembedded"], result["appended"]), (2, 2, 0))
        self.assertEqual(self.embedded, ["fixed-b", "fixed-c"])
        after_keys, after = self.blob()
        self.assertEqual(after_keys, before_keys, "re-embedding must not reorder or add rows")
        self.assertEqual([after[0], after[3]], [before[0], before[3]], "untouched rows keep their bytes")
        self.assertEqual(after[1], bytes([len("fixed-b")] * self.DIMS))
        with open(self.ctx.path(artifacts.PREMISE_TAGS), encoding="utf-8") as fh:
            run = json.load(fh)
        self.assertEqual(run["tags"], {"movie:1": ["a"], "movie:2": ["fixed-b"], "movie:3": ["fixed-c"],
                                       "tv:9": ["daily"]})
        self.assertEqual(run["dailyIncrements"], [{"titles": 1}], "the run copy's own record survives")
        self.assertEqual(premise_daily.committed_corrections(self.ctx), [], "a second pass finds nothing")

    def test_a_correction_never_embeds_an_unrelated_missing_key(self):
        # The live blob lacks three tagged titles; a correction-only run must leave them to the premise step.
        self.write_tags({"movie:2": ["fixed-b"]}, {"movie:1": ["a"], "movie:2": ["wrong-b"], "movie:5": ["no-row"]})
        self.write_blob(["movie:1", "movie:2"])
        result = premise_daily.apply_corrections(self.ctx, "http://embed.invalid", ["movie:2"])
        self.assertEqual((result["reembedded"], result["appended"]), (1, 0))
        self.assertEqual(self.embedded, ["fixed-b"])
        self.assertEqual(self.blob()[0], ["movie:1", "movie:2"])


if __name__ == "__main__":
    unittest.main()
