#!/usr/bin/env python3
"""Classify every grounded title's facets with a System One model, one call per title.

  ./den stage articles --out-dir out-repass
  scripts/v2/run_facets.py --articles out-repass/articles.jsonl --out out-repass/facets.jsonl [--limit N]

## Why this exists at all, and why it is not `llm_phase.py`

That module batches 40 titles into a file for one subagent, because a generating model is expensive per call
and the prompt has to be amortised. None of that holds here: the state is one article, questions are
evaluated in parallel against it, output is unbilled, and the answer comes back typed. So the unit is a
title, not a batch, and most of `llm_phase.py`'s hard-won rules become unnecessary rather than useful — a
typed API cannot write the wrong ids, cannot return a short file, and cannot emit a value outside its own
criteria. What survives is the discipline those rules protected: the output is keyed by the input, coverage
is checked against what was asked for, and a resumed run repeats nothing and drops nothing.

## What is asked

The facet axes (`facet_questions.py`, parsed from a versioned prompt) plus `validity`, which is
free: the state is already paid for, so asking whether it is the RIGHT state costs nothing but makes a
defect measurable that currently needs a human to notice. See den-dataset#16 — six tmdbIds share the
Wuthering Heights novel's article, and nothing in the pipeline can tell.

## What is stored

The whole probability distribution per axis, not the argmax. Output tokens are free, so keeping the
distribution costs nothing, and it turns thresholds into a build-time decision that can be retuned without
re-running anything. `TaxonomyClassifier` already works this way; it just currently thresholds a number a
generating model wrote down rather than a calibrated one.
"""
import argparse
import hashlib
import json
import math
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from lib.typesafe_client import MODEL, TypeSafe, TypeSafeError             # noqa: E402
from pipeline.facet_questions import PROMPT, questions as facet_questions  # noqa: E402

VALIDITY = {
    "validity": {
        "type": "choice",
        "instructions": ("Is this Wikipedia article grounded on the Requested target named at the top of "
                         "the state? Judge the article's primary subject, especially its opening sentence."),
        "criteria": {
            "correct-screen-work": ("The article is primarily about this requested film or television "
                                    "program. A program remains correct when it covers multiple seasons, "
                                    "summarizes episodes, or mentions spinoffs and related works."),
            "source-work": ("The article is primarily about a novel, book or light-novel series, play, "
                            "comic or manga series, game, or other source that the requested screen work "
                            "adapted. This remains source-work when later sections discuss adaptations."),
            "other-screen-work": ("The article is primarily about a different film, television program, "
                                  "remake, sequel or adaptation than the Requested target."),
            "multi-work-overview": ("The article primarily covers a franchise, brand, shared universe, or "
                                    "multiple distinct screen works or versions rather than this one target."),
            "season-or-episode": ("The article's subject is one particular season, episode, or episode "
                                  "list rather than the requested television program as a whole."),
            "not-a-work": "A person, disambiguation page, or something else that is not a work.",
        },
    },
}


def sha256_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def state_for(rec, max_chars=0):
    """Identify the requested screen work before the article, so validity is target-aware."""
    media = "film" if rec["mediaType"] == "movie" else "television program"
    year = f" ({rec['year']})" if rec.get("year") else ""
    article = rec["text"][:max_chars] if max_chars else rec["text"]
    return f"Requested target: {media} — {rec.get('title') or ''}{year}\n\nWikipedia article:\n{article}"


