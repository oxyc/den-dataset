#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import article_sections
import combined_questions
import run_combined


def answer_for(question):
    kind = question["type"]
    if kind == "noul":
        return {"type": "noul", "noul": 0.8}
    if kind == "choice":
        keys = list(question["criteria"])
        return {
            "type": "choice", "choice": keys[0], "confidence": 1.0,
            "probabilities": {key: 1.0 if index == 0 else 0.0 for index, key in enumerate(keys)},
        }
    if kind == "score":
        return {
            "type": "score", "score": 0.0, "confidence": 1.0,
            "legend": {str(index): level for index, level in enumerate(question["criteria"])},
            "probabilities": {str(index): 1.0 if index == 0 else 0.0
                              for index in range(len(question["criteria"]))},
        }
    raise AssertionError(kind)


class FakeClient:
    def __init__(self):
        self.model = combined_questions.PINNED_MODEL
        self.calls = []

    def ask_with_metadata(self, state, questions):
        self.calls.append((state, questions))
        return ({key: answer_for(question) for key, question in questions.items()},
                {"model": combined_questions.PINNED_MODEL,
                 "usage": {"input_tokens": 10, "output_tokens": 5}})


class FailingClient:
    instances = []

    def __init__(self, model):
        self.model = model
        self.calls = 0
        self.input_tokens = self.output_tokens = 0
        self.lock = threading.Lock()
        self.__class__.instances.append(self)

    def ask_with_metadata(self, state, questions):
        with self.lock:
            self.calls += 1
        raise run_combined.TypeSafeError("HTTP 422: systemic contract mismatch")

    @property
    def spend(self):
        return 0


def record(text):
    article_headings = [section["heading"] for section in article_sections.parse_sections(text)[1:]]
    return {
        "mediaType": "movie", "tmdbId": 7, "title": "Example", "year": 2001,
        "article": "Example (film)", "language": "en", "revId": 99,
        "sections": article_headings, "plotSections": ["Plot"], "text": text,
    }


class QuestionTests(unittest.TestCase):
    def test_canonical_taxonomy_and_question_counts(self):
        questions, mapping, taxonomy = combined_questions.global_questions()
        self.assertEqual(taxonomy["version"], "t02")
        self.assertEqual(len(taxonomy["primaryGenres"]), 17)
        self.assertEqual(len(taxonomy["subgenres"]), 19)
        self.assertEqual(len(taxonomy["thematic"]), 40)
        self.assertEqual(len(taxonomy["regional"]), 13)
        self.assertEqual(len(taxonomy["moods"]), 16)
        self.assertEqual(len(mapping), 75)
        self.assertEqual(len(questions), 94)
        self.assertNotIn("tax__theme__nordic_noir", questions)
        self.assertEqual(questions["score__complexity"]["type"], "score")
        self.assertEqual(questions["tax__theme__heist"]["type"], "noul")

    def test_all_primitives_validate_and_corruption_fails(self):
        questions = {
            "yes": {"type": "noul", "instructions": "yes?"},
            "pick": {"type": "choice", "instructions": "pick", "criteria": {"a": "A", "b": "B"}},
            "rate": {"type": "score", "instructions": "rate", "criteria": ["low", "high"]},
        }
        answers = {key: answer_for(question) for key, question in questions.items()}
        run_combined.validate_answers(answers, questions)
        answers["rate"]["legend"]["1"] = "wrong"
        with self.assertRaisesRegex(Exception, "legend differs"):
            run_combined.validate_answers(answers, questions)

    def test_score_accepts_provider_display_rounding_but_not_real_disagreement(self):
        question = {
            "rate": {"type": "score", "instructions": "rate",
                     "criteria": ["zero", "one", "two", "three", "four"]},
        }
        answer = answer_for(question["rate"])
        answer.update({
            "score": 3.89, "confidence": 0.8,
            "probabilities": {"0": 0.0, "1": 0.0, "2": 0.0, "3": 0.08, "4": 0.92},
        })
        run_combined.validate_answers({"rate": answer}, question)
        answer["score"] = 3.7
        with self.assertRaisesRegex(Exception, "disagrees with distribution"):
            run_combined.validate_answers({"rate": answer}, question)


