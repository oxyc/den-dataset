#!/usr/bin/env python3
"""Buy the structural-affinity probability profile without repeating the existing delta questions.

Uses `run_delta`'s state/provenance/spend machinery because only the question set and output artifact differ:

  pipeline/run_structural.py --articles out-repass/articles.jsonl \
      --combined out-repass/combined-v1-r2.jsonl \
      --combined out-repass/combined-v1-r2-token-fallback.jsonl \
      --combined out-repass/combined-v1-r2-token-fallback-2.jsonl \
      --out out-repass/structural-v1.jsonl --model jev-1.13.0 --plan

Replace `--plan` with `--spend` only after reading its estimate. The pass asks 18 typed Nouls and generates
no text. It is separate from delta so the 33 critique/depiction/audience/technique questions already bought
for the corpus are never paid for twice.
"""
import os
import sys

if not __package__:
    sys.path[0] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from pipeline.run_delta import run_questions  # noqa: E402
from pipeline.structural_questions import structural_questions  # noqa: E402


def main(argv=None):
    return run_questions(structural_questions(), argv)


if __name__ == "__main__":
    raise SystemExit(main())
