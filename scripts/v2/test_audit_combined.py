#!/usr/bin/env python3
import copy
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audit_combined
import combined_questions
import run_combined

#: The `taxonomySha256` every shard in `out-repass` records: the genres & moods vocabulary as the Swift
#: source it was written in until oxyc/den-dataset#27 converted it to JSON.
SUPERSEDED_TAXONOMY_SHA = "dd048e59177955307d1bbff7bf88c79f4a66e8bec053c96a32769406d6172f49"


def answer_for(question):
    kind = question["type"]
    if kind == "noul":
        return {"type": "noul", "noul": 0.8}
    if kind == "choice":
        values = list(question["criteria"])
        return {
            "type": "choice", "choice": values[0], "confidence": 1.0,
            "probabilities": {value: 1.0 if index == 0 else 0.0
                              for index, value in enumerate(values)},
        }
    return {
        "type": "score", "score": 0.0, "confidence": 1.0,
        "legend": {str(index): value for index, value in enumerate(question["criteria"])},
        "probabilities": {str(index): 1.0 if index == 0 else 0.0
                          for index in range(len(question["criteria"]))},
    }


class FakeClient:
    model = combined_questions.PINNED_MODEL

    def ask_with_metadata(self, state, questions):
        return ({key: answer_for(question) for key, question in questions.items()}, {
            "model": self.model, "usage": {"input_tokens": 10, "output_tokens": 5},
        })


def fixture():
    questions = {
        "validity": {
            "type": "choice", "instructions": "valid?",
            "criteria": {"correct-screen-work": "yes", "source-work": "no"},
        },
        "narrative_applicability": {
            "type": "choice", "instructions": "kind?",
            "criteria": {"bounded-fictional-narrative": "yes", "non-narrative-program": "no"},
        },
        "era": {
            "type": "choice", "instructions": "when?",
            "criteria": {"contemporary": "now", "does-not-apply": "unknown"},
        },
        "tax__mood__cozy": {"type": "noul", "instructions": "cozy?"},
        "score__intensity": {
            "type": "score", "instructions": "intense?", "criteria": ["low", "high"],
        },
    }
    rec = {
        "mediaType": "movie", "tmdbId": 7, "title": "Example", "year": 2001,
        "article": "Example (film)", "language": "en", "revId": 99,
        "extractorArticleRevId": 99, "plotSections": ["Plot"], "sections": ["Plot", "Reception"],
        "text": "An example film.\n== Plot ==\nA person returns home.\n== Reception ==\nIt was praised.",
    }
    manifest = {
        "runId": "audit-test", "configSha256": "config-test",
        "config": {
            "requestedModel": FakeClient.model, "maxStateChars": 110_000,
            "globalQuestions": questions,
        },
    }
    row = run_combined.classify(rec, FakeClient(), questions, manifest, 110_000)
    return rec, manifest, row