class SectionTests(unittest.TestCase):
    def test_stable_ids_duplicate_occurrences_spans_and_extractor_diff(self):
        text = "Lead text\n\n== Plot ==\nFirst.\n\n== Notes ==\nNo.\n\n== Plot ==\nSecond.\n"
        sections = article_sections.parse_sections(text, ["Plot"])
        self.assertEqual([s["id"] for s in sections], ["s000", "s001", "s002", "s003"])
        self.assertEqual([sections[1]["headingOccurrence"], sections[3]["headingOccurrence"]], [1, 2])
        self.assertTrue(sections[1]["extractorSelected"])
        self.assertFalse(sections[3]["extractorSelected"])
        for section in sections:
            body = text[section["start"]:section["end"]]
            self.assertEqual(section["textSha256"], article_sections.sha256_text(body))

    def test_groups_never_reference_an_absent_section(self):
        rec = record("Lead\n\n== Plot ==\n" + "P" * 6000 + "\n== Reception ==\n" + "R" * 6000)
        sections = article_sections.parse_sections(rec["text"], rec["plotSections"])
        groups = article_sections.section_groups(rec, sections, 7000)
        self.assertGreater(len(groups), 1)
        seen = []
        for group in groups:
            state = article_sections.state_for(rec, group)
            present = set(state["article"]["sections"])
            questions = {f"section__{s['id']}": combined_questions.section_question(s["id"])
                         for s in group}
            referenced = {key.removeprefix("section__") for key in questions}
            self.assertEqual(referenced, present)
            self.assertLessEqual(article_sections.encoded_chars(state), 7000)
            seen.extend(referenced)
        self.assertEqual(set(seen), {s["id"] for s in sections})


class RunnerTests(unittest.TestCase):
    def manifest(self):
        return {
            "runId": "test-run", "configSha256": "f" * 64,
            "config": {"requestedModel": combined_questions.PINNED_MODEL},
        }

    def test_normal_title_is_one_call_with_all_sections(self):
        rec = record("Lead.\n\n== Plot ==\nA happens.\n\n== Reception ==\nCritics liked it.")
        global_qs, _, _ = combined_questions.global_questions()
        client = FakeClient()
        row = run_combined.classify(rec, client, global_qs, self.manifest(), 110000)
        self.assertEqual(len(client.calls), 1)
        self.assertFalse(row["oversized"])
        self.assertEqual(len(row["answers"]), 94)
        self.assertEqual(len(row["sections"]), 3)
        self.assertEqual(row["omittedFromGlobalState"], [])

    def test_oversized_title_audits_every_section_before_global_selection(self):
        rec = record("Lead.\n\n== Plot ==\n" + "P" * 6000 + "\n== Reception ==\n" + "R" * 6000)
        global_qs, _, _ = combined_questions.global_questions()
        client = FakeClient()
        row = run_combined.classify(rec, client, global_qs, self.manifest(), 7000)
        self.assertTrue(row["oversized"])
        self.assertGreaterEqual(len(client.calls), 3)  # at least two groups, then global
        section_phases = [entry for entry in row["calls"] if entry["phase"].startswith("section-group")]
        self.assertEqual({sid for entry in section_phases for sid in entry["sectionIds"]},
                         {section["id"] for section in row["sections"]})
        self.assertEqual(row["calls"][-1]["phase"], "global-after-section-audit")

    def test_manifest_refuses_configuration_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "manifest.json")
            first = run_combined.load_or_create_manifest(path, {"model": "jev-1.13.0"})
            again = run_combined.load_or_create_manifest(path, {"model": "jev-1.13.0"})
            self.assertEqual(first["runId"], again["runId"])
            with self.assertRaisesRegex(SystemExit, "different"):
                run_combined.load_or_create_manifest(path, {"model": "jev-latest"})

    def test_plan_does_not_create_output_or_need_api_key(self):
        with tempfile.TemporaryDirectory() as directory:
            articles = os.path.join(directory, "articles.jsonl")
            output = os.path.join(directory, "out.jsonl")
            with open(articles, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(record("Lead\n\n== Plot ==\nStory")) + "\n")
            status = run_combined.main(["--articles", articles, "--out", output, "--plan"])
            self.assertEqual(status, 0)
            self.assertFalse(os.path.exists(output))
            self.assertFalse(os.path.exists(output + ".manifest.json"))
            self.assertFalse(os.path.exists(output + ".lock"))

    def test_paid_output_has_an_exclusive_process_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "out.jsonl.lock")
            first = run_combined.acquire_output_lock(path)
            try:
                with self.assertRaisesRegex(SystemExit, "another process"):
                    run_combined.acquire_output_lock(path)
            finally:
                run_combined.release_output_lock(first)
            second = run_combined.acquire_output_lock(path)
            run_combined.release_output_lock(second)

    def test_systemic_api_error_opens_circuit_before_corpus_is_consumed(self):
        with tempfile.TemporaryDirectory() as directory:
            articles = os.path.join(directory, "articles.jsonl")
            output = os.path.join(directory, "out.jsonl")
            with open(articles, "w", encoding="utf-8") as fh:
                for tmdb_id in range(100):
                    rec = record("Lead\n\n== Plot ==\nStory")
                    rec["tmdbId"] = tmdb_id
                    fh.write(json.dumps(rec) + "\n")
            FailingClient.instances.clear()
            with mock.patch.object(run_combined, "TypeSafe", FailingClient):
                status = run_combined.main([
                    "--articles", articles, "--out", output, "--workers", "8",
                ])
            client = FailingClient.instances[-1]
            self.assertEqual(status, 1)
            self.assertGreaterEqual(client.calls, 1)
            self.assertLessEqual(client.calls, 8, "only calls already in flight may escape the breaker")
            self.assertEqual(os.path.getsize(output), 0)


if __name__ == "__main__":
    unittest.main()