def validate_answers(answers, questions):
    """Reject partial, invented or malformed Choice output before it reaches an append-only artifact."""
    if not isinstance(answers, dict):
        raise TypeSafeError("answers is not an object")
    expected, actual = set(questions), set(answers)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise TypeSafeError(f"answer keys differ: missing={missing} extra={extra}")

    for axis, question in questions.items():
        if question.get("type") != "choice":
            raise TypeSafeError(f"{axis}: run_facets only supports Choice questions")
        answer = answers[axis]
        if not isinstance(answer, dict):
            raise TypeSafeError(f"{axis}: answer is not an object")
        allowed = set(question["criteria"])
        if answer.get("choice") not in allowed:
            raise TypeSafeError(f"{axis}: invalid choice {answer.get('choice')!r}")
        confidence = answer.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) \
                or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise TypeSafeError(f"{axis}: invalid confidence {confidence!r}")
        probabilities = answer.get("probabilities")
        if not isinstance(probabilities, dict) or set(probabilities) != allowed:
            missing = sorted(allowed - set(probabilities or {})) if isinstance(probabilities, dict) else []
            extra = sorted(set(probabilities or {}) - allowed) if isinstance(probabilities, dict) else []
            raise TypeSafeError(f"{axis}: probability keys differ: missing={missing} extra={extra}")
        for value, probability in probabilities.items():
            if isinstance(probability, bool) or not isinstance(probability, (int, float)) \
                    or not math.isfinite(probability) or not 0 <= probability <= 1:
                raise TypeSafeError(f"{axis}.{value}: invalid probability {probability!r}")
        total = sum(probabilities.values())
        if abs(total - 1.0) > 0.03:
            raise TypeSafeError(f"{axis}: probabilities sum to {total:.6f}, not 1")


def answer_row(rec, answers, question_set, questions_sha256, prompt_sha256, model, run_started_at,
               state, truncated):
    """One output row: the identity, the choice and confidence per axis, and the full distribution."""
    facets = {}
    for axis, a in answers.items():
        facets[axis] = {
            "value": a.get("choice"),
            "confidence": a.get("confidence"),
            "probabilities": a.get("probabilities") or {},
        }
    return {
        "mediaType": rec["mediaType"], "tmdbId": rec["tmdbId"], "title": rec.get("title"),
        "article": rec.get("article"), "language": rec.get("language"),
        "articleChars": rec.get("chars"), "articleRevId": rec.get("revId"),
        "articleSha256": sha256_text(rec["text"]),
        "stateChars": len(state), "stateSha256": sha256_text(state),
        "truncated": truncated,
        "questionSet": question_set, "questionsSha256": questions_sha256,
        "promptSha256": prompt_sha256, "model": model, "runStartedAt": run_started_at,
        "facets": facets,
    }


def argument_parser():
    ap = argparse.ArgumentParser()
    ap.add_argument("--articles", required=True, help="JSONL from `./den stage articles`")
    ap.add_argument("--out", required=True, help="JSONL, appended to; re-running resumes from it")
    ap.add_argument("--prompt", default=PROMPT,
                    help="versioned facet prompt (default: data/prompts/facets-v1.md)")
    ap.add_argument("--limit", type=int, help="stop after N titles (smoke tests)")
    ap.add_argument("--max-chars", type=int, default=0,
                    help="truncate the article state (0 = whole article, the default and the intent)")
    ap.add_argument("--workers", type=int, default=8,
                    help="concurrent calls. The documented ceiling is 1,200/min; 8 sits well under it.")
    ap.add_argument("--model", default=MODEL, help=f"System One model (default: {MODEL})")
    ap.add_argument("--allow-legacy-resume", action="store_true",
                    help="resume old rows without provenance hashes; unsafe except for a known frozen run")
    return ap