class CombinedAuditTests(unittest.TestCase):
    def test_complete_artifact_is_reconstructed_and_summarized(self):
        rec, manifest, row = fixture()
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "combined.jsonl")
            with open(output, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
            summary = audit_combined.audit([rec], output, manifest)
        self.assertEqual(summary["integrity"]["rows"], 1)
        self.assertEqual(summary["run"]["calls"], 1)
        self.assertEqual(summary["run"]["inputTokens"], 10)
        self.assertEqual(summary["choices"]["era"]["publishable"], 1)
        self.assertEqual(summary["labelsAtLeast070"]["tax__mood__cozy"], 1)
        self.assertEqual(summary["sections"]["count"], 3)

    def test_tampered_call_provenance_fails(self):
        rec, manifest, row = fixture()
        row["calls"][0]["stateSha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "combined.jsonl")
            with open(output, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
            with self.assertRaisesRegex(ValueError, "state provenance differs"):
                audit_combined.audit([rec], output, manifest)

    def test_incomplete_artifact_fails(self):
        rec, manifest, _ = fixture()
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "combined.jsonl")
            open(output, "w", encoding="utf-8").close()
            with self.assertRaisesRegex(ValueError, "missing 1 article keys"):
                audit_combined.audit([rec], output, manifest)

    def test_disjoint_manifested_shards_form_exact_union(self):
        first, first_manifest, first_row = fixture()
        second = copy.deepcopy(first)
        second["tmdbId"] = 8
        second["title"] = "Second"
        second_manifest = copy.deepcopy(first_manifest)
        second_manifest["runId"] = "fallback-run"
        second_manifest["configSha256"] = "fallback-config"
        second_row = run_combined.classify(
            second, FakeClient(), second_manifest["config"]["globalQuestions"],
            second_manifest, 110_000,
        )
        with tempfile.TemporaryDirectory() as directory:
            first_output = os.path.join(directory, "first.jsonl")
            second_output = os.path.join(directory, "second.jsonl")
            with open(first_output, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(first_row) + "\n")
            with open(second_output, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(second_row) + "\n")
            summary = audit_combined.audit([first, second], sources=[
                {"output": first_output, "manifest": first_manifest, "allowedKeys": {"movie:7"}},
                {"output": second_output, "manifest": second_manifest, "allowedKeys": {"movie:8"}},
            ])
        self.assertEqual(summary["integrity"]["rows"], 2)
        self.assertEqual(summary["integrity"]["shards"], 2)
        self.assertEqual(summary["run"]["calls"], 2)


class ManifestFileTests(unittest.TestCase):
    """How a manifest's committed inputs are found again.

    The taxonomy moved out of `Sources/` (oxyc/den-dataset#27) and is recorded repo-relative from then
    on. A manifest older than the move names an absolute path in whatever checkout bought the shard, and
    the shards it describes were paid for once — so the audit has to reach the file they name. What it
    holds them to is the digest recorded beside the path, which the move did not touch.
    """

    def manifest_for(self, **overrides):
        prompt_sha = run_combined.sha256_file(combined_questions.PROMPT)
        taxonomy_sha = run_combined.sha256_file(combined_questions.TAXONOMY)
        config = {
            "schemaVersion": run_combined.SCHEMA_VERSION,
            "articlesSha256": "articles-sha", "enrichedEvidenceSha256": "enriched-sha",
            "prompt": combined_questions.PROMPT, "promptSha256": prompt_sha,
            "taxonomy": os.path.join("data", "genres-moods-vocabulary.json"),
            "taxonomySha256": taxonomy_sha,
            "globalQuestions": {}, "globalQuestionsSha256": audit_combined.sha256_text(
                run_combined.canonical({})),
            "sectionQuestionTemplate": audit_combined.section_question("SECTION_ID"),
            "sectionQuestionTemplateSha256": audit_combined.sha256_text(run_combined.canonical(
                audit_combined.section_question("SECTION_ID"))),
            "labelQuestionMapping": {}, "labelQuestionMappingSha256": audit_combined.sha256_text(
                run_combined.canonical({})),
            "implementationSha256": {},
        }
        config.update(overrides)
        return {"configSha256": audit_combined.sha256_text(run_combined.canonical(config)),
                "config": config}

    def validate(self, manifest):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            articles = fh.name
        self.addCleanup(os.remove, articles)
        manifest["config"]["articlesSha256"] = run_combined.sha256_file(articles)
        manifest["configSha256"] = audit_combined.sha256_text(
            run_combined.canonical(manifest["config"]))
        audit_combined.validate_manifest(manifest, articles, "enriched-sha")

    def test_a_repo_relative_taxonomy_resolves_against_the_repo(self):
        """The recorded spelling since the move, and the reason a checkout elsewhere can audit a shard."""
        config = self.manifest_for()["config"]
        self.assertEqual(audit_combined.manifest_file(config, "taxonomy"),
                         os.path.join(audit_combined.ROOT, "data", "genres-moods-vocabulary.json"))
        self.validate(self.manifest_for())

    def test_a_manifest_that_names_the_pre_move_path_is_still_auditable(self):
        """Every shard in `out-repass` names `<checkout>/Sources/DenDataset/Taxonomy.swift`, which is not
        a file anywhere any more, so the file the pass reads today answers for it."""
        pre_move = "/gone/Sources/DenDataset/Taxonomy.swift"
        self.assertEqual(audit_combined.manifest_file({"taxonomy": pre_move}, "taxonomy"),
                         combined_questions.TAXONOMY)
        self.validate(self.manifest_for(taxonomy=pre_move))

    def test_a_shipped_shard_names_the_deleted_swift_path_and_its_digest_and_still_passes(self):
        """What every manifest in `out-repass` actually holds: the pre-move absolute path AND the digest
        of the vocabulary as Swift source, which converting it to JSON could not preserve. The path falls
        back to the file the pass reads today; the digest is forgiven only because the conversion is
        recorded under `supersededInputs`, with the question digests as the evidence."""
        self.validate(self.manifest_for(taxonomy="/gone/Sources/DenDataset/Taxonomy.swift",
                                        taxonomySha256=SUPERSEDED_TAXONOMY_SHA))

    def test_a_vocabulary_that_really_changed_is_still_refused(self):
        """What the allowance must not become: a path nobody can resolve is forgiven and a recorded
        rewrite is forgiven, but a digest nobody wrote a reason for is not."""
        with self.assertRaisesRegex(ValueError, "taxonomy artifact hash differs"):
            self.validate(self.manifest_for(taxonomySha256="0" * 64))
        with self.assertRaisesRegex(ValueError, "taxonomy artifact hash differs"):
            self.validate(self.manifest_for(taxonomy="/gone/Sources/DenDataset/Taxonomy.swift",
                                            taxonomySha256="0" * 64))

    def test_a_prompt_digest_borrows_nothing_from_the_vocabularys_allowance(self):
        """The allowances are per input. A prompt that changed is a change to what was asked, and the
        entry recorded for the vocabulary says nothing about it."""
        with self.assertRaisesRegex(ValueError, "prompt artifact hash differs"):
            self.validate(self.manifest_for(promptSha256=SUPERSEDED_TAXONOMY_SHA))

    def test_a_manifest_that_names_no_taxonomy_at_all_is_refused(self):
        self.assertIsNone(audit_combined.manifest_file({}, "taxonomy"))
        with self.assertRaisesRegex(ValueError, "taxonomy artifact hash differs"):
            self.validate(self.manifest_for(taxonomy=None))

    def test_a_recorded_change_prints_a_reference_it_cannot_abbreviate(self):
        """A lineage entry written by the commit that supersedes it names the change rather than a hash
        it cannot know, and cutting that to seven characters would print a fragment of a sentence."""
        self.assertEqual(audit_combined.short_ref("c03afa0c4e8dbad44b829101b6d52fdb4485c105"), "c03afa0")
        self.assertEqual(audit_combined.short_ref("the commit that moved the taxonomy"),
                         "the commit that moved the taxonomy")
        self.assertEqual(audit_combined.short_ref(None), "?")


class ImplementationLineageTests(unittest.TestCase):
    """The recorded exceptions to the implementation-hash refusal.

    The refusal is what stops a bundle produced by a version of the pass nobody can account for. It has
    to have exceptions, because the pass is edited for reasons that cannot reach a row — but an exception
    that is a bare digest is indistinguishable from one that hides a real change, so every entry has to
    carry the commit that superseded it and a reason somebody wrote.
    """

    IMPLEMENTATION = ("run_combined.py", "article_sections.py", "combined_questions.py",
                      "typesafe_client.py")

    def test_the_working_trees_digests_need_no_exception(self):
        current = {name: run_combined.sha256_file(os.path.join(audit_combined.HERE, name))
                   for name in self.IMPLEMENTATION}
        self.assertEqual(audit_combined.validate_implementation("here", {
            "implementationSha256": current}), [])

    def test_an_unrecorded_digest_is_refused(self):
        with self.assertRaisesRegex(ValueError, "implementation-lineage.json"):
            audit_combined.validate_implementation("here", {
                "implementationSha256": {"run_combined.py": "0" * 64}})

    def test_every_recorded_entry_names_a_commit_and_a_reason(self):
        for name, entries in audit_combined.load_lineage().items():
            self.assertIn(name, self.IMPLEMENTATION, "a file the pass does not hash")
            for entry in entries:
                self.assertRegex(entry.get("sha256", ""), r"^[0-9a-f]{64}$")
                self.assertTrue(entry.get("commit"), f"{name}: no commit")
                self.assertTrue(entry.get("supersededBy"), f"{name}: no superseding commit")
                self.assertGreater(len(entry.get("why", "")), 40, f"{name}: no reason worth reading")

    def test_no_recorded_entry_names_the_file_as_it_stands(self):
        """A stale entry. The digest in the tree needs no exception, and one recorded for it would go on
        excusing that digest after the file moves on."""
        for name, entries in audit_combined.load_lineage().items():
            current = run_combined.sha256_file(os.path.join(audit_combined.HERE, name))
            for entry in entries:
                self.assertNotEqual(entry["sha256"], current, f"{name}: stale lineage entry")


class InputLineageTests(unittest.TestCase):
    """The same mechanism for the committed inputs, kept in its own section of the same file.

    A data file's digest is not an implementation's. Recording the vocabulary's under a source file's
    name would say `combined_questions.py` changed in a way it did not, and would let a future
    vocabulary edit inherit a reason that was written about code.
    """

    ROLES = ("prompt", "taxonomy")

    def test_the_vocabularys_superseded_digest_is_recorded_with_its_evidence(self):
        entries = audit_combined.load_input_lineage()["taxonomy"]
        entry = next(e for e in entries if e["sha256"] == SUPERSEDED_TAXONOMY_SHA)
        self.assertIn("8812611c5690e50914e31918dfc3734d7d9c4b1802f5b8fdfa3a77c5f13b5964",
                      entry["evidence"])
        self.assertIn("06ca2b871550e6ffb197afc17c0192da6a5a6c6e1eb3a5d011010f85964fc577",
                      entry["evidence"])

    def test_every_recorded_entry_names_a_commit_a_reason_and_its_evidence(self):
        for role, entries in audit_combined.load_input_lineage().items():
            self.assertIn(role, self.ROLES, "an input the manifest does not pin by digest")
            for entry in entries:
                self.assertRegex(entry.get("sha256", ""), r"^[0-9a-f]{64}$")
                self.assertTrue(entry.get("commit"), f"{role}: no commit")
                self.assertTrue(entry.get("supersededBy"), f"{role}: no superseding commit")
                self.assertGreater(len(entry.get("why", "")), 40, f"{role}: no reason worth reading")
                self.assertGreater(len(entry.get("evidence", "")), 40, f"{role}: no evidence")

    def test_no_recorded_entry_names_the_input_as_it_stands(self):
        """The same staleness check the source files get: a digest the tree already holds needs no
        exception, and one left recorded for it would excuse the file after it changes again."""
        for role, entries in audit_combined.load_input_lineage().items():
            current = run_combined.sha256_file(audit_combined.CURRENT_FILE[role])
            for entry in entries:
                self.assertNotEqual(entry["sha256"], current, f"{role}: stale lineage entry")

    def test_the_working_trees_inputs_need_no_exception(self):
        config = {"prompt": combined_questions.PROMPT,
                  "promptSha256": run_combined.sha256_file(combined_questions.PROMPT),
                  "taxonomy": combined_questions.TAXONOMY,
                  "taxonomySha256": run_combined.sha256_file(combined_questions.TAXONOMY)}
        self.assertEqual(audit_combined.validate_inputs("here", config), [])

    def test_an_allowance_says_which_input_it_forgave(self):
        config = {"prompt": combined_questions.PROMPT,
                  "promptSha256": run_combined.sha256_file(combined_questions.PROMPT),
                  "taxonomy": "/gone/Sources/DenDataset/Taxonomy.swift",
                  "taxonomySha256": SUPERSEDED_TAXONOMY_SHA}
        granted = audit_combined.validate_inputs("here", config)
        self.assertEqual([entry["input"] for entry in granted], ["taxonomy"])
        self.assertEqual(granted[0]["file"], "data/taxonomy-t02.swift")


if __name__ == "__main__":
    unittest.main()
