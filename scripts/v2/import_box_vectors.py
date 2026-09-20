#!/usr/bin/env python3
"""Turn `embed_docs.py` output from the serving box into an index store `finalize` can read.

  scripts/v2/import_box_vectors.py --vectors box-vectors.jsonl --labels out-repass/labels-t02.json \
      --out-dir out-repass/index --embedder-health '{"runtime":"den-embed/5.1.2",…}'

`embed_docs.py` writes `{"key": "movie:11", "v": [...]}` — keyed, because the file travels between machines
and 1,097 ids in this corpus are both a film and a series. The index store is a different shape: two
append-only files, `labels.jsonl` and `vectors.jsonl`, **zipped positionally** — line *i* of one describes
line *i* of the other — and a vector row is `{"tmdbId": 11, "v": [...]}` with no mediaType at all.

So this is a join followed by a deliberate discard of the only field that could detect a bad join. That is
why every check below runs BEFORE anything is written:

  - every vector key must exist in --labels (an unknown key means the two sides disagree about the corpus)
  - no duplicate keys (a key embedded twice would silently take one vector for both rows)
  - every vector must be 1024 ints in int8 range (a truncated or float row fails much later, as nonsense)

`StoreIntegrity.alignedPrefix` re-checks the pairing on the Swift side, but only on `tmdbId`, so a movie
row paired with the series of the same id passes it. This script is the only place the mediaType half of
the key is still present, which makes it the only place that mistake can be caught.

Row ORDER is taken from the vector file, not from the labels artifact: those vectors were written in the
order the box embedded them, and re-ordering them here would be a second chance to get the pairing wrong
for no gain. The labels file is reordered to match.

## Records with no vector are reported, never dropped silently

A title in --labels that the box never embedded (no plot, so no document was composed) simply does not
appear in the store. That is correct — but it is also how a much larger loss would look, so the count is
printed and compared against --expect-missing when given.
"""
import argparse
import json
import os
import sys

DIMS = 1024


def die(msg):
    sys.exit(f"refusing to write: {msg}")


ap = argparse.ArgumentParser()
ap.add_argument("--vectors", required=True, help="vectors.jsonl as embed_docs.py wrote it on the box")
ap.add_argument("--labels", required=True, help="labels-t02.json — the record per title")
ap.add_argument("--out-dir", required=True, help="the index dir to write labels.jsonl + vectors.jsonl into")
ap.add_argument("--embedder-health", help="the serving den-embed's /health JSON, recorded as embedder.json")
ap.add_argument("--expect-missing", type=int,
                help="how many labelled titles are expected to have no vector; refuse if more")
ap.add_argument("--dry-run", action="store_true", help="run every check and report, but write nothing")
args = ap.parse_args()

store = json.load(open(args.labels, encoding="utf-8"))
records = {f"{r['mediaType']}:{r['tmdbId']}": r for r in store["records"]}
if len(records) != len(store["records"]):
    die(f"{args.labels} holds duplicate mediaType:tmdbId keys")

rows, seen = [], set()
with open(args.vectors, encoding="utf-8") as fh:
    for n, line in enumerate(fh, 1):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError as e:
            die(f"{args.vectors}:{n} is not JSON ({e}) — a killed run leaves a half-written final line")
        key, v = r.get("key"), r.get("v")
        if key in seen:
            die(f"{args.vectors}:{n} repeats key {key!r}; one of the two vectors would be lost")
        if key not in records:
            die(f"{args.vectors}:{n} has key {key!r}, which is not in {args.labels}")
        if not isinstance(v, list) or len(v) != DIMS:
            die(f"{args.vectors}:{n} ({key}) has {len(v) if isinstance(v, list) else type(v).__name__} "
                f"values, expected {DIMS}")
        if not all(isinstance(x, int) and -128 <= x <= 127 for x in v):
            die(f"{args.vectors}:{n} ({key}) is not int8 — a float or out-of-range row is a different "
                f"quantisation, not a rounding difference")
        seen.add(key)
        rows.append((key, v))

missing = sorted(set(records) - seen)
print(json.dumps({
    "vectors": len(rows), "labelled": len(records), "labelledWithoutVector": len(missing),
    "missingSample": missing[:8],
}, indent=2))
if args.expect_missing is not None and len(missing) > args.expect_missing:
    die(f"{len(missing)} labelled titles have no vector, more than the {args.expect_missing} expected")

if args.dry_run:
    print("dry run: nothing written")
    sys.exit(0)

os.makedirs(args.out_dir, exist_ok=True)
labels_path = os.path.join(args.out_dir, "labels.jsonl")
vectors_path = os.path.join(args.out_dir, "vectors.jsonl")

# Written together, in one pass, in the vector file's own order — the two files are only ever correct as a
# pair, so neither is left on disk in a state the other does not match.
with open(labels_path, "w", encoding="utf-8") as lf, open(vectors_path, "w", encoding="utf-8") as vf:
    for key, v in rows:
        rec = records[key]
        lf.write(json.dumps(rec, sort_keys=True) + "\n")
        vf.write(json.dumps({"tmdbId": rec["tmdbId"], "v": v}) + "\n")

if args.embedder_health:
    h = json.loads(args.embedder_health)
    # embedder.json is what later runs are gated against, so it records the service that ACTUALLY produced
    # these vectors. Writing the value we wish were true is how the 5.1.1/5.1.2 drift went unnoticed.
    json.dump({"dims": h["dims"], "maxTokens": h["max_tokens"], "model": h["model"],
               "runtime": h["runtime"], "vectorEpoch": h["vector_epoch"]},
              open(os.path.join(args.out_dir, "embedder.json"), "w"), indent=1, sort_keys=True)

print(json.dumps({"wrote": len(rows), "labels": labels_path, "vectors": vectors_path}, indent=2))
