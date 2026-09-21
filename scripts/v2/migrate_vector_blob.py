#!/usr/bin/env python3
"""One-time: rewrite a v1 vector blob as `DENVEC02`, prepending the keys it always implied.

    scripts/v2/migrate_vector_blob.py --blob out/vectors-bge-m3.bin \
        --labels out/labels-t02.json --out out-migrated/vectors-bge-m3.bin

REWRITES, never re-embeds. den-embed's output differs by BUILD HOST — an arm64 laptop and the x86_64 box
produce different vectors for the same text — so re-running the embedder to "regenerate with keys" would
change the values, not just the layout, and silently replace a measured index with a different one. The
rows are copied through byte for byte and the copy is verified against the original before anything is
written to `--out`.

`--labels` is the order we trust TODAY: the record order that v1's positional join already used, and the
only record of it that exists. This script is the last thing that will ever need it for that — after the
keys are in the file, the join is by key and nothing reads the order again.
"""
import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vector_blob  # noqa: E402
from build_store import labels_by_key  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--blob", required=True, help="the v1 .bin to convert")
    ap.add_argument("--labels", required=True,
                    help="the labels-*.json whose RECORD ORDER this blob is aligned to")
    ap.add_argument("--out", required=True, help="where to write the DENVEC02 copy (never in place)")
    args = ap.parse_args()

    if os.path.abspath(args.out) == os.path.abspath(args.blob):
        sys.exit(f"--out is --blob ({args.blob}) — this writes a new file, it does not convert in place")

    count, dim, keys, blob, base = vector_blob.read(args.blob, allow_legacy=True)
    if keys is not None:
        sys.exit(f"{args.blob} is already a {vector_blob.MAGIC.decode()} blob — nothing to migrate")
    order = list(labels_by_key(args.labels, "labels"))
    if len(order) != count:
        sys.exit(f"{args.labels} lists {len(order)} records but {args.blob} holds {count} rows. These are "
                 f"not the same generation, and the record order is the ONLY thing that says which title "
                 f"each row belongs to — refusing to guess.")

    payload = blob[base:]
    vector_blob.write(args.out, order, payload, dim)

    # Verify by reading the written file back, not by trusting the bytes we just handed to write().
    out_count, out_dim, out_keys, out_blob, out_base = vector_blob.read(args.out)
    same = (out_count == count and out_dim == dim and out_keys == order
            and out_blob[out_base:] == payload)
    print(json.dumps({
        "blob": args.blob, "out": args.out, "rows": count, "dims": dim,
        "labels": args.labels,
        "vectorsSha256Before": hashlib.sha256(payload).hexdigest(),
        "vectorsSha256After": hashlib.sha256(out_blob[out_base:]).hexdigest(),
        "vectorsByteIdentical": same,
        "bytesBefore": len(blob), "bytesAfter": len(out_blob),
        "keyColumnBytes": count * vector_blob.KEY_BYTES,
        "firstKey": order[0], "lastKey": order[-1],
    }, indent=1))
    if not same:
        os.remove(args.out)
        sys.exit("the rewritten blob does not match the original row for row — removed it, nothing shipped")


if __name__ == "__main__":
    main()
