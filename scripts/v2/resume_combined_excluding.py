#!/usr/bin/env python3
"""Resume a frozen combined run while quarantining explicit capacity exceptions.

This wrapper does not change classification semantics or manifest configuration.  It imports the frozen
runner and changes only the selected-record set, like ``--only-key`` and ``--limit`` already do.  Excluded
keys must be completed in a separately manifested shard and verified with ``audit_combined_bundle.py``.
"""
import json
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from pipeline.combined_questions import global_questions
from pipeline.run_combined import (acquire_output_lock, argument_parser, article_key, attach_enriched_evidence,
                                   load_articles, paid_run, release_output_lock)


def main(argv=None):
    parser = argument_parser()
    parser.add_argument(
        "--exclude-key", action="append", required=True,
        help="exact movie:<id>/tv:<id> key to quarantine in a separate manifested shard",
    )
    args = parser.parse_args(argv)
    if args.plan or args.only_key is not None or args.limit is not None:
        raise SystemExit("resume exclusion cannot be combined with --plan, --only-key, or --limit")

    manifest_path = args.manifest or args.out + ".manifest.json"
    if not os.path.isfile(manifest_path):
        raise SystemExit(f"refusing to initiate a run: existing manifest required at {manifest_path}")

    excluded = set(args.exclude_key)
    global_qs, label_mapping, tax = global_questions(args.prompt, args.taxonomy)
    records, input_keys = load_articles(args.articles)
    absent = excluded - input_keys
    if absent:
        raise SystemExit(f"excluded keys absent from article input: {sorted(absent)}")
    enriched_sha = attach_enriched_evidence(records, args.enriched_dir)
    selected = [record for record in records if article_key(record) not in excluded]

    lock_handle = acquire_output_lock(args.out + ".lock")
    try:
        existing = set()
        if os.path.isfile(args.out):
            with open(args.out, encoding="utf-8") as fh:
                for line_number, line in enumerate(fh, 1):
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise SystemExit(f"{args.out}:{line_number}: malformed JSON: {exc}") from None
                    existing.add(f"{row.get('mediaType')}:{row.get('tmdbId')}")
        overlap = excluded & existing
        if overlap:
            raise SystemExit(
                f"excluded keys already present in original output; separate shard would duplicate: "
                f"{sorted(overlap)}"
            )
        print(f"  quarantining {len(excluded)} key(s) for a separate manifested shard", file=sys.stderr)
        return paid_run(
            args, global_qs, label_mapping, tax, enriched_sha, records, input_keys, selected,
        )
    finally:
        release_output_lock(lock_handle)


if __name__ == "__main__":
    sys.exit(main())