def main(argv=None):
    args = argument_parser().parse_args(argv)
    run_started_at = datetime.now(timezone.utc).isoformat()
    question_set = os.path.basename(args.prompt)
    questions = {**facet_questions(args.prompt), **VALIDITY}
    non_choice = [name for name, q in questions.items() if q.get("type") != "choice"]
    if non_choice:
        raise SystemExit(f"run_facets is Choice-only; unsupported questions: {', '.join(non_choice)}")
    questions_json = json.dumps(questions, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    questions_sha256 = sha256_text(questions_json)
    with open(args.prompt, encoding="utf-8") as fh:
        prompt_sha256 = sha256_text(fh.read())

    done = {}
    if os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as fh:
            lines = fh.readlines()
            for line_number, line in enumerate(lines, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    raise SystemExit(f"{args.out}:{line_number}: malformed JSON; preserve and repair it "
                                     "explicitly before resuming")
                was = row.get("questionSet", "facets-v1.md")
                if was != question_set:
                    raise SystemExit(f"{args.out} contains {was}, cannot resume it with {question_set}")
                legacy = not row.get("questionsSha256") or not row.get("model")
                if legacy and not args.allow_legacy_resume:
                    raise SystemExit(f"{args.out} contains legacy rows without question/model provenance; "
                                     "use a new output or explicitly pass --allow-legacy-resume")
                if not legacy and (row["questionsSha256"] != questions_sha256
                                   or row["model"] != args.model):
                    raise SystemExit(f"{args.out} was produced by different questions or model; "
                                     "use a new output")
                key = f"{row['mediaType']}:{row['tmdbId']}"
                if key in done:
                    raise SystemExit(f"{args.out} contains duplicate key {key}")
                done[key] = row
        print(f"  resuming: {len(done):,} already classified", file=sys.stderr)

    todo = []
    input_keys = set()
    with open(args.articles, encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, 1):
            rec = json.loads(line)
            key = f"{rec['mediaType']}:{rec['tmdbId']}"
            if key in input_keys:
                raise SystemExit(f"{args.articles}:{line_number}: duplicate key {key}")
            input_keys.add(key)
            if key in done:
                prior = done[key]
                prior_hash = prior.get("articleSha256")
                if prior_hash and prior_hash != sha256_text(rec["text"]):
                    raise SystemExit(f"{args.out} has {key} for different article text; use a new output")
                if prior.get("articleRevId") is not None and prior["articleRevId"] != rec.get("revId"):
                    raise SystemExit(f"{args.out} has {key} for a different article revision; use a new output")
                state = state_for(rec, args.max_chars)
                if prior.get("stateSha256") and prior["stateSha256"] != sha256_text(state):
                    raise SystemExit(f"{args.out} has {key} for different effective state; use a new output")
                continue
            todo.append(rec)
    extra = set(done) - input_keys
    if extra:
        raise SystemExit(f"{args.out} contains {len(extra)} keys absent from {args.articles}; use a new output")
    if args.limit:
        todo = todo[:args.limit]
    if not todo:
        print("nothing to do", file=sys.stderr)
        return 0

    print(f"  {len(todo):,} titles · {len(questions)} questions per call "
          f"({len(json.dumps(questions)):,} chars of questions)", file=sys.stderr)

    client = TypeSafe(model=args.model)
    lock = threading.Lock()
    out_fh = open(args.out, "a", encoding="utf-8")
    written = failed = 0

    def work(rec):
        nonlocal written, failed
        state = state_for(rec, args.max_chars)
        try:
            answers = client.ask(state, questions)
            validate_answers(answers, questions)
        except (TypeSafeError, ValueError) as exc:
            with lock:
                failed += 1
                print(f"  FAILED {rec['mediaType']}:{rec['tmdbId']} {rec.get('title')}: {exc}",
                      file=sys.stderr)
            return
        truncated = bool(args.max_chars and len(rec["text"]) > args.max_chars)
        row = answer_row(rec, answers, question_set, questions_sha256, prompt_sha256, args.model,
                         run_started_at, state, truncated)
        with lock:
            out_fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            out_fh.flush()       # a kill costs the calls in flight, not the run
            written += 1
            if written % 250 == 0:
                print(f"  {written:,}/{len(todo):,} · {client.summary()}", file=sys.stderr)

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(work, todo))
    finally:
        out_fh.close()

    print(json.dumps({"written": written, "failed": failed, "calls": client.calls,
                      "inputTokens": client.input_tokens, "spendUSD": round(client.spend, 4),
                      "out": args.out}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
