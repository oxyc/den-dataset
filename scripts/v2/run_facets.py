#!/usr/bin/env python3
"""Classify every grounded title's facets with a System One model, one call per title.

  taxonomy-backfill dump-articles --enriched-dir out-repass/enriched --out out-repass/articles.jsonl
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

The nine facet axes (`facet_questions.py`, parsed from `prompts/facets-v1.md`) plus `validity`, which is
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
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from facet_questions import questions as facet_questions          # noqa: E402
from typesafe_client import TypeSafe, TypeSafeError               # noqa: E402

VALIDITY = {
    "validity": {
        "type": "choice",
        "instructions": ("What does this article describe? Judge from the article itself — its opening "
                         "sentence usually says outright what kind of work it is about."),
        "criteria": {
            "single-work": "One film, or one television series. The ordinary case.",
            "source-work": ("The novel, play, comic or game that a screen work was adapted FROM, rather "
                            "than the adaptation. Often signalled by an opening like 'is an 1847 novel by'."),
            "franchise-overview": "A franchise, series of films, or shared universe covering several works.",
            "season-aggregation": ("A season or episode list for a serial, where the plot is per-season "
                                   "rather than one story. Correct for television, wrong for a film."),
            "not-a-work": "A person, a disambiguation page, or something else entirely.",
        },
    },
}


def answer_row(rec, answers):
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
        "articleChars": rec.get("chars"), "facets": facets,
    }


ap = argparse.ArgumentParser()
ap.add_argument("--articles", required=True, help="JSONL from `taxonomy-backfill dump-articles`")
ap.add_argument("--out", required=True, help="JSONL, appended to; re-running resumes from it")
ap.add_argument("--limit", type=int, help="stop after N titles (smoke tests)")
ap.add_argument("--max-chars", type=int, default=0,
                help="truncate the article state (0 = whole article, the default and the intent)")
ap.add_argument("--workers", type=int, default=8,
                help="concurrent calls. The documented ceiling is 1,200/min; 8 sits well under it.")
args = ap.parse_args()

done = set()
if os.path.exists(args.out):
    with open(args.out, encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
                done.add(f"{r['mediaType']}:{r['tmdbId']}")
            except Exception:
                continue          # a torn last line from a kill; it will simply be redone
    print(f"  resuming: {len(done):,} already classified", file=sys.stderr)

todo = []
with open(args.articles, encoding="utf-8") as fh:
    for line in fh:
        rec = json.loads(line)
        if f"{rec['mediaType']}:{rec['tmdbId']}" in done:
            continue
        todo.append(rec)
if args.limit:
    todo = todo[:args.limit]
if not todo:
    sys.exit("nothing to do")

QUESTIONS = {**facet_questions(), **VALIDITY}
print(f"  {len(todo):,} titles · {len(QUESTIONS)} questions per call "
      f"({len(json.dumps(QUESTIONS)):,} chars of questions)", file=sys.stderr)

client = TypeSafe()
lock = threading.Lock()
out_fh = open(args.out, "a", encoding="utf-8")
written = failed = 0


def work(rec):
    global written, failed
    state = rec["text"][:args.max_chars] if args.max_chars else rec["text"]
    try:
        answers = client.ask(state, QUESTIONS)
    except TypeSafeError as exc:
        with lock:
            failed += 1
            print(f"  FAILED {rec['mediaType']}:{rec['tmdbId']} {rec.get('title')}: {exc}", file=sys.stderr)
        return
    row = answer_row(rec, answers)
    with lock:
        out_fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        out_fh.flush()           # a kill costs the calls in flight, not the run
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
