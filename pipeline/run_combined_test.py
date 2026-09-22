#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock
from types import SimpleNamespace

from . import article_sections, combined_questions, run_combined
from .contract import REPO

sys.path.insert(0, os.path.join(REPO, "scripts", "v2"))
import resume_combined_excluding  # noqa: E402


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


#: What the shipped pass recorded in `combined-v1-r2.jsonl.manifest.json`. `SHIPPED_TAXONOMY_SHA` is the
#: digest of the vocabulary as Swift source; converting it to JSON (oxyc/den-dataset#27) necessarily
#: changed that, so it is no longer the file's digest and is kept here as the superseded one the lineage
#: has to name. The two question digests are what replaced it as the pin: they are what the pass actually
#: bought answers to, and they did not move.
SHIPPED_TAXONOMY_SHA = "dd048e59177955307d1bbff7bf88c79f4a66e8bec053c96a32769406d6172f49"
SHIPPED_GLOBAL_QUESTIONS_SHA = "8812611c5690e50914e31918dfc3734d7d9c4b1802f5b8fdfa3a77c5f13b5964"
SHIPPED_LABEL_MAPPING_SHA = "06ca2b871550e6ffb197afc17c0192da6a5a6c6e1eb3a5d011010f85964fc577"


class QuestionTests(unittest.TestCase):
    def test_the_vocabulary_is_the_committed_json_file(self):
        """The vocabulary is data, not code: nothing compiled the Swift it used to be written in, and it
        sits beside the other committed inputs under the name of what it holds."""
        self.assertEqual(os.path.relpath(combined_questions.TAXONOMY, combined_questions.ROOT),
                         os.path.join("data", "genres-moods-vocabulary.json"))
        with open(combined_questions.TAXONOMY, encoding="utf-8") as fh:
            document = json.load(fh)
        self.assertEqual(document["version"], "t02")

    def test_the_questions_built_from_it_are_the_ones_the_paid_pass_asked(self):
        """The $20.47 pass will not be run again, so the question set it bought answers to is the oracle,
        and it is the whole safety argument for rewriting the file the questions are built from: the
        vocabulary is JSON now, and these two digests say the labels came through it unchanged. A label
        added, dropped, renamed or reordered would show up here as a different digest."""
        questions, mapping, _ = combined_questions.global_questions()
        self.assertEqual(run_combined.sha256_text(run_combined.canonical(questions)),
                         SHIPPED_GLOBAL_QUESTIONS_SHA)
        self.assertEqual(run_combined.sha256_text(run_combined.canonical(mapping)),
                         SHIPPED_LABEL_MAPPING_SHA)

    def test_the_regional_labels_no_question_is_built_from_are_the_shipped_ones(self):
        """The one family the question digests cannot speak for: regional labels describe origin, are
        derived from metadata and are asked about nowhere, so they are held to their names directly."""
        self.assertEqual(combined_questions.taxonomy()["regional"], [
            "Nordic Noir", "K-Drama", "Korean Thriller", "British Crime", "French Cinema",
            "Italian Cinema", "Spanish-language Thriller", "Latin American", "Turkish Drama",
            "Bollywood/Hindi", "Scandinavian", "J-Horror", "German Cinema"])

    def test_a_label_written_twice_is_refused(self):
        """One spelling per label, across families as well as within one. Two families holding the same
        name would ask the same question twice under different ids, or collide on one."""
        document = json.load(open(combined_questions.TAXONOMY, encoding="utf-8"))
        document["moods"] = document["moods"] + ["Heist"]
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "vocabulary.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(document, fh)
            with self.assertRaisesRegex(ValueError, "written once"):
                combined_questions.taxonomy(path)

    def test_a_family_that_is_missing_or_empty_is_refused(self):
        """A vocabulary that lost a family would build a question set missing every label in it, and the
        run would buy answers to it. It stops at the read instead."""
        for broken in ({"moods": []}, {"thematic": None}, {"version": ""}):
            document = {**json.load(open(combined_questions.TAXONOMY, encoding="utf-8")), **broken}
            with tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, "vocabulary.json")
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(document, fh)
                with self.assertRaises(ValueError):
                    combined_questions.taxonomy(path)

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

    def config_for(self, directory):
        """The configuration a real run would record, over a one-title article dump."""
        articles = os.path.join(directory, "articles.jsonl")
        with open(articles, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(record("Lead\n\n== Plot ==\nStory")) + "\n")
        args = run_combined.argument_parser().parse_args(
            ["--articles", articles, "--out", os.path.join(directory, "out.jsonl")])
        global_qs, mapping, tax = combined_questions.global_questions(args.prompt, args.taxonomy)
        return run_combined.manifest_config(args, global_qs, mapping, tax, "enriched-sha")

    def test_the_manifest_records_the_taxonomy_repo_relative(self):
        """Where a committed file sits is not part of what a run bought. Recorded absolute, one pass in
        two checkouts hashed to two configurations, and moving the file was a change to the run's
        identity rather than to a filename."""
        with tempfile.TemporaryDirectory() as directory:
            config = self.config_for(directory)
        self.assertEqual(config["taxonomy"], os.path.join("data", "genres-moods-vocabulary.json"))
        self.assertFalse(os.path.isabs(config["taxonomy"]))
        self.assertTrue(os.path.isfile(os.path.join(combined_questions.ROOT, config["taxonomy"])))
        self.assertEqual(config["taxonomyVersion"], "t02")
        self.assertEqual(config["globalQuestionsSha256"], SHIPPED_GLOBAL_QUESTIONS_SHA)
        self.assertEqual(config["plannerVersion"], run_combined.PLANNER_VERSION)

    def test_a_taxonomy_outside_the_repo_keeps_its_absolute_path(self):
        """`repo_relative` spells a path from the root only when it is under it. A `../..` walk out of the
        repo would be neither absolute nor repo-relative — it would depend on where the checkout sits,
        which is the property being removed."""
        outside = os.path.join(os.path.dirname(combined_questions.ROOT), "elsewhere", "vocabulary.json")
        self.assertEqual(run_combined.repo_relative(outside), outside)

    def test_a_manifest_from_before_the_taxonomy_moved_is_refused_and_says_what_to_do(self):
        """The shards already bought. The planner bump makes their configuration a different one, so a
        resume stops — and the refusal has to distinguish this from a real change, because the rows are
        fine: the same labels, at a new path and in a new format.

        The refusal claims that from the config in front of it rather than from history: the question
        digests are in the same config and are not among the keys that differ, so what this run would ask
        is what the manifest bought.
        """
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "manifest.json")
            config = self.config_for(directory)
            before = {**config, "plannerVersion": "whole-or-role-selected-v1",
                      "taxonomy": "/somewhere/else/Sources/DenDataset/Taxonomy.swift",
                      "taxonomySha256": SHIPPED_TAXONOMY_SHA}
            created = run_combined.load_or_create_manifest(path, before)
            with self.assertRaises(SystemExit) as refused:
                run_combined.load_or_create_manifest(path, config)
            message = str(refused.exception)
            self.assertIn("plannerVersion", message)
            self.assertIn("taxonomySha256", message)
            self.assertIn("data/genres-moods-vocabulary.json", message)
            self.assertIn("The questions are unchanged", message)
            self.assertIn("audit_combined_bundle.py", message)
            with open(path, encoding="utf-8") as fh:
                on_disk = json.load(fh)
            self.assertEqual(on_disk["configSha256"], created["configSha256"],
                             "the refusal re-stamped the manifest it was supposed to preserve")

    def test_a_refusal_that_is_not_the_move_names_the_keys_and_claims_nothing_about_the_vocabulary(self):
        """The same refusal for any other drift. Saying the vocabulary is unchanged when the model or the
        article dump moved would excuse the change it is there to catch."""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "manifest.json")
            config = self.config_for(directory)
            run_combined.load_or_create_manifest(path, config)
            with self.assertRaises(SystemExit) as refused:
                run_combined.load_or_create_manifest(path, {**config, "requestedModel": "jev-9.9.9"})
            message = str(refused.exception)
            self.assertIn("requestedModel", message)
            self.assertIn("jev-9.9.9", message)
            self.assertNotIn("The questions are unchanged", message)

    def test_a_vocabulary_that_really_changed_is_not_called_a_reformatting(self):
        """The claim is only made while the question digests agree. Edit a label and they do not, so the
        key that says so is among the differences and the message stays silent about the vocabulary —
        which is the whole reason it reads them off the config instead of off this commit's history."""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "manifest.json")
            config = self.config_for(directory)
            run_combined.load_or_create_manifest(path, config)
            changed = {**config, "taxonomy": os.path.join("data", "elsewhere.json"),
                       "taxonomySha256": "0" * 64, "globalQuestionsSha256": "1" * 64}
            with self.assertRaises(SystemExit) as refused:
                run_combined.load_or_create_manifest(path, changed)
            message = str(refused.exception)
            self.assertIn("globalQuestionsSha256", message)
            self.assertNotIn("The questions are unchanged", message)

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

    def test_resume_exclusion_requires_an_existing_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            articles = os.path.join(directory, "articles.jsonl")
            output = os.path.join(directory, "out.jsonl")
            with open(articles, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(record("Lead\n\n== Plot ==\nStory")) + "\n")
            with self.assertRaisesRegex(SystemExit, "existing manifest required"):
                resume_combined_excluding.main([
                    "--articles", articles, "--out", output, "--exclude-key", "movie:7",
                ])

    def test_resume_exclusion_rejects_unknown_key_before_paid_run(self):
        with tempfile.TemporaryDirectory() as directory:
            articles = os.path.join(directory, "articles.jsonl")
            output = os.path.join(directory, "out.jsonl")
            with open(articles, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(record("Lead\n\n== Plot ==\nStory")) + "\n")
            with open(output + ".manifest.json", "w", encoding="utf-8") as fh:
                fh.write("{}")
            with self.assertRaisesRegex(SystemExit, "excluded keys absent"):
                resume_combined_excluding.main([
                    "--articles", articles, "--out", output,
                    "--exclude-key", "movie:999",
                ])

    def test_resume_exclusion_rejects_zero_limit(self):
        with self.assertRaisesRegex(SystemExit, "cannot be combined"):
            resume_combined_excluding.main([
                "--articles", "unused", "--out", "unused", "--exclude-key", "movie:7",
                "--limit", "0",
            ])

    def test_resume_exclusion_selects_other_rows_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            articles = os.path.join(directory, "articles.jsonl")
            output = os.path.join(directory, "out.jsonl")
            first = record("Lead\n\n== Plot ==\nOne")
            second = record("Lead\n\n== Plot ==\nTwo")
            second["tmdbId"] = 8
            with open(articles, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(first) + "\n")
                fh.write(json.dumps(second) + "\n")
            with open(output + ".manifest.json", "w", encoding="utf-8") as fh:
                fh.write("{}")
            handle = object()
            with mock.patch.object(resume_combined_excluding, "global_questions", return_value=({}, {}, {})), \
                    mock.patch.object(resume_combined_excluding, "attach_enriched_evidence", return_value="sha"), \
                    mock.patch.object(resume_combined_excluding, "acquire_output_lock", return_value=handle), \
                    mock.patch.object(resume_combined_excluding, "release_output_lock") as release, \
                    mock.patch.object(resume_combined_excluding, "paid_run", return_value=0) as paid:
                status = resume_combined_excluding.main([
                    "--articles", articles, "--out", output, "--exclude-key", "movie:7",
                ])
            self.assertEqual(status, 0)
            self.assertEqual([run_combined.article_key(rec) for rec in paid.call_args.args[-1]], ["movie:8"])
            release.assert_called_once_with(handle)

    def test_resume_exclusion_rejects_key_already_in_original_output(self):
        with tempfile.TemporaryDirectory() as directory:
            articles = os.path.join(directory, "articles.jsonl")
            output = os.path.join(directory, "out.jsonl")
            rec = record("Lead\n\n== Plot ==\nStory")
            with open(articles, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
            with open(output, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"mediaType": "movie", "tmdbId": 7}) + "\n")
            with open(output + ".manifest.json", "w", encoding="utf-8") as fh:
                fh.write("{}")
            with self.assertRaisesRegex(SystemExit, "already present"):
                resume_combined_excluding.main([
                    "--articles", articles, "--out", output, "--exclude-key", "movie:7",
                ])


if __name__ == "__main__":
    unittest.main()
