import importlib.util
import json
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location("more_like_prior", os.path.join(HERE, "more_like_prior.py"))
prior = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prior)


class FakeJev:
    def __init__(self):
        self.input_tokens = 0
        self.calls = 0

    @property
    def spend(self):
        return self.input_tokens * prior.TypeSafe.RATE_PER_INPUT_TOKEN

    def ask_with_metadata(self, state, questions):
        self.calls += 1
        self.input_tokens += 100
        score = 0.8 if int(state["pair"]["b"]["title"].split()[-1]) % 3 == 2 else 0.2
        return {"co_rating": {"type": "noul", "noul": score}}, {
            "model": prior.MODEL, "usage": {"input_tokens": 100, "output_tokens": 1}}


class MoreLikePriorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.source = os.path.join(self.temp.name, "ruler.json")
        self.work = os.path.join(self.temp.name, "work")
        cases = []
        for index in range(6):
            roles = {}
            for offset, role in enumerate(("anchor", "positive", "negative")):
                tmdb = index * 3 + offset + 1
                roles[role] = {"key": f"movie:{tmdb}", "title": f"Title {tmdb}", "year": 2000 + index}
            cases.append({"pairId": f"pair-{index}", **roles, "titleYear": None,
                          "plot": {"positive": 0.7, "negative": 0.3},
                          "controls": {"yearGapEqual": True, "seedGenreEqual": True,
                                       "popularityLogGap": 0.02}})
        with open(self.source, "w", encoding="utf-8") as fh:
            json.dump({"schema": prior.RULER_SCHEMA, "sourceComment": prior.SOURCE_COMMENT,
                       "cases": cases}, fh)

    def tearDown(self):
        self.temp.cleanup()

    def test_preregister_fake_run_resume_and_merge(self):
        manifest = prior.prepare(self.source, self.work, sample_size=4, minimum_population=6)
        self.assertEqual(manifest["calls"], 8)
        out = os.path.join(self.temp.name, "answers.jsonl")
        result = prior.run(self.work, out, spend=True, cap=1, workers=2, client=FakeJev())
        self.assertEqual(result["written"], 8)
        self.assertEqual(prior.run(self.work, out)["callsPlanned"], 0)
        merged = os.path.join(self.temp.name, "merged.json")
        report = prior.merge(self.source, self.work, out, merged)
        self.assertEqual(report["casesWithPrior"], 4)
        with open(merged, encoding="utf-8") as fh:
            cases = json.load(fh)["cases"]
        self.assertEqual(sum(case["titleYear"] is not None for case in cases), 4)

    def test_whole_run_must_fit_cap(self):
        prior.prepare(self.source, self.work, sample_size=4, minimum_population=6)
        client = FakeJev()
        with self.assertRaisesRegex(RuntimeError, "whole remaining prior run"):
            prior.run(self.work, os.path.join(self.temp.name, "answers.jsonl"), spend=True,
                      cap=0.000001, client=client)
        self.assertEqual(client.calls, 0)


if __name__ == "__main__":
    unittest.main()
