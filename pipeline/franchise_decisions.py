#!/usr/bin/env python3
"""The durable, prose-free part of a paid franchise answer.

Raw franchise states contain Wikipedia text and the answer rows contain its title and provider audit
record.  They stay in the private work directory.  This file is the smaller decision the next stateless
daily run needs: candidate ids, the three typed answers, the pinned model and exact token usage.
"""
import hashlib
import json
import math
import os
import tempfile

from lib.typesafe_client import TypeSafe

from . import run_combined as rc
from .contract import StageError

SCHEMA = 1


def questions_sha(questions):
    return hashlib.sha256(rc.canonical(questions).encode()).hexdigest()


def _candidate_json(ids):
    # Character-overlap candidates are ``("characters", (<key>, ...))``.  Normalize
    # both tuple levels now: a JSON write/read does that implicitly, and leaving the
    # inner tuple alive made a freshly reconstructed raw decision compare unequal to
    # the identical durable decision loaded from JSON.
    return [[candidate[0], list(candidate[1])] if isinstance(candidate, tuple) else candidate
            for candidate in ids]


def _candidate_ids(stored, where):
    if not isinstance(stored, list):
        raise StageError(f"franchises: {where} candidates are not a list")
    out = []
    for candidate in stored:
        if isinstance(candidate, str):
            out.append(candidate)
        elif isinstance(candidate, list) and len(candidate) == 2 and candidate[0] == "characters" \
                and isinstance(candidate[1], list) and all(isinstance(key, str) for key in candidate[1]):
            out.append(("characters", tuple(candidate[1])))
        else:
            raise StageError(f"franchises: {where} has an invalid candidate {candidate!r}")
    return out


def usage_of(row):
    calls = row.get("calls") or []
    return {"inputTokens": sum(call.get("inputTokens") or 0 for call in calls),
            "outputTokens": sum(call.get("outputTokens") or 0 for call in calls)}


def from_paid_row(key, row, candidates):
    """The replayable decision from one fully audited paid row."""
    models = sorted({call.get("responseModel") for call in row.get("calls") or []
                     if call.get("responseModel")})
    if len(models) != 1:
        raise StageError(f"franchises: {key} paid row records {len(models)} response models")
    return {"candidates": _candidate_json(candidates), "answers": row["answers"], "model": models[0],
            "usage": usage_of(row)}


def validate_decision(key, decision, questions, pinned_model, where):
    if not isinstance(decision, dict) or set(decision) != {"candidates", "answers", "model", "usage"}:
        raise StageError(f"franchises: {where} {key} has invalid decision fields")
    candidates = _candidate_ids(decision["candidates"], f"{where} {key}")
    if not candidates:
        raise StageError(f"franchises: {where} {key} has no candidates")
    if decision["model"] != pinned_model:
        raise StageError(f"franchises: {where} {key} used {decision['model']!r}, expected {pinned_model!r}")
    try:
        rc.validate_answers(decision["answers"], questions)
    except rc.TypeSafeError as refusal:
        raise StageError(f"franchises: {where} {key}: {refusal}") from None
    usage = decision["usage"]
    if not isinstance(usage, dict) or set(usage) != {"inputTokens", "outputTokens"}:
        raise StageError(f"franchises: {where} {key} has invalid usage fields")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in usage.values()):
        raise StageError(f"franchises: {where} {key} has invalid token usage")
    return decision["answers"], candidates


def read(path, questions, pinned_model):
    if not os.path.exists(path):
        return {}, None
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    where = os.path.basename(path)
    if doc.get("schema") != SCHEMA:
        raise StageError(f"franchises: {where} schema is {doc.get('schema')!r}, expected {SCHEMA}")
    expected = questions_sha(questions)
    if doc.get("questionsSha256") != expected:
        raise StageError(f"franchises: {where} was decided under other franchise questions")
    if doc.get("model") != pinned_model:
        raise StageError(f"franchises: {where} model is {doc.get('model')!r}, expected {pinned_model!r}")
    decisions = doc.get("decisions")
    if not isinstance(decisions, dict):
        raise StageError(f"franchises: {where} decisions are not an object")
    for key, decision in decisions.items():
        validate_decision(key, decision, questions, pinned_model, where)
    expected_usage = totals(decisions)
    if doc.get("usage") != expected_usage:
        raise StageError(f"franchises: {where} usage does not total its decisions")
    return decisions, doc


def totals(decisions):
    input_tokens = sum(d["usage"]["inputTokens"] for d in decisions.values())
    output_tokens = sum(d["usage"]["outputTokens"] for d in decisions.values())
    return {"calls": len(decisions), "inputTokens": input_tokens, "outputTokens": output_tokens,
            "costUSD": round(input_tokens * TypeSafe.RATE_PER_INPUT_TOKEN, 8)}


def document(decisions, questions, pinned_model):
    return {"schema": SCHEMA, "questionsSha256": questions_sha(questions), "model": pinned_model,
            "decisions": dict(sorted(decisions.items())), "usage": totals(decisions)}


def write(path, decisions, questions, pinned_model):
    doc = document(decisions, questions, pinned_model)
    body = json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    try:
        with open(path, encoding="utf-8") as fh:
            if fh.read() == body:
                return doc
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd, candidate = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)),
                                     prefix=".tmp-franchise-decisions-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(candidate, path)
    finally:
        if os.path.exists(candidate):
            os.unlink(candidate)
    return doc


def projection(decisions, calls):
    """A measured projection from completed calls. The completed spend is exact; a future token count is not."""
    exact = totals(decisions)
    if not exact["calls"]:
        return {"calibration": exact, "projected": None}
    tokens = math.ceil(exact["inputTokens"] / exact["calls"] * calls)
    return {"calibration": exact,
            "projected": {"calls": calls, "inputTokens": tokens,
                          "costUSD": round(tokens * TypeSafe.RATE_PER_INPUT_TOKEN, 8),
                          "method": f"mean of {exact['calls']} completed calls; future tokens are an estimate"}}
