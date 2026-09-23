#!/usr/bin/env python3
"""Collect the titles the generation run dropped, and re-batch them.

  pipeline/build_premise_backfill.py --phase out-premise-v2/gen [--per-batch 22]

A generating pass returns a correct-looking answer that is short: 22 works in, 20 out, and a report that
says "all works processed". `validate_premise_batch.py` catches that — the key set is the only evidence
worth trusting — but catching it per batch leaves the question of what to do with the survivors, and
re-running a whole batch to recover two titles pays for twenty that are already on disk.

So the recovery is per TITLE, not per batch: every key that appears in an input and not in its output is
swept into fresh batches here, whatever batch it came from. That also means a single sweep handles drops
from every wave at once, and running it twice is harmless — the second run finds only what the first run's
batches still missed.

Backfill batches are numbered from `--start` (default 9000) so they cannot collide with the original
worklist's numbering, and they are written into the same `in/` directory, so the generator prompt, the
validator and the eventual merge all treat them as ordinary batches.
"""
import argparse
import json
import os

ap = argparse.ArgumentParser()
ap.add_argument("--phase", required=True, help="the gen/ directory holding in/ and out/")
ap.add_argument("--per-batch", type=int, default=22)
ap.add_argument("--start", type=int, default=9000, help="first backfill batch number")
ap.add_argument("--dry-run", action="store_true")
args = ap.parse_args()

in_dir, out_dir = os.path.join(args.phase, "in"), os.path.join(args.phase, "out")

# Every input row, by key, so a dropped title can be re-sent with the evidence it was meant to be judged on.
rows, produced, no_output = {}, set(), []
for name in sorted(os.listdir(in_dir)):
    if not name.startswith("batch-") or not name.endswith(".json"):
        continue
    batch = json.load(open(os.path.join(in_dir, name), encoding="utf-8"))
    for r in batch:
        rows[r["key"]] = r
    out_path = os.path.join(out_dir, name)
    if not os.path.exists(out_path):
        no_output.append(name)
        continue
    try:
        got = json.load(open(out_path, encoding="utf-8"))
    except json.JSONDecodeError:
        # A half-written or malformed output is not evidence that its titles were done.
        no_output.append(name)
        continue
    produced |= {r.get("key") for r in got if isinstance(r, dict)}

# A batch with no output at all is a different failure (never dispatched, or still in flight) and re-batching
# its titles here would race the agent that may still be writing it. Name them; do not sweep them.
swept = []
for name in sorted(os.listdir(in_dir)):
    if not name.startswith("batch-") or name in no_output:
        continue
    out_path = os.path.join(out_dir, name)
    if not os.path.exists(out_path):
        continue
    batch = json.load(open(os.path.join(in_dir, name), encoding="utf-8"))
    swept.extend(r for r in batch if r["key"] not in produced)

batches = [swept[i:i + args.per_batch] for i in range(0, len(swept), args.per_batch)]
written = []
if not args.dry_run:
    for i, slice_ in enumerate(batches):
        path = os.path.join(in_dir, f"batch-{args.start + i:04d}.json")
        json.dump(slice_, open(path, "w", encoding="utf-8"), ensure_ascii=False)
        written.append(os.path.basename(path))

print(json.dumps({
    "titlesInWorklist": len(rows),
    "titlesProduced": len(produced),
    "titlesToBackfill": len(swept),
    "backfillBatches": len(batches),
    "written": written,
    # Mid-run this is mostly batches not dispatched yet, so print a count and a sample rather than
    # a hundred filenames; at the end of the run a non-empty list here is the thing to act on.
    "batchesWithNoUsableOutput": len(no_output),
    "noUsableOutputSample": no_output[:12],
}, indent=2))
