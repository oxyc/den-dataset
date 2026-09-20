#!/usr/bin/env python3
"""Run the delta question set over an article dump, reusing `run_combined`'s machinery.

  scripts/v2/run_delta.py --articles out-repass/delta-pilot-articles.jsonl \
      --out out-repass/delta-pilot.jsonl --model jev-1.13.0 [--plan]

Only the QUESTIONS differ from the corpus pass. State building, validation, resume, the output lock and
the manifest are `run_combined`'s, unchanged — a second implementation of any of those is a second thing to
get wrong, and the corpus run already proved these.

The delta asks no per-section questions: the section roles were bought by the corpus run and re-asking them
would pay for 27 answers a title to learn what is already on disk.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_combined as rc  # noqa: E402
from combined_questions import taxonomy  # noqa: E402
from delta_questions import delta_questions  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--articles", required=True)
    parser.add_argument("--enriched-dir")
    parser.add_argument("--out", required=True)
    parser.add_argument("--manifest")
    parser.add_argument("--model", default="jev-1.13.0")
    # The manifest records the prompt and taxonomy that produced the answers; the delta uses the same
    # prompt as the corpus run so the two sets are comparable.
    parser.add_argument("--prompt", default=rc.PROMPT)
    parser.add_argument("--taxonomy", default=rc.TAXONOMY)
    parser.add_argument("--allow-mutable-model", action="store_true")
    parser.add_argument("--max-state-chars", type=int, default=rc.DEFAULT_MAX_STATE_CHARS)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--only-key")
    parser.add_argument("--plan", action="store_true", help="validate and report; make no API calls")
    args = parser.parse_args(argv)

    if args.model.endswith("-latest") and not args.allow_mutable_model:
        raise SystemExit("refusing mutable model alias; use a pinned model or --allow-mutable-model")

    questions = delta_questions()
    records, input_keys = rc.load_articles(args.articles)
    enriched_sha = rc.attach_enriched_evidence(records, args.enriched_dir)
    selected = records[: args.limit] if args.limit else records
    if args.only_key:
        selected = [r for r in records if rc.article_key(r) == args.only_key]
        if not selected:
            raise SystemExit(f"--only-key {args.only_key} is absent from the article input")

    if args.plan:
        print(json.dumps(rc.plan(selected, questions, args.max_state_chars), ensure_ascii=False, indent=2))
        return 0

    # No section questions in the delta, so nothing consults `sections_for_record`. Patching it to return
    # nothing keeps `classify` on its existing path rather than forking it.
    rc.sections_for_record = lambda rec: []

    lock = rc.acquire_output_lock(args.out + ".lock")
    try:
        # The real taxonomy, so the manifest records which vocabulary version these answers sit beside
        # even though the delta asks none of its labels.
        return rc.paid_run(args, questions, {}, taxonomy(args.taxonomy), enriched_sha, records,
                           input_keys, selected)
    finally:
        rc.release_output_lock(lock)


if __name__ == "__main__":
    raise SystemExit(main())
