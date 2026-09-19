#!/usr/bin/env python3
"""Fold the v2 generation run into the published premise tags, and say which strings are new.

  scripts/v2/merge_premise_tags.py --phase out-premise-v2/gen --out data/premise-tags-v2.json

v1 is NOT overwritten. `vectors-premise.bin` is aligned to v1's tag strings, so rewriting them in place
would leave every vector describing text that no longer exists — the failure `merge-premise-coverage.py`
is written to avoid, in a form no checksum would catch.

## Keying

`build-premise-tags.py` recovers a title's mediaType POSITIONALLY, because the v1 batches carry a bare
tmdbId and 1,097 ids in this corpus are both a film and a series. This run's batches carry `movie:123` /
`tv:123` keys directly, so there is nothing to recover and nothing to guess. The merge refuses any key it
cannot parse rather than falling back to a bare id.

## The two repairs

`validate_premise_batch.py` reports per-tag problems it deliberately does not reject a batch for, because
they are mechanical:

  * a tag that only needs normalising (`cinecittà-ambition` -> `cinecitta-ambition`) is REPAIRED. The
    index is ASCII; a tag with an accent in it joins against nothing, and dropping it would throw away a
    real premise over a typo.
  * a tag that is nothing but genre words (`romantic-comedy`, `comedy-adventure-sci-fi`) is DROPPED. It
    restates what the taxonomy labels already record, and `data/README.md` credits that ban with why this
    index beats the plot index by +11.3 pp.

A drop can take a title below the spec's 8-tag floor. That is reported, not fixed: re-running the title
costs a model call for something a short tag list still answers, and a title with 7 real tags is worth
more than one with 7 real tags and `romantic-comedy`.

## What gets embedded

Only titles whose composed string CHANGED. A title already in v1 whose v2 tags are identical keeps its
existing vector, so the embed cost is the new titles plus the repaired ones, not the whole corpus.
"""
import argparse
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("v", os.path.join(HERE, "validate_premise_batch.py"))
_v = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_v)


def compose(tags):
    """The embedding document for a title: tags, most-defining first, space separated.

    Identical to the v1 composition (scripts/v2/embed_tags.py) so a v1-vs-v2 comparison measures the tags
    and not the formatting. Hyphens stay — bge-m3 sub-word tokenizes them, and splitting a compound
    premise into its parts measurably blurs it.
    """
    return " ".join(tags)


def clean(tags):
    """Apply the two mechanical repairs; return (kept, repaired, dropped)."""
    kept, repaired, dropped = [], [], []
    for t in tags:
        if not isinstance(t, str) or not t.strip():
            continue
        fixed = _v.normalise(t)
        if fixed != t:
            if not _v.TAG.match(fixed) or _v.has_non_ascii(fixed):
                dropped.append(t)
                continue
            repaired.append((t, fixed))
            t = fixed
        if _v.genre_words(t):
            dropped.append(t)
            continue
        if t not in kept:  # a repair can collide with a tag already present
            kept.append(t)
    return kept, repaired, dropped


ap = argparse.ArgumentParser()
ap.add_argument("--phase", required=True, help="the gen/ directory holding in/ and out/")
ap.add_argument("--v1", default="data/premise-tags-v1.json")
ap.add_argument("--out", required=True)
ap.add_argument("--embed-list", help="write the keys whose string changed, for the embed run")
args = ap.parse_args()

v1 = json.load(open(args.v1, encoding="utf-8"))
old_tags = v1["tags"]

new, repairs, drops, below_floor = {}, 0, 0, []
for name in sorted(os.listdir(os.path.join(args.phase, "out"))):
    if not (name.startswith("batch-") and name.endswith(".json")):
        continue
    for row in json.load(open(os.path.join(args.phase, "out", name), encoding="utf-8")):
        if not isinstance(row, dict):
            continue
        key = row.get("key")
        if not isinstance(key, str) or key.count(":") != 1 or key.split(":")[0] not in ("movie", "tv"):
            sys.exit(f"{name}: unusable key {key!r} — refusing to guess mediaType")
        kept, rep, drp = clean(row.get("tags") or [])
        repairs += len(rep)
        drops += len(drp)
        if not kept:
            sys.exit(f"{name}: {key} has no tags left after cleaning — refusing to write an empty record")
        if len(kept) < _v.MIN_TAGS:
            below_floor.append(key)
        new[key] = kept

merged = dict(old_tags)
overwritten = sum(1 for k in new if k in merged)
merged.update(new)

changed = [k for k, t in merged.items()
           if k not in old_tags or compose(t) != compose(old_tags[k])]

out = {
    "schema": v1["schema"],
    "index": "premise-v2",
    "count": len(merged),
    "derivedFrom": (v1["derivedFrom"] + "; extended by the v2 run over the re-ground Wikipedia corpus, "
                    "spec data/premise-tags-v1.SPEC.md, evidence limited to Jev-classified story-premise "
                    "and theme-subject sections"),
    "embeddedBy": v1["embeddedBy"],
    "supersedes": v1["index"],
    "tags": merged,
    # Carried forward verbatim: these titles have tags and no vector because the DT-N merge never ran, and
    # that is still true of the v2 artifact until scripts/merge-premise-coverage.py runs.
    "coverageFilled": v1.get("coverageFilled", []),
    "vectorsMissingFor": v1.get("vectorsMissingFor", []),
}
os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
json.dump(out, open(args.out, "w", encoding="utf-8"), indent=1, sort_keys=True)

if args.embed_list:
    json.dump(sorted(changed), open(args.embed_list, "w", encoding="utf-8"), indent=1)

print(json.dumps({
    "v1Titles": len(old_tags), "v2Generated": len(new), "merged": len(merged),
    "addedTitles": len(merged) - len(old_tags), "overwrittenTitles": overwritten,
    "tagsRepaired": repairs, "tagsDropped": drops,
    "titlesBelowFloorAfterDrop": len(below_floor), "belowFloorSample": below_floor[:8],
    "stringsToEmbed": len(changed),
    "out": args.out, "embedList": args.embed_list,
}, indent=2))
