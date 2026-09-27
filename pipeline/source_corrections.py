#!/usr/bin/env python3
"""Evidence-bound, operator-only correction transactions (den-dataset#145).

This module deliberately calls no source, model, embedding, or publishing service.  It freezes the
candidate an operator gives it, verifies an independently written approval, installs one deterministic
enriched batch, and refuses publication until receipts for every derived artifact bind to those bytes.
"""
import argparse
import hashlib
import json
import os
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import cache as caching

from pipeline import enrich
from pipeline.contract import StageError

SCHEMA = 1
STATES = ("awaiting", "appliedPending", "published")
PROOFS = ("classify", "critique", "genresMoods", "premise", "document", "vector", "withdrawal", "watcher")
KEY = re.compile(r"^(movie|tv):([0-9]+)$")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def read(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as error:
        raise StageError(f"correction: {path} is not readable JSON ({error})") from None


def write(path, value):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    caching.write_atomically(path, (json.dumps(value, indent=1, ensure_ascii=False, sort_keys=True) + "\n").encode())


def key_of(record):
    try:
        key = enrich.key(record["mediaType"], record["tmdbId"])
    except (KeyError, TypeError):
        raise StageError("correction: candidate has no mediaType/tmdbId") from None
    if not KEY.match(key):
        raise StageError(f"correction: malformed candidate key {key!r}")
    return key


def plot_sha(record):
    text = record.get("overview") if record.get("hasWikiPlot") else None
    if record.get("hasWikiPlot") and not isinstance(text, str):
        raise StageError("correction: grounded candidate has no plot text")
    return hashlib.sha256(text.encode()).hexdigest() if text is not None else None


def source(record, fallback=None):
    grounded = bool(record.get("hasWikiPlot"))
    owner = record if grounded else (fallback or {})
    revision = record.get("plotRevId") if grounded else None
    return {
        "article": owner.get("plotArticle"),
        "language": (owner.get("plotLanguage") or "en") if owner.get("plotArticle") else None,
        "revision": revision if isinstance(revision, int) and not isinstance(revision, bool) else None,
        "plotSha256": plot_sha(record),
        "status": "grounded" if grounded else "lost",
    }


def validate_article(key, candidate, article_input, identity):
    if not isinstance(article_input, dict):
        raise StageError("correction: candidate article input is not an object")
    if identity["status"] == "lost":
        expected = {"status": "missing", "article": identity["article"], "language": identity["language"]}
        if any(article_input.get(name) != value for name, value in expected.items()):
            raise StageError("correction: lost-plot observation does not preserve the watched article/language")
        return
    checks = {
        "mediaType": candidate["mediaType"], "tmdbId": candidate["tmdbId"],
        "article": identity["article"], "language": identity["language"], "revId": identity["revision"],
    }
    wrong = [name for name, value in checks.items() if article_input.get(name) != value]
    if wrong or not isinstance(article_input.get("text"), str):
        raise StageError(f"correction: frozen article input does not match {key} candidate ({wrong or ['text']})")


def evidence_for(baseline, candidate, article_input):
    key = key_of(candidate)
    if key_of(baseline) != key:
        raise StageError("correction: baseline and candidate name different titles")
    identity = source(candidate, baseline)
    if not identity["article"]:
        raise StageError("correction: no article identity to correct or watch")
    validate_article(key, candidate, article_input, identity)
    article_sha = digest(article_input)
    evidence = dict(identity, key=key, articleInputSha256=article_sha,
                    candidateRecordSha256=digest(candidate), baselineRecordSha256=digest(baseline))
    evidence["sourceDigestSha256"] = digest(evidence)
    return evidence


def transaction_path(root, key, source_digest):
    return os.path.join(root, f"{key.replace(':', '-')}-{source_digest[:16]}")


def prepare(root, baseline, candidate, article_input, review):
    """Freeze one candidate and its review entry. Repeating the same preparation is byte-idempotent."""
    if not isinstance(review, dict) or not review:
        raise StageError("correction: review entry must be a non-empty object")
    evidence = evidence_for(baseline, candidate, article_input)
    path = transaction_path(root, evidence["key"], evidence["sourceDigestSha256"])
    state_path = os.path.join(path, "state.json")
    wanted = {
        "schemaVersion": SCHEMA, "state": "awaiting", "evidence": evidence,
        "reviewEntrySha256": digest(review), "proofs": {},
    }
    if os.path.exists(state_path):
        existing = load(path)
        immutable = {k: existing[k] for k in ("schemaVersion", "evidence", "reviewEntrySha256")}
        if immutable != {k: wanted[k] for k in immutable}:
            raise StageError(f"correction: {path} already names different reviewed evidence")
        return path
    for name, value in (("baseline.json", baseline), ("candidate.json", candidate),
                        ("article-input.json", article_input), ("review.json", review)):
        write(os.path.join(path, name), value)
    write(state_path, wanted)
    return path


def load(path):
    state = read(os.path.join(path, "state.json"))
    if state.get("schemaVersion") != SCHEMA or state.get("state") not in STATES:
        raise StageError(f"correction: {path} has an unknown transaction state")
    frozen = (("candidate.json", "candidateRecordSha256"), ("baseline.json", "baselineRecordSha256"),
              ("article-input.json", "articleInputSha256"), ("review.json", "reviewEntrySha256"))
    for name, field in frozen:
        expected = state[field] if field == "reviewEntrySha256" else state["evidence"][field]
        if digest(read(os.path.join(path, name))) != expected:
            raise StageError(f"correction: frozen {name} no longer matches the reviewed transaction")
    baseline = read(os.path.join(path, "baseline.json"))
    candidate = read(os.path.join(path, "candidate.json"))
    article_input = read(os.path.join(path, "article-input.json"))
    if evidence_for(baseline, candidate, article_input) != state.get("evidence"):
        raise StageError("correction: state evidence no longer describes the frozen candidate")
    return state


def approval_for(path, **audit):
    """Return the exact approval shape for a human-controlled signer/writer; this does not store approval."""
    state = load(path)
    return dict(audit, decision="approve", key=state["evidence"]["key"], evidence=state["evidence"],
                reviewEntrySha256=state["reviewEntrySha256"])


def verify_approval(state, approval):
    expected = {"decision": "approve", "key": state["evidence"]["key"], "evidence": state["evidence"],
                "reviewEntrySha256": state["reviewEntrySha256"]}
    if any(approval.get(name) != value for name, value in expected.items()):
        raise StageError("correction: approval does not bind the exact revision/article/language/plot/review")


def queue_moved(path, observed, article_input):
    state = load(path)
    baseline = read(os.path.join(path, "candidate.json"))
    review = {"reason": "source moved after review", "supersedes": state["evidence"]["sourceDigestSha256"]}
    queued = prepare(os.path.dirname(path), baseline, observed, article_input, review)
    state["movedTo"] = os.path.basename(queued)
    write(os.path.join(path, "state.json"), state)
    return queued


def _next_batch(enriched_dir):
    return max((number for number, _name in enrich.batches(enriched_dir)), default=0) + 1


def apply(path, enriched_dir, approval, observed, article_input):
    """Install the approved record once. No derived work and no external request happens here."""
    state = load(path)
    if state["state"] == "published":
        return enrich.batch_path(os.path.dirname(enriched_dir), state["applyBatch"])
    verify_approval(state, approval)
    current = evidence_for(read(os.path.join(path, "baseline.json")), observed, article_input)
    if current != state["evidence"]:
        queued = queue_moved(path, observed, article_input)
        raise StageError(f"correction: source moved after review; queued {queued} for a new review")
    if "applyBatch" not in state:
        state["applyBatch"] = _next_batch(enriched_dir)
        write(os.path.join(path, "state.json"), state)
    batch = enrich.batch_path(os.path.dirname(enriched_dir), state["applyBatch"])
    body = enrich.swift_json([read(os.path.join(path, "candidate.json"))]).encode("utf-8")
    if os.path.exists(batch):
        with open(batch, "rb") as handle:
            if handle.read() != body:
                raise StageError(f"correction: reserved batch {batch} contains different bytes")
    else:
        os.makedirs(enriched_dir, exist_ok=True)
        caching.write_atomically(batch, body)
    state["state"] = "appliedPending"
    write(os.path.join(path, "state.json"), state)
    return batch


def validate_proof(path, state, kind, proof):
    evidence = state["evidence"]
    if proof.get("key") != evidence["key"] or proof.get("sourceDigestSha256") != evidence["sourceDigestSha256"]:
        raise StageError(f"correction: {kind} proof is not derived from this candidate")
    if kind in ("classify", "critique", "genresMoods", "premise") \
            and proof.get("articleInputSha256") != evidence["articleInputSha256"]:
        raise StageError(f"correction: {kind} proof reused another article input")
    if kind == "document":
        needed = ("classify", "critique", "genresMoods", "premise")
        expected_inputs = {name: state["proofs"].get(name) for name in needed}
        if None in expected_inputs.values() or proof.get("inputs") != expected_inputs:
            raise StageError("correction: document proof does not bind every fresh model/label artifact")
        if not isinstance(proof.get("text"), str) or proof.get("documentSha256") != hashlib.sha256(proof["text"].encode()).hexdigest():
            raise StageError("correction: document proof does not hash its exact text")
    if kind == "vector":
        document = read(os.path.join(path, "proof-document.json")) if os.path.exists(os.path.join(path, "proof-document.json")) else {}
        if proof.get("documentSha256") != document.get("documentSha256"):
            raise StageError("correction: vector proof does not bind the frozen document")
    if kind == "withdrawal" and proof.get("withdrawn") is not True:
        raise StageError("correction: withdrawal proof does not withdraw the lost candidate")
    if kind == "watcher" and (proof.get("article"), proof.get("language")) != \
            (evidence["article"], evidence["language"]):
        raise StageError("correction: watcher does not retain the lost source identity")


def attach_proof(path, kind, proof):
    """Freeze a derived-artifact receipt. Receipts are data from other stages, never generated here."""
    if kind not in PROOFS:
        raise StageError(f"correction: unknown proof kind {kind}")
    state = load(path)
    if state["state"] != "appliedPending":
        raise StageError("correction: proofs attach only after exact candidate application")
    validate_proof(path, state, kind, proof)
    proof_path = os.path.join(path, f"proof-{kind}.json")
    if os.path.exists(proof_path) and read(proof_path) != proof:
        raise StageError(f"correction: refusing to replace the {kind} proof")
    write(proof_path, proof)
    state = load(path)
    state["proofs"][kind] = digest(proof)
    write(os.path.join(path, "state.json"), state)


def publication_gate(path):
    state = load(path)
    if state["state"] not in ("appliedPending", "published"):
        raise StageError("correction: candidate has not been applied")
    grounded = state["evidence"]["status"] == "grounded"
    required = ("classify", "critique", "genresMoods", "premise", "document", "vector") if grounded \
        else ("withdrawal", "watcher")
    missing = [kind for kind in required if kind not in state["proofs"]]
    if missing:
        raise StageError(f"correction: no-spend/publish refusal; exact candidate lacks {missing}")
    for kind, expected in state["proofs"].items():
        proof = read(os.path.join(path, f"proof-{kind}.json"))
        if digest(proof) != expected:
            raise StageError(f"correction: {kind} proof changed after it was attached")
        # Re-run all binding checks, including document/vector order, without rewriting.
        validate_proof(path, state, kind, proof)
    return {"correctionDigestSha256": state["evidence"]["sourceDigestSha256"],
            "applyBatch": state["applyBatch"], "proofs": dict(state["proofs"])}


def record_published(path, receipt):
    gate = publication_gate(path)
    if any(receipt.get(name) != value for name, value in gate.items()):
        raise StageError("correction: publish receipt does not contain the exact gated artifact set")
    if not isinstance(receipt.get("datasetVersion"), str) or not receipt["datasetVersion"]:
        raise StageError("correction: publish receipt names no dataset version")
    if not isinstance(receipt.get("maxBatchId"), int) or isinstance(receipt["maxBatchId"], bool) \
            or receipt["maxBatchId"] < gate["applyBatch"]:
        raise StageError("correction: publish receipt does not include the applied correction batch")
    state = load(path)
    state["state"] = "published"
    state["published"] = receipt
    write(os.path.join(path, "state.json"), state)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_cmd = sub.add_parser("prepare", help="freeze a candidate and review; calls no service")
    prepare_cmd.add_argument("--root", required=True)
    for name in ("baseline", "candidate", "article-input", "review"):
        prepare_cmd.add_argument(f"--{name}", required=True)
    apply_cmd = sub.add_parser("apply", help="install an exactly approved candidate as appliedPending")
    apply_cmd.add_argument("transaction")
    apply_cmd.add_argument("--enriched-dir", required=True)
    apply_cmd.add_argument("--approval", required=True)
    apply_cmd.add_argument("--observed", required=True)
    apply_cmd.add_argument("--article-input", required=True)
    proof_cmd = sub.add_parser("attach-proof", help="freeze a receipt emitted by a derived-artifact pass")
    proof_cmd.add_argument("transaction")
    proof_cmd.add_argument("kind", choices=PROOFS)
    proof_cmd.add_argument("proof")
    gate = sub.add_parser("gate", help="prove a transaction is safe to publish; writes nothing")
    gate.add_argument("transaction")
    published = sub.add_parser("record-published", help="record an exact receipt after publication")
    published.add_argument("transaction")
    published.add_argument("receipt")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        print(prepare(args.root, read(args.baseline), read(args.candidate), read(args.article_input),
                      read(args.review)))
    elif args.command == "apply":
        print(apply(args.transaction, args.enriched_dir, read(args.approval), read(args.observed),
                    read(args.article_input)))
    elif args.command == "attach-proof":
        attach_proof(args.transaction, args.kind, read(args.proof))
    elif args.command == "gate":
        print(json.dumps(publication_gate(args.transaction), indent=1, sort_keys=True))
    elif args.command == "record-published":
        record_published(args.transaction, read(args.receipt))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StageError as error:
        raise SystemExit(f"error: {error}") from None
