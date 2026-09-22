#!/usr/bin/env python3
"""Run the delta question set over an article dump, reusing `run_combined`'s machinery.

  scripts/v2/run_delta.py --articles out-repass/articles.jsonl --enriched-dir out-repass/enriched \
      --combined out-repass/combined-v1-r2.jsonl \
      --combined out-repass/combined-v1-r2-token-fallback.jsonl \
      --combined out-repass/combined-v1-r2-token-fallback-2.jsonl \
      --out out-repass/delta-v2.jsonl --model jev-1.13.0 [--plan | --spend]

**It buys from a paid provider**, so it refuses to call it without `--spend`, the same opt-in `den run`
asks for before the classify pass. Without either flag it prints the plan's estimate and stops. It runs
outside the stage order, so `den run`'s gate never reaches it: the flag is here or nowhere.

Only the QUESTIONS differ from the corpus pass. State building, validation, resume, the output lock and
the manifest are `run_combined`'s, unchanged — a second implementation of any of those is a second thing to
get wrong, and the corpus run already proved these.

The delta asks no per-section questions: the section roles were bought by the corpus run and re-asking them
would pay for 27 answers a title to learn what is already on disk. It still sends the article: each title's
state is the sections the corpus run sent it, read from that run's rows (`--combined`, every shard). That is
the whole article where it fitted, and the role-selected sections where it did not — including the titles the
corpus run could only send under a lower ceiling, which a single ceiling here would get wrong.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_combined as rc  # noqa: E402
from article_sections import sha256_text  # noqa: E402
from combined_questions import taxonomy  # noqa: E402
from delta_questions import delta_questions  # noqa: E402


def corpus_states(paths, records):
    """The section ids the corpus run sent each record as its state, keyed by article key.

    Section ids are positions in one article, so a row is used only when it read the same article with the
    same sections; a row that did not is refused rather than skipped.
    """
    wanted = {rc.article_key(rec): rec for rec in records}
    states = {}
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                row = json.loads(line)
                key = f"{row['mediaType']}:{row['tmdbId']}"
                if key not in wanted:
                    continue
                if key in states:
                    raise SystemExit(f"{path}: {key} appears in more than one --combined shard")
                rec = wanted[key]
                if row.get("articleSha256") != sha256_text(rec["text"]) \
                        or [(s["id"], s["textSha256"]) for s in row["sections"]] \
                        != [(s["id"], s["textSha256"]) for s in rc.sections_for_record(rec)]:
                    raise SystemExit(f"{path}: {key} was classified from a different article than --articles "
                                     "holds, so its state does not describe it")
                states[key] = row["globalStateSectionIds"]
    return states


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--articles", required=True)
    parser.add_argument("--enriched-dir")
    parser.add_argument("--combined", action="append", required=True,
                        help="a shard of the corpus run's rows, repeated for every shard; each title is sent "
                             "the state its row records")
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
    parser.add_argument("--spend", action="store_true",
                        help="buy the answers from the paid provider; without it the run stops at the estimate")
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

    states = corpus_states(args.combined, selected)
    try:
        estimate = rc.plan(selected, questions, args.max_state_chars, state_ids_by_key=states)
    except ValueError as exc:
        raise SystemExit(f"{exc}: every title needs its corpus row, so pass every --combined shard") from None

    if args.plan:
        print(json.dumps(estimate, ensure_ascii=False, indent=2))
        return 0

    if not args.spend:
        print(f"refusing to buy without --spend: this run would pay for {estimate['calls']:,} calls over "
              f"{estimate['titles']:,} titles, roughly ${estimate['roughCostUSD']:,.2f} "
              f"({estimate['estimateCaveat']}). Titles already answered in {args.out} are not bought again.\n"
              f"  --plan prints the whole plan; --spend buys it.", file=sys.stderr)
        return 2

    lock = rc.acquire_output_lock(args.out + ".lock")
    try:
        # The real taxonomy, so the manifest records which vocabulary version these answers sit beside
        # even though the delta asks none of its labels.
        return rc.paid_run(args, questions, {}, taxonomy(args.taxonomy), enriched_sha, records,
                           input_keys, selected, state_ids_by_key=states)
    finally:
        rc.release_output_lock(lock)


if __name__ == "__main__":
    raise SystemExit(main())
