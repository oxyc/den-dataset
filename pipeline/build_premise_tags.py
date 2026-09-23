#!/usr/bin/env python3
"""Consolidate the v1 premise tag batches into one committed JSON — the STRINGS behind vectors-premise.bin.

Why this exists: `vectors-premise.bin` has shipped for months while the tags it was built from lived only in
624 unmerged batch files in a working directory. Nobody could see why two titles were neighbours, the tags
could not be used as facets, and `labels-premise.json` is a byte-identical copy of the plot labels, so it
answers none of it. A vector space whose source text is unpublished cannot be debugged or trusted.

Keying: the batch files carry a BARE tmdbId with no mediaType, which is the collision hazard this corpus has
bitten on before (movie 95 is Armageddon, tv 95 is Buffy). `premise-ids.json` is verified to be in the same
order as `labels-premise.json`, so mediaType is recovered POSITIONALLY rather than by trusting the bare id.
The check below refuses to write if that ordering assumption ever stops holding.

    pipeline/build_premise_tags.py <out-t02 dir> <output.json>
"""
import json
import os
import sys


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "out-t02"
    dest = sys.argv[2] if len(sys.argv) > 2 else "data/premise-tags-v1.json"
    wip = os.path.join(root, "premise-tags-wip")

    ids = json.load(open(os.path.join(wip, "premise-ids.json")))
    labels = json.load(open(os.path.join(root, "labels-premise.json")))
    rows = labels["records"] if isinstance(labels, dict) else labels

    # The positional join is the whole basis for knowing a tag list's mediaType. If the two files ever stop
    # agreeing, a bare-id fallback would silently file a series' tags under a film, so refuse instead.
    if [r["tmdbId"] for r in rows] != ids:
        sys.exit("premise-ids.json is no longer aligned with labels-premise.json — refusing to guess mediaType")
    media = {tid: r["mediaType"] for tid, r in zip(ids, rows)}

    tags = {}
    batch_dir = os.path.join(wip, "out")
    for name in sorted(os.listdir(batch_dir)):
        if not (name.startswith("batch-") and name.endswith(".json")):
            continue
        for entry in json.load(open(os.path.join(batch_dir, name))):
            tid, t = entry.get("tmdbId"), entry.get("tags")
            if tid is None or not t:
                continue
            mt = media.get(tid)
            if mt is None:      # generated for a title that never made the shipped index
                continue
            tags[f"{mt}:{tid}"] = t

    # The 219 titles the v1 run never covered (166 films, 53 series) were tagged later under the v2 schema and
    # left unmerged. Their vectors sit in v2/vectors/vectors-coverage-fill.bin; without this merge the premise
    # index is permanently 219 short of the plot index for no reason.
    #
    # v2 records are {tag, kind, rank, ...}, not plain strings, and carry kinds v1's spec forbids —
    # `tone-setting` is a mood word, which the spec bans outright. Only `premise` and `subject` kinds are
    # taken, ordered by rank so "most-defining-first" survives the schema change.
    filled = []
    fill_path = os.path.join(root, "v2", "coverage-fill", "coverage-fill-aggregated.json")
    if os.path.exists(fill_path):
        for key, rec in json.load(open(fill_path))["records"].items():
            if key in tags:
                continue
            kept = [t for t in rec.get("tags", []) if t.get("kind") in ("premise", "subject")]
            kept.sort(key=lambda t: t.get("rank", 0))
            if kept:
                tags[key] = [t["tag"] for t in kept]
                filled.append(key)

    missing = [f"{media[i]}:{i}" for i in ids if f"{media[i]}:{i}" not in tags]
    out = {
        "schema": 1,
        "index": "premise-v1",
        "count": len(tags),
        # Recorded so a reader knows what these strings ARE without finding the spec: an LLM was given ONLY
        # the Wikipedia plot and asked for the structural premise, most-defining first, with proper nouns and
        # genre/mood words forbidden. That ban is why this index cannot serve character search, and why it
        # beats the plot index at similarity.
        "derivedFrom": "Wikipedia plot text (hasWikiPlot titles only), via the LLM spec in "
                       "premise-tags-wip/GEN-SPEC-PROD.md: 8-12 open-vocabulary structural tags, "
                       "most-defining-first, NO proper nouns, NO genre or mood words",
        "embeddedBy": "bge-m3 via den-embed; the vectors are vectors-premise.bin, aligned to "
                      "premise-ids.json order",
        # Named, not just counted: these rows came from the v2 tagger, so their tags were selected by kind
        # rather than produced under the v1 spec. A consumer comparing tag styles should know which is which.
        # NOT the same as the published vector count. vectors-premise.bin holds 37,314 vectors; the
        # `coverageFilled` titles have TAGS here and NO VECTOR in the published index, because that merge
        # (DT-N) has never been run -- the vectors sit unmerged in v2/vectors/vectors-coverage-fill.bin.
        # Anything joining these tags to that blob by position or assuming parity WILL be wrong.
        "vectorsPublished": 37314,
        "vectorsMissingFor": sorted(filled),
        "coverageFilled": sorted(filled),
        "tags": tags,
    }
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    with open(dest, "w") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    print(json.dumps({"wrote": dest, "titles": len(tags), "missing": len(missing),
                      "bytes": os.path.getsize(dest)}))


if __name__ == "__main__":
    main()
