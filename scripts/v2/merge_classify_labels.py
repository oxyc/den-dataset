#!/usr/bin/env python3
"""Fold the classification run into `labels-t02.json`.

  scripts/v2/merge_classify_labels.py --phase out-repass/classify \
      --labels out-repass/labels-t02.json --out out-repass/labels-t02.json

The run labelled the 9,010 titles that have a Wikipedia plot and no labels. This adds them to the
file the embed stage reads, in that file's own record shape.

## What is dropped on the way in

`validate_classify_batch.py` reports these; this is where they are acted on, so the two must agree:

  - a row whose `primary_genre` is missing or outside the vocabulary — the row carries no usable
    label, so it is not written. `build_classify_backfill.py` sweeps it.
  - a subgenre or mood outside the vocabulary, with a confidence out of range, below the 0.5 floor,
    or repeated within its own list — that ENTRY is dropped, the row is kept.
  - anything past the third subgenre or mood, weakest first.

A batch with invented or duplicated keys is refused outright, because a reconstructed key silently
mis-labels a title (movie 95 is *Armageddon*, tv 95 is *Buffy*).

## Coverage cannot shrink

Every key already in `--labels` is still there afterwards, and the count is checked against the
input rather than trusted. The premise merge would have DROPPED 999 titles silently — it had every
title it was asked about and no way to notice the ones it was not asked about. The guard below is
that lesson: a merge that ends with fewer titles than it started is a bug, not a result.
"""
import argparse
import json
import os
import sys

MAX_SUB, MAX_MOOD = 3, 3


def clean(items, allowed, cap):
    """Vocabulary, range, floor, duplicates, cap — the validator's `quality` rules, applied."""
    out, seen = [], set()
    for it in items or []:
        if not isinstance(it, dict) or "label" not in it:
            continue
        label, conf = it["label"], it.get("confidence")
        if label in seen or label not in allowed:
            continue
        if not isinstance(conf, (int, float)) or not 0.5 <= conf <= 1:
            continue
        seen.add(label)
        out.append({"label": label, "confidence": conf})
    out.sort(key=lambda x: -x["confidence"])
    return out[:cap]


ap = argparse.ArgumentParser()
ap.add_argument("--phase", required=True)
ap.add_argument("--labels", required=True, help="the store to extend; read, never truncated in place")
ap.add_argument("--out", required=True)
# `source` is checked against `pipeline/finalize.py`'s SOURCES by the embed and finalize stages, which
# accept only llm/recipe/wikidata/cluster. A descriptive value like "llm-t02-classify" writes fine here and
# then fails every later read of the store with a refusal naming a record index rather than the cause.
SOURCES = ("llm", "recipe", "wikidata", "cluster")
ap.add_argument("--source", default="llm", choices=SOURCES)
args = ap.parse_args()

vocab = json.load(open(os.path.join(args.phase, "vocab.json"), encoding="utf-8"))
pg_ok, sg_ok, md_ok = (set(vocab["primary_genre"]), set(vocab["subgenres"]), set(vocab["moods"]))

store = json.load(open(args.labels, encoding="utf-8"))
existing = {f"{r['mediaType']}:{r['tmdbId']}": r for r in store["records"]}
before = len(existing)

in_dir, out_dir = os.path.join(args.phase, "in"), os.path.join(args.phase, "out")
asked = set()
for name in sorted(os.listdir(in_dir)):
    if name.startswith("batch-") and name.endswith(".json"):
        asked |= {r["key"] for r in json.load(open(os.path.join(in_dir, name), encoding="utf-8"))}

added, replaced, dropped, refused = {}, 0, [], []
for name in sorted(os.listdir(out_dir)):
    if not (name.startswith("batch-") and name.endswith(".json")):
        continue
    path = os.path.join(out_dir, name)
    try:
        rows = json.load(open(path, encoding="utf-8"))
    except json.JSONDecodeError as e:
        refused.append(f"{name}: unreadable ({e})")
        continue
    if not isinstance(rows, list):
        refused.append(f"{name}: not a JSON array")
        continue

    keys = [r.get("key") for r in rows if isinstance(r, dict)]
    invented = sorted(set(keys) - asked)
    if invented:
        refused.append(f"{name}: invented keys {invented[:4]}")
        continue
    if len(set(keys)) != len(keys):
        refused.append(f"{name}: duplicated keys {sorted({k for k in keys if keys.count(k) > 1})}")
        continue

    for row in rows:
        if not isinstance(row, dict):
            continue
        key, pg = row.get("key"), row.get("primary_genre")
        if pg not in pg_ok:
            dropped.append(f"{key}: primary_genre {pg!r}")
            continue
        media, _, tmdb = key.partition(":")
        added[key] = {
            "animated": bool(row.get("animated")),
            "mediaType": media,
            "moods": clean(row.get("moods"), md_ok, MAX_MOOD),
            "primaryGenre": pg,
            "source": args.source,
            "subgenres": clean(row.get("subgenres"), sg_ok, MAX_SUB),
            "tmdbId": int(tmdb),
        }

if refused:
    for r in refused:
        print(f"REFUSED {r}", file=sys.stderr)
    sys.exit(f"{len(refused)} batch(es) refused; nothing written. Re-run them, then merge.")

for key, record in added.items():
    if key in existing:
        replaced += 1
    existing[key] = record

# The guard. A merge that loses a title is a bug; say so before writing, not after.
if len(existing) < before:
    sys.exit(f"refusing to write: {before} titles in, {len(existing)} out — the merge LOST titles.")

records = sorted(existing.values(), key=lambda r: (r["mediaType"], r["tmdbId"]))
store["records"], store["count"] = records, len(records)
json.dump(store, open(args.out, "w", encoding="utf-8"), indent=1, sort_keys=True)

print(json.dumps({
    "titlesAsked": len(asked), "titlesLabelled": len(added),
    "rowsDroppedNoUsableGenre": len(dropped), "dropSample": dropped[:6],
    "alreadyPresentAndReplaced": replaced,
    "storeBefore": before, "storeAfter": len(records), "out": args.out,
}, indent=2))
