#!/usr/bin/env python3
"""Collect the titles a classification run dropped, and re-batch them.

  scripts/v2/build_classify_backfill.py --phase out-repass/classify [--per-batch 20] [--dry-run]

Same shape and same reason as `build_premise_backfill.py`: a pass returns 16 rows for a 20-row batch and
reports "16 works" as though that were the input. Re-running the whole batch to recover 4 titles pays again
for 16 that are already correct, so recovery is per TITLE — every key present in an input and absent from
its output, swept into fresh batches numbered from `--start`, whatever batch they came from.

Running it twice is harmless: the second pass finds only what the first one's batches still missed.

A batch with NO output is a different failure — never dispatched, or still in flight — and its titles are
NOT swept, because re-batching them would race an agent that may still be writing. Those are counted and
named instead.
"""
import argparse
import json
import os

ap = argparse.ArgumentParser()
ap.add_argument("--phase", required=True)
ap.add_argument("--per-batch", type=int, default=20)
ap.add_argument("--start", type=int, default=9000)
ap.add_argument("--dry-run", action="store_true")
args = ap.parse_args()

in_dir, out_dir = os.path.join(args.phase, "in"), os.path.join(args.phase, "out")

rows, produced, no_output = {}, set(), []
for name in sorted(os.listdir(in_dir)):
    if not (name.startswith("batch-") and name.endswith(".json")):
        continue
    for r in json.load(open(os.path.join(in_dir, name), encoding="utf-8")):
        rows[r["key"]] = r
    out_path = os.path.join(out_dir, name)
    if not os.path.exists(out_path):
        no_output.append(name)
        continue
    try:
        got = json.load(open(out_path, encoding="utf-8"))
    except json.JSONDecodeError:
        no_output.append(name)   # half-written; its titles are not done
        continue
    produced |= {r.get("key") for r in got if isinstance(r, dict)}

swept = []
for name in sorted(os.listdir(in_dir)):
    if not (name.startswith("batch-") and name.endswith(".json")) or name in no_output:
        continue
    if not os.path.exists(os.path.join(out_dir, name)):
        continue
    swept.extend(r for r in json.load(open(os.path.join(in_dir, name), encoding="utf-8"))
                 if r["key"] not in produced)

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
    "batchesWithNoUsableOutput": len(no_output),
    "noUsableOutputSample": no_output[:12],
}, indent=2))
