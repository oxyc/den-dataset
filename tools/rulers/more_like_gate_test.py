import importlib.util
import json
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location("more_like_gate", os.path.join(HERE, "more_like_gate.py"))
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


class FakeJev:
    def __init__(self):
        self.input_tokens = 0
        self.spend = 0.0
        self.calls = 0

    def ask_with_metadata(self, state, questions):
        self.calls += 1
        answers = {}
        positive_label = next(label for label in ("a", "b")
                              if int(state["works"][label]["key"].split(":")[1]) % 3 == 2)
        for key, question in questions.items():
            high = key.startswith(positive_label)
            if question["type"] == "noul":
                answers[key] = {"type": "noul", "noul": 0.9 if high else 0.1}
            else:
                choice = "strong" if high else "reject"
                answers[key] = {"type": "choice", "choice": choice, "confidence": 0.9,
                                "probabilities": {name: 1.0 if name == choice else 0.0
                                                  for name in gate.VERDICTS}}
        self.input_tokens += 100
        self.spend += 0.00001
        return answers, {"model": gate.MODEL, "usage": {"input_tokens": 100, "output_tokens": 10}}


class MoreLikeGateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ruler = os.path.join(self.temp.name, "ruler.json")
        self.articles = os.path.join(self.temp.name, "articles.jsonl")
        self.work = os.path.join(self.temp.name, "work")
        cases = []
        article_rows = []
        for index in range(6):
            roles = {}
            for offset, role in enumerate(("anchor", "positive", "negative")):
                tmdb = index * 3 + offset + 1
                roles[role] = {"key": f"movie:{tmdb}", "title": f"Title {tmdb}", "year": 2000 + index}
                article_rows.append({"mediaType": "movie", "tmdbId": tmdb, "title": f"Title {tmdb}",
                                     "year": 2000 + index, "article": f"Title {tmdb} (film)",
                                     "text": f"Lead {tmdb}.\n== Plot ==\nStory {tmdb}.",
                                     "plotSections": ["Plot"]})
            cases.append({"pairId": f"pair-{index}", **roles,
                          "titleYear": {"positive": 0.8, "negative": 0.2},
                          "plot": {"positive": 0.7, "negative": 0.3},
                          "controls": {"yearGapEqual": True, "seedGenreEqual": True,
                                       "popularityLogGap": 0.02}})
        with open(self.ruler, "w", encoding="utf-8") as fh:
            json.dump({"schema": gate.RULER_SCHEMA, "sourceComment": gate.SOURCE_COMMENT,
                       "cases": cases}, fh)
        with open(self.articles, "w", encoding="utf-8") as fh:
            for row in article_rows:
                fh.write(json.dumps(row) + "\n")

    def tearDown(self):
        self.temp.cleanup()

    def test_prepare_is_deterministic_and_run_without_spend_calls_nothing(self):
        # MovieLens and the catalogue legitimately spell some titles differently; the title-only arm keeps
        # the former, while article evidence must expose the current canonical catalogue identity.
        with open(self.ruler, encoding="utf-8") as fh:
            ruler = json.load(fh)
        ruler["cases"][0]["anchor"]["title"] = "Title 1, The"
        with open(self.ruler, "w", encoding="utf-8") as fh:
            json.dump(ruler, fh)
        articles, _ = gate.load_articles(self.articles)
        state, _ = gate.build_state(ruler["cases"][0], articles)
        self.assertEqual(state["works"]["anchor"]["title"], "Title 1")
        first = gate.prepare(self.ruler, self.articles, self.work, sample_size=4, minimum_population=6)
        before = gate.file_digest(os.path.join(self.work, "worklist.jsonl"))
        second = gate.prepare(self.ruler, self.articles, self.work, sample_size=4, minimum_population=6)
        self.assertEqual(first, second)
        self.assertEqual(before, gate.file_digest(os.path.join(self.work, "worklist.jsonl")))
        plan = gate.run(self.work, os.path.join(self.temp.name, "answers.jsonl"))
        self.assertEqual(plan["callsPlanned"], 4)
        self.assertFalse(os.path.exists(os.path.join(self.temp.name, "answers.jsonl")))
        _, rows = gate.load_plan(self.work)
        self.assertNotIn("positive", rows[0]["state"]["works"])
        self.assertEqual(set(rows[0]["state"]["works"]), {"anchor", "a", "b"})

    def test_fake_provider_resume_and_score(self):
        gate.prepare(self.ruler, self.articles, self.work, sample_size=4, minimum_population=6)
        out = os.path.join(self.temp.name, "answers.jsonl")
        result = gate.run(self.work, out, spend=True, client=FakeJev(), workers=2)
        self.assertEqual(result["written"], 4)
        self.assertEqual(gate.run(self.work, out)["callsPlanned"], 0)
        report = gate.score(self.work, out)
        self.assertEqual(report["articleAuc"], 1.0)
        self.assertFalse(report["passed"], "the article arm ties the already-perfect fake prior")

    def test_refuses_a_broken_9d_control(self):
        with open(self.ruler, encoding="utf-8") as fh:
            blob = json.load(fh)
        blob["cases"][0]["controls"]["yearGapEqual"] = False
        with open(self.ruler, "w", encoding="utf-8") as fh:
            json.dump(blob, fh)
        with self.assertRaisesRegex(ValueError, "year and genre"):
            gate.prepare(self.ruler, self.articles, self.work, sample_size=4, minimum_population=6)

    def test_old_article_dump_requires_enriched_metadata(self):
        rows = list(gate.read_jsonl(self.articles))
        for row in rows:
            row.pop("plotSections")
        with open(self.articles, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        with self.assertRaisesRegex(SystemExit, "pass --enriched-dir"):
            gate.prepare(self.ruler, self.articles, self.work, sample_size=4, minimum_population=6)

    def test_old_article_dump_backfills_exact_extractor_evidence(self):
        rows = list(gate.read_jsonl(self.articles))
        enriched = os.path.join(self.temp.name, "enriched")
        os.mkdir(enriched)
        batch = []
        for row in rows:
            row.pop("plotSections")
            batch.append({"mediaType": row["mediaType"], "tmdbId": row["tmdbId"],
                          "year": row["year"], "plotArticle": row["article"], "plotRevId": 123,
                          "plotSections": ["Plot"]})
        with open(self.articles, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        with open(os.path.join(enriched, "batch-000.json"), "w", encoding="utf-8") as fh:
            json.dump(batch, fh)

        registration = gate.prepare(self.ruler, self.articles, self.work, sample_size=4,
                                    minimum_population=6, enriched_dir=enriched)
        self.assertRegex(registration["evidenceMetadataSha256"], r"^[0-9a-f]{64}$")
        _, planned = gate.load_plan(self.work)
        for row in planned:
            for work in row["state"]["works"].values():
                self.assertIn("Story ", work["articleEvidence"])

    def test_old_article_dump_refuses_stale_enriched_article_identity(self):
        rows = list(gate.read_jsonl(self.articles))
        enriched = os.path.join(self.temp.name, "enriched")
        os.mkdir(enriched)
        batch = []
        for row in rows:
            row.pop("plotSections")
            batch.append({"mediaType": row["mediaType"], "tmdbId": row["tmdbId"],
                          "year": row["year"], "plotArticle": row["article"], "plotRevId": 123,
                          "plotSections": ["Plot"]})
        for item in batch:
            item["plotArticle"] = "Different article"
        with open(self.articles, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        with open(os.path.join(enriched, "batch-000.json"), "w", encoding="utf-8") as fh:
            json.dump(batch, fh)

        with self.assertRaisesRegex(SystemExit, "article differs"):
            gate.prepare(self.ruler, self.articles, self.work, sample_size=4, minimum_population=6,
                         enriched_dir=enriched)

    def test_spend_cap_is_reserved_before_a_provider_call(self):
        gate.prepare(self.ruler, self.articles, self.work, sample_size=4, minimum_population=6)
        _, rows = gate.load_plan(self.work)
        ceiling = gate.request_token_upper_bound(rows[0]["state"], gate.questions())
        client = FakeJev()
        with self.assertRaisesRegex(RuntimeError, "refusing the next request"):
            gate.run(self.work, os.path.join(self.temp.name, "answers.jsonl"), spend=True, client=client,
                     max_spend=(ceiling - 1) * gate.TypeSafe.RATE_PER_INPUT_TOKEN)
        self.assertEqual(client.calls, 0)


if __name__ == "__main__":
    unittest.main()
