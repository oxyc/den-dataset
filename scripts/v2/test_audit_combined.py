#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audit_combined
import combined_questions
import run_combined


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


if __name__ == "__main__":
    unittest.main()
