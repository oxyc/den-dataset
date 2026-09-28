import gzip
import importlib.util
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("more_like_cascade_full",
                                              os.path.join(HERE, "more_like_cascade_full.py"))
full = importlib.util.module_from_spec(spec)
spec.loader.exec_module(full)

#: Three anchors whose rows are each other plus eleven more films, so every anchor has a top ten and a tail.
TITLES = [f"movie:{i}" for i in range(1, 15)]
ANCHORS = ["movie:1", "movie:2", "movie:3"]


class Fake:
    """Answers every Noul 0.8, bills 10 tokens a call, and counts the calls."""

    def __init__(self):
        self.calls = 0

    def ask_with_metadata(self, state, questions):
        self.calls += 1
        return ({label: {"type": "noul", "noul": 0.8} for label in questions},
                {"model": full.MODEL, "usage": {"input_tokens": 10, "output_tokens": 0}})


class FullRunTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.pool = os.path.join(self.dir, "pool.json.gz")
        rows = {anchor: [key for key in TITLES if key != anchor] for anchor in ANCHORS}
        popularity = {key: {"rank": i, "typeSize": len(TITLES), "title": f"T{i}", "year": 2000}
                      for i, key in enumerate(TITLES, 1)}
        with gzip.open(self.pool, "wt") as fh:
            json.dump({"datasetVersion": "test", "storeSha256": "x", "popularity": popularity, "rows": rows}, fh)
        self.articles = os.path.join(self.dir, "articles.jsonl")
        with open(self.articles, "w") as fh:
            for key in TITLES:
                kind, tmdb = key.split(":")
                fh.write(json.dumps({"mediaType": kind, "tmdbId": int(tmdb), "title": key, "year": 2000,
                                     "text": f"The lead of {key}.", "plotSections": []}) + "\n")
        self.work = os.path.join(self.dir, "work")
        self.pilot = os.path.join(self.dir, "pilot")
        os.makedirs(self.pilot)

    def prepare(self, cap=1.0):
        full.prepare(self.pool, self.articles, self.dir, self.work, cap)

    def run_(self, client, **kwargs):
        with open(os.devnull, "w") as log:
            return full.run(self.work, self.pool, self.pilot, spend=True, client=client, log=log, **kwargs)

    def test_every_anchor_is_screened_then_weighed_and_exported_in_atlas_order(self):
        self.prepare()
        fake = Fake()
        summary = self.run_(fake)
        self.assertEqual((summary["weighed"], summary["screened"], fake.calls), (3, 3, 6))
        self.assertIsNone(summary["stopped"])
        out = os.path.join(self.dir, "jev.json")
        full.export(self.work, self.pool, out)
        with open(out) as fh:
            anchors = json.load(fh)["anchors"]
        # Atlas's top ten for movie:1, in its order; the screen's 0.8 against a 0.8 floor promotes nobody.
        self.assertEqual([key for key, _ in anchors["movie:1"]], TITLES[1:11])
        self.assertTrue(all(score == 0.8 for _, score in anchors["movie:1"]))

    def test_a_rerun_resumes_and_pays_for_nothing_answered(self):
        self.prepare()
        self.run_(Fake())
        again = Fake()
        self.assertEqual(self.run_(again)["weighed"], 3)
        self.assertEqual(again.calls, 0)

    def test_a_pilot_answer_for_the_same_state_is_reused_and_costs_nothing_new(self):
        self.prepare()
        frozen = full.Frozen(self.work, self.pool)
        screen = frozen.screen_row("movie:1")
        with open(os.path.join(self.pilot, "screen-answers.jsonl"), "w") as fh:
            fh.write(json.dumps({"anchor": "movie:1", "stateSha256": screen["stateSha256"],
                                 "questionsSha256": screen["questionsSha256"], "model": full.MODEL,
                                 "scores": {label: 0.1 for label in screen["labels"]},
                                 "usage": {"input_tokens": 999}}) + "\n")
        fake = Fake()
        summary = self.run_(fake)
        self.assertEqual(summary["reused"]["screen"], 1)
        self.assertEqual(fake.calls, 5)
        self.assertEqual(summary["newInputTokens"], 50)

    def test_the_cap_stops_between_anchors_and_never_leaves_a_screen_without_its_evidence(self):
        self.prepare(cap=0.0)
        fake = Fake()
        summary = self.run_(fake)
        self.assertEqual((summary["weighed"], summary["screened"], fake.calls), (0, 0, 0))
        self.assertIn("cap", summary["stopped"])


if __name__ == "__main__":
    unittest.main()
