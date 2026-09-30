import json
import os
import tempfile
import unittest

from . import premise_daily


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


if __name__ == "__main__":
    unittest.main()
