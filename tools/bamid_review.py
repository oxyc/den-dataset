#!/usr/bin/env python3
"""Compatibility entry point for the generic maturity review queue's BAMID provider."""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import cache  # noqa: E402
from pipeline.contract import StageError  # noqa: E402
from tools import maturity_review  # noqa: E402


def build(facts_path, snapshot_path, limit=200, seed="bamid-review-v1", decisions=None):
    return maturity_review.build(facts_path, snapshot_path, "BAMID", limit, seed, decisions)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--facts", required=True)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--seed", default="bamid-review-v1")
    args = parser.parse_args(argv)
    try:
        report = build(args.facts, args.snapshot, args.limit, args.seed)
    except (OSError, ValueError, StageError) as refusal:
        parser.error(str(refusal))
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    cache.write_atomically(args.out, (json.dumps(report, ensure_ascii=False, indent=2,
                                                 sort_keys=True) + "\n").encode())
    print(json.dumps(report["summary"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
