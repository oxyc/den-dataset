#!/usr/bin/env python3
"""Offline contract tests for the Jev facet prompt and append-only runner."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import facet_questions  # noqa: E402
import run_facets        # noqa: E402


class FacetPromptTests(unittest.TestCase):
    def test_v2_load_bearing_rules_reach_the_api_questions(self):
        path = os.path.join(os.path.dirname(os.path.dirname(HERE)), "data", "prompts", "facets-v2.md")
        questions = facet_questions.questions(path)
        self.assertEqual(
            list(questions),
            ["era", "setting", "scope", "ending", "pacing", "chronology", "continuity",
             "timespan", "conflict", "ensemble", "tone", "archetype"],
        )
        self.assertTrue(questions["era"]["instructions"].startswith("Answer from the supplied article only"))
        self.assertIn("ignore whether it is episodic", questions["pacing"]["instructions"])
        self.assertIn("supporting characters", questions["ensemble"]["instructions"])
        self.assertIn("bounded fictional", questions["archetype"]["instructions"])
        self.assertIn("choose `framed` first", questions["chronology"]["instructions"])
        self.assertIn("required for documentaries", questions["archetype"]["criteria"]["does-not-apply"])
        self.assertIn("required when the article does not reveal tempo",
                      questions["pacing"]["criteria"]["does-not-apply"])
        self.assertIn("never the shape of the ending", questions["tone"]["instructions"])
        self.assertIn("never the work's overall tone", questions["ending"]["instructions"])
        self.assertEqual(
            set(run_facets.VALIDITY["validity"]["criteria"]),
            {"correct-screen-work", "source-work", "other-screen-work", "multi-work-overview",
             "season-or-episode", "not-a-work"},
        )


class AnswerValidationTests(unittest.TestCase):
    QUESTIONS = {
        "axis": {"type": "choice", "instructions": "pick", "criteria": {"a": None, "b": None}},
    }

    def test_accepts_exact_choice_distribution(self):
        run_facets.validate_answers({
            "axis": {"choice": "a", "confidence": 0.8, "probabilities": {"a": 0.8, "b": 0.2}},
        }, self.QUESTIONS)

    def test_rejects_missing_axis(self):
        with self.assertRaisesRegex(run_facets.TypeSafeError, "answer keys differ"):
            run_facets.validate_answers({}, self.QUESTIONS)

    def test_rejects_invented_choice(self):
        with self.assertRaisesRegex(run_facets.TypeSafeError, "invalid choice"):
            run_facets.validate_answers({
                "axis": {"choice": "c", "confidence": 0.8,
                         "probabilities": {"a": 0.8, "b": 0.2}},
            }, self.QUESTIONS)

    def test_rejects_incomplete_distribution(self):
        with self.assertRaisesRegex(run_facets.TypeSafeError, "probability keys differ"):
            run_facets.validate_answers({
                "axis": {"choice": "a", "confidence": 1.0, "probabilities": {"a": 1.0}},
            }, self.QUESTIONS)


class RunnerTests(unittest.TestCase):
    def write_jsonl(self, path, rows):
        with open(path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def fixture(self, directory):
        prompt = os.path.join(directory, "test.md")
        with open(prompt, "w", encoding="utf-8") as fh:
            fh.write("- **`axis`** — choose one:\n  `a` · `b`\n")
        articles = os.path.join(directory, "articles.jsonl")
        rec = {"mediaType": "movie", "tmdbId": 1, "title": "One", "article": "One",
               "language": "en", "chars": 12, "revId": 7, "text": "article text"}
        self.write_jsonl(articles, [rec])
        return prompt, articles, rec

    def resume_row(self, prompt, rec, **changes):
        questions = {**facet_questions.questions(prompt), **run_facets.VALIDITY}
        encoded = json.dumps(questions, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        row = {
            "mediaType": rec["mediaType"], "tmdbId": rec["tmdbId"],
            "questionSet": os.path.basename(prompt),
            "questionsSha256": run_facets.sha256_text(encoded), "model": run_facets.MODEL,
            "articleSha256": run_facets.sha256_text(rec["text"]),
            "articleRevId": rec["revId"],
            "stateSha256": run_facets.sha256_text(run_facets.state_for(rec)),
        }
        row.update(changes)
        return row

    def test_incomplete_response_exits_nonzero_and_is_not_written(self):
        class IncompleteClient:
            calls = input_tokens = 0
            spend = 0

            def __init__(self, model):
                self.model = model

            def ask(self, state, questions):
                self.calls += 1
                return {}

        with tempfile.TemporaryDirectory() as directory:
            prompt, articles, _ = self.fixture(directory)
            output = os.path.join(directory, "out.jsonl")
            with mock.patch.object(run_facets, "TypeSafe", IncompleteClient):
                code = run_facets.main(["--articles", articles, "--out", output,
                                        "--prompt", prompt, "--workers", "1"])
            self.assertEqual(code, 1)
            with open(output, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "")

    def test_state_names_the_requested_screen_work(self):
        state = run_facets.state_for({"mediaType": "movie", "title": "Time of EVE: The Movie",
                                      "year": 2010, "text": "Article."})
        self.assertEqual(state, "Requested target: film — Time of EVE: The Movie (2010)\n\n"
                                "Wikipedia article:\nArticle.")

    def test_written_row_carries_reproducibility_fields(self):
        class CompleteClient:
            calls = input_tokens = 0
            spend = 0

            def __init__(self, model):
                self.model = model

            def ask(self, state, questions):
                self.calls += 1
                answer = lambda choice, keys: {  # noqa: E731
                    "choice": choice, "confidence": 1.0,
                    "probabilities": {key: float(key == choice) for key in keys},
                }
                return {name: answer(next(iter(q["criteria"])), q["criteria"]) for name, q in questions.items()}

        with tempfile.TemporaryDirectory() as directory:
            prompt, articles, rec = self.fixture(directory)
            output = os.path.join(directory, "out.jsonl")
            with mock.patch.object(run_facets, "TypeSafe", CompleteClient):
                code = run_facets.main(["--articles", articles, "--out", output,
                                        "--prompt", prompt, "--workers", "1"])
            self.assertEqual(code, 0)
            with open(output, encoding="utf-8") as fh:
                row = json.load(fh)
            self.assertEqual(row["articleRevId"], 7)
            self.assertEqual(row["articleSha256"], run_facets.sha256_text(rec["text"]))
            self.assertEqual(row["stateSha256"], run_facets.sha256_text(run_facets.state_for(rec)))
            self.assertNotEqual(row["stateSha256"], row["articleSha256"])
            self.assertFalse(row["truncated"])
            self.assertRegex(row["questionsSha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(row["promptSha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(row["model"], run_facets.MODEL)
            self.assertRegex(row["runStartedAt"], r"^\d{4}-\d\d-\d\dT")

    def test_resume_rejects_legacy_rows_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            prompt, articles, _ = self.fixture(directory)
            output = os.path.join(directory, "out.jsonl")
            self.write_jsonl(output, [{"mediaType": "movie", "tmdbId": 1,
                                       "questionSet": "test.md"}])
            with self.assertRaisesRegex(SystemExit, "legacy rows"):
                run_facets.main(["--articles", articles, "--out", output, "--prompt", prompt])

    def test_resume_rejects_question_and_state_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            prompt, articles, rec = self.fixture(directory)
            output = os.path.join(directory, "out.jsonl")
            self.write_jsonl(output, [self.resume_row(prompt, rec, questionsSha256="0" * 64)])
            with self.assertRaisesRegex(SystemExit, "different questions or model"):
                run_facets.main(["--articles", articles, "--out", output, "--prompt", prompt])

            self.write_jsonl(output, [self.resume_row(prompt, rec, stateSha256="0" * 64)])
            with self.assertRaisesRegex(SystemExit, "different effective state"):
                run_facets.main(["--articles", articles, "--out", output, "--prompt", prompt])

            self.write_jsonl(output, [self.resume_row(prompt, rec)])
            with self.assertRaisesRegex(SystemExit, "different effective state"):
                run_facets.main(["--articles", articles, "--out", output, "--prompt", prompt,
                                 "--max-chars", "3"])

    def test_resume_rejects_changed_article_text_or_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            prompt, articles, rec = self.fixture(directory)
            output = os.path.join(directory, "out.jsonl")
            self.write_jsonl(output, [self.resume_row(prompt, rec)])

            self.write_jsonl(articles, [{**rec, "text": "changed text"}])
            with self.assertRaisesRegex(SystemExit, "different article text"):
                run_facets.main(["--articles", articles, "--out", output, "--prompt", prompt])

            self.write_jsonl(articles, [{**rec, "revId": 8}])
            with self.assertRaisesRegex(SystemExit, "different article revision"):
                run_facets.main(["--articles", articles, "--out", output, "--prompt", prompt])

    def test_resume_rejects_duplicate_and_extra_output_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            prompt, articles, rec = self.fixture(directory)
            output = os.path.join(directory, "out.jsonl")
            row = self.resume_row(prompt, rec)
            self.write_jsonl(output, [row, row])
            with self.assertRaisesRegex(SystemExit, "duplicate key"):
                run_facets.main(["--articles", articles, "--out", output, "--prompt", prompt])

            extra = self.resume_row(prompt, {**rec, "tmdbId": 2})
            self.write_jsonl(output, [extra])
            with self.assertRaisesRegex(SystemExit, "keys absent"):
                run_facets.main(["--articles", articles, "--out", output, "--prompt", prompt])

    def test_resume_rejects_malformed_output(self):
        with tempfile.TemporaryDirectory() as directory:
            prompt, articles, _ = self.fixture(directory)
            output = os.path.join(directory, "out.jsonl")
            with open(output, "w", encoding="utf-8") as fh:
                fh.write('{"torn":')
            with self.assertRaisesRegex(SystemExit, "malformed JSON"):
                run_facets.main(["--articles", articles, "--out", output, "--prompt", prompt])


if __name__ == "__main__":
    unittest.main()
