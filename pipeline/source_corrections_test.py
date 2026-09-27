import hashlib
import json
import os
import tempfile
import unittest

from . import enrich, source_corrections as corrections
from .contract import StageError


def record(text, revision=2, article="Example (film)", language="en"):
    row = {"mediaType": "movie", "tmdbId": 7, "animated": False, "wikidataItem": "Q7",
           "hasWikiPlot": text is not None}
    if text is not None:
        row.update(overview=text, plotArticle=article, plotLanguage=language, plotRevId=revision,
                   plotSections=["Plot"])
    return row


def article(text="Whole new article", revision=2, article_name="Example (film)", language="en"):
    return {"mediaType": "movie", "tmdbId": 7, "article": article_name, "language": language,
            "revId": revision, "text": text, "sections": ["Plot"], "plotSections": ["Plot"]}


class CorrectionTransaction(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.out = temp.name
        self.root = os.path.join(self.out, "corrections")
        self.enriched = os.path.join(self.out, "enriched")
        self.old = record("Old plot", 1)
        self.new = record("Correct plot", 2)
        self.input = article()
        self.tx = corrections.prepare(self.root, self.old, self.new, self.input,
                                      {"reviewer": "operator", "finding": "source correction"})
        self.approval = corrections.approval_for(self.tx, approvedBy="operator")

    def state(self):
        return corrections.load(self.tx)

    def apply(self):
        return corrections.apply(self.tx, self.enriched, self.approval, self.new, self.input)

    def proof(self, kind, **extra):
        evidence = self.state()["evidence"]
        base = {"key": evidence["key"], "sourceDigestSha256": evidence["sourceDigestSha256"]}
        if kind in ("classify", "critique", "genresMoods", "premise"):
            base["articleInputSha256"] = evidence["articleInputSha256"]
            base["row"] = {"fresh": kind}
        base.update(extra)
        return base

    def attach_all(self):
        for kind in ("classify", "critique", "genresMoods", "premise"):
            corrections.attach_proof(self.tx, kind, self.proof(kind))
        text = "the exact composed document"
        sha = hashlib.sha256(text.encode()).hexdigest()
        inputs = {kind: self.state()["proofs"][kind]
                  for kind in ("classify", "critique", "genresMoods", "premise")}
        corrections.attach_proof(self.tx, "document", self.proof(
            "document", text=text, documentSha256=sha, inputs=inputs))
        corrections.attach_proof(self.tx, "vector", self.proof("vector", documentSha256=sha,
                                                                 vectorSha256="1" * 64))

    def test_awaiting_freezes_exact_evidence_and_review(self):
        state = self.state()
        self.assertEqual(state["state"], "awaiting")
        self.assertEqual(state["evidence"]["revision"], 2)
        self.assertEqual(state["evidence"]["article"], "Example (film)")
        self.assertEqual(state["evidence"]["language"], "en")
        self.assertEqual(state["evidence"]["plotSha256"], hashlib.sha256(b"Correct plot").hexdigest())
        self.assertEqual(state["reviewEntrySha256"], corrections.digest(corrections.read(
            os.path.join(self.tx, "review.json"))))
        self.assertEqual(corrections.prepare(self.root, self.old, self.new, self.input,
                                             {"reviewer": "operator", "finding": "source correction"}), self.tx)

    def test_source_moving_between_review_and_apply_is_queued_not_applied(self):
        moved, moved_input = record("Moved again", 3), article("Third article", 3)
        with self.assertRaisesRegex(StageError, "source moved after review; queued"):
            corrections.apply(self.tx, self.enriched, self.approval, moved, moved_input)
        state = self.state()
        self.assertEqual(state["state"], "awaiting")
        queued = os.path.join(self.root, state["movedTo"])
        self.assertEqual(corrections.load(queued)["state"], "awaiting")
        self.assertEqual(corrections.read(os.path.join(queued, "candidate.json"))["overview"], "Moved again")
        self.assertFalse(os.path.exists(self.enriched))

    def test_approval_cannot_be_reused_for_article_language_revision_plot_or_review(self):
        for field, value in (("revision", 9), ("article", "Other"), ("language", "fr"),
                             ("plotSha256", "0" * 64)):
            wrong = json.loads(json.dumps(self.approval))
            wrong["evidence"][field] = value
            with self.assertRaisesRegex(StageError, "approval does not bind"):
                corrections.apply(self.tx, self.enriched, wrong, self.new, self.input)
        wrong = json.loads(json.dumps(self.approval))
        wrong["reviewEntrySha256"] = "0" * 64
        with self.assertRaisesRegex(StageError, "approval does not bind"):
            corrections.apply(self.tx, self.enriched, wrong, self.new, self.input)

    def test_retry_after_batch_write_before_state_write_is_idempotent(self):
        state = self.state()
        state["applyBatch"] = 1
        corrections.write(os.path.join(self.tx, "state.json"), state)
        os.makedirs(self.enriched)
        body = enrich.swift_json([self.new]).encode()
        with open(enrich.batch_path(self.out, 1), "wb") as handle:
            handle.write(body)
        batch = self.apply()
        self.assertEqual(batch, enrich.batch_path(self.out, 1))
        self.assertEqual(self.state()["state"], "appliedPending")
        with open(batch, "rb") as handle:
            self.assertEqual(handle.read(), body)
        self.assertEqual(self.apply(), batch)
        self.assertEqual(enrich.batches(self.enriched), [(1, "batch-1.json")])

    def test_reserved_batch_with_other_bytes_is_refused(self):
        state = self.state()
        state["applyBatch"] = 1
        corrections.write(os.path.join(self.tx, "state.json"), state)
        os.makedirs(self.enriched)
        with open(enrich.batch_path(self.out, 1), "w", encoding="utf-8") as handle:
            handle.write("[]")
        with self.assertRaisesRegex(StageError, "reserved batch.*different bytes"):
            self.apply()
        self.assertEqual(self.state()["state"], "awaiting")

    def test_no_spend_leaves_candidate_pending_and_publication_refuses(self):
        self.apply()
        self.assertEqual(self.state()["state"], "appliedPending")
        with self.assertRaisesRegex(StageError, "no-spend/publish refusal"):
            corrections.publication_gate(self.tx)
        self.assertEqual(self.state()["state"], "appliedPending")

    def test_old_article_or_old_labels_cannot_satisfy_candidate(self):
        self.apply()
        old = self.proof("classify")
        old["articleInputSha256"] = "0" * 64
        with self.assertRaisesRegex(StageError, "reused another article"):
            corrections.attach_proof(self.tx, "classify", old)
        old = self.proof("genresMoods")
        old["sourceDigestSha256"] = "0" * 64
        with self.assertRaisesRegex(StageError, "not derived from this candidate"):
            corrections.attach_proof(self.tx, "genresMoods", old)

    def test_exact_evidence_gates_and_records_publication(self):
        self.apply()
        self.attach_all()
        gate = corrections.publication_gate(self.tx)
        wrong = dict(gate, datasetVersion="v2", maxBatchId=gate["applyBatch"], proofs={})
        with self.assertRaisesRegex(StageError, "exact gated artifact set"):
            corrections.record_published(self.tx, wrong)
        receipt = dict(gate, datasetVersion="v2", maxBatchId=gate["applyBatch"])
        corrections.record_published(self.tx, receipt)
        self.assertEqual(self.state()["state"], "published")
        self.assertEqual(corrections.publication_gate(self.tx), gate)

    def test_changed_frozen_input_or_proof_is_refused(self):
        changed = dict(self.input, text="tampered")
        corrections.write(os.path.join(self.tx, "article-input.json"), changed)
        with self.assertRaisesRegex(StageError, "frozen article-input.json"):
            corrections.load(self.tx)

    def test_state_cannot_relabel_frozen_evidence(self):
        state = self.state()
        state["evidence"]["sourceDigestSha256"] = "0" * 64
        corrections.write(os.path.join(self.tx, "state.json"), state)
        with self.assertRaisesRegex(StageError, "state evidence no longer describes"):
            corrections.load(self.tx)


class LostAndRegained(unittest.TestCase):
    def test_lost_correction_keeps_watcher_that_can_queue_a_regain(self):
        with tempfile.TemporaryDirectory() as out:
            root, enriched = os.path.join(out, "corrections"), os.path.join(out, "enriched")
            old, lost = record("Old plot", 1), record(None)
            missing = {"status": "missing", "article": "Example (film)", "language": "en"}
            tx = corrections.prepare(root, old, lost, missing, {"finding": "plot was removed"})
            approval = corrections.approval_for(tx, approvedBy="operator")
            corrections.apply(tx, enriched, approval, lost, missing)
            evidence = corrections.load(tx)["evidence"]
            base = {"key": "movie:7", "sourceDigestSha256": evidence["sourceDigestSha256"]}
            corrections.attach_proof(tx, "withdrawal", dict(base, withdrawn=True))
            corrections.attach_proof(tx, "watcher", dict(base, article="Example (film)", language="en"))
            corrections.publication_gate(tx)

            regained, regained_input = record("The plot is back", 4), article("Regained article", 4)
            next_tx = corrections.prepare(root, lost, regained, regained_input,
                                          {"finding": "watcher observed plot regain", "watcher": base})
            next_state = corrections.load(next_tx)
            self.assertEqual(next_state["state"], "awaiting")
            self.assertEqual(next_state["evidence"]["status"], "grounded")
            self.assertEqual(next_state["evidence"]["revision"], 4)


if __name__ == "__main__":
    unittest.main()
