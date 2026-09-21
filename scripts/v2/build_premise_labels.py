#!/usr/bin/env python3
"""Write `labels-premise.json` in the premise blob's own row order.

  scripts/v2/build_premise_labels.py --ids out-premise-v2/vectors/premise-v2-ids.json \
      --labels out-repass/labels-t02.json --blob out-premise-v2/vectors/vectors-premise-v2.bin \
      --out out-repass/labels-premise.json

The premise index is a separate vector space from the plot index and carries its own row order. A
`DENVEC02` blob records that order itself, one u64 key per row; the blobs that predate it do not, and their
order lives only in the ids file written beside them. Pairing the wrong sidecar with the wrong blob
produces an index that loads cleanly and returns nonsense — which is why the keys moved into the file.

So this is the sidecar for that blob: record *i* here describes row *i* there, and the order is taken from
the ids file rather than re-derived, because re-deriving it is the mistake.

## Why it is rebuilt rather than shipped as-is

The premise ids cover 44,531 titles; the previous `labels-premise.json` held 38,532 and was missing 5,999 of
them outright. Those are titles that had a plot but no labels until the classification pass, so the record
now exists and the row can carry real labels instead of being absent.

## Checks

The header count is read from the blob and asserted against the ids file, and where the blob names its own
rows the ids file must BE that order — the one pairing error that is cheap to catch and fatal to miss. A
missing record is refused rather than skipped: dropping one would shift every row after it by one, which
is precisely the silent misalignment above.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vector_blob  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--ids", required=True, help="premise-v2-ids.json — the blob's row order")
ap.add_argument("--labels", required=True, help="labels-t02.json — the record per title")
ap.add_argument("--blob", help="vectors-premise-v2.bin, to assert the header count matches")
ap.add_argument("--out", required=True)
args = ap.parse_args()

ids = json.load(open(args.ids, encoding="utf-8"))
if len(set(ids)) != len(ids):
    sys.exit(f"refusing: {args.ids} repeats a key; the blob's rows would not be addressable")

store = json.load(open(args.labels, encoding="utf-8"))
records = {f"{r['mediaType']}:{r['tmdbId']}": r for r in store["records"]}

if args.blob:
    count, _, blob_keys, _, _ = vector_blob.read(args.blob, allow_legacy=True)
    if count != len(ids):
        sys.exit(f"refusing: {args.blob} holds {count} rows but {args.ids} names {len(ids)} — "
                 f"one of the two is from a different build")
    # A DENVEC02 blob names its rows outright, which turns the count check into an identity check.
    if blob_keys is not None and blob_keys != list(ids):
        sys.exit(f"refusing: {args.blob} names its own rows and they are not {args.ids}'s order")

missing = [k for k in ids if k not in records]
if missing:
    sys.exit(f"refusing: {len(missing)} ids have no record in {args.labels} (e.g. {missing[:5]}). "
             f"Skipping them would shift every row after each one.")

out = {"taxonomyVersion": store["taxonomyVersion"],
       "count": len(ids),
       "records": [records[k] for k in ids]}
json.dump(out, open(args.out, "w", encoding="utf-8"), indent=1, sort_keys=True)

print(json.dumps({"records": len(ids), "blobRows": count if args.blob else None,
                  "taxonomyVersion": out["taxonomyVersion"], "out": args.out}, indent=2))
