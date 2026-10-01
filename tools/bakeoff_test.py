"""`tools/bakeoff.py`: three models over one premise worklist in one command, offline, each answering for
itself — one model's refusal is its own, never passed to a fallback — and the table it writes."""
import argparse
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bakeoff  # noqa: E402
from lib import llm, llm_providers as providers  # noqa: E402

TAGS = ["identity-swap", "hidden-heir", "family-secret", "reluctant-alliance",
        "betrayal-revenge", "class-barrier", "secret-parentage", "race-against-time"]


class Fake:
    def __init__(self, refuses):
        self.refuses = refuses

    def online(self, cfg, request):
        rows = json.loads(request["prompt"].split("\n", 1)[1])
        if cfg["model"] in self.refuses:
            return providers.answer("", refused=True)
        return providers.answer(json.dumps({"rows": [{"key": r["key"], "tags": TAGS} for r in rows]}),
                                usage={"inputTokens": 1000, "cachedTokens": 0, "outputTokens": 100,
                                       "reasoningTokens": 0})


class Bakeoff(unittest.TestCase):
    def test_three_models_are_one_command_and_one_table(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "gen", "in"))
            with open(os.path.join(root, "gen", "in", "batch-0000.json"), "w") as fh:
                json.dump([{"key": "movie:7", "mediaType": "movie", "tmdbId": 7,
                            "plot": "Two strangers exchange identities."}], fh)
            with open(os.path.join(root, "gen", "manifest.json"), "w") as fh:
                json.dump({"batches": 1}, fh)
            prices = {m: [("2026-01-01", 1e-6, 2e-6, 1e-6)] for m in ("a", "b", "c")}
            with mock.patch.dict(providers.PROVIDERS, {"fake": Fake({"c"})}), \
                    mock.patch.dict(llm.PRICES, prices):
                table = bakeoff.run(argparse.Namespace(
                    step="premise_tags", phase=os.path.join(root, "gen"), models=["fake:a", "fake:b", "fake:c"],
                    out=os.path.join(root, "out"), max_spend_usd=1.0, per_day=5, workers=1), log=io.StringIO())
            with open(os.path.join(root, "out", "table.md")) as fh:
                written = fh.read()
            files = sorted(os.listdir(os.path.join(root, "out")))
        self.assertEqual([(row["model"], row["answered"], row["refused"]) for row in table],
                         [("fake:a", 1, 0), ("fake:b", 1, 0), ("fake:c", 0, 1)])
        self.assertAlmostEqual(table[0]["perTitleUSD"], 1000 * 1e-6 + 100 * 2e-6)
        self.assertEqual(table[0]["tagsPerTitle"], 8)
        self.assertIn("| fake:c | 1 | 0 | 1 |", written)
        self.assertEqual(files, ["fake-a.json", "fake-b.json", "fake-c.json", "table.md"])


if __name__ == "__main__":
    unittest.main()
