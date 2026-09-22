#!/usr/bin/env python3
"""Merge the corpus facts pass with the delta pass into the file that ships.

The facts scrape runs twice and cannot run once. Corpus titles have a vector and their pass stamps every
record `hasVector`; delta titles have none, and their pass does not. `hasVector` is stated per record
because /recommend must never let a vectorless record into an ANN path, so the two passes cannot be
collapsed into one.

Note `hasVector` is a PASS-level stamp, not a fact looked up per title. And the delta set is not only "new
arrivals the >=50-vote worklist cannot reach yet": it is every title /recommend needs that has no vector,
which has held series with thousands of votes and no plot, and titles the classify pass dropped from the
labels (docs/OPERATE.md step 6a says how the list is derived).

The facts stage (`pipeline/facts.py`) runs this merge after both passes. Before that nothing in this repo
performed it: the published `facts-<version>.json` was assembled by hand, which is how a rebuild once
**dropped the 137 delta records** — exactly the titles nothing else covers, so the loss was invisible from
every other artifact and showed up only as /recommend quietly losing library titles.

Delta records LOSE to corpus records on a collision: the corpus pass has a vector and the fuller scrape, and
a title that has since been embedded should be read as embedded.

    scripts/merge-facts.py <corpus-facts.json> <delta-facts.json> <out.json> [--version <datasetVersion>]
"""
import json
import sys


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main():
    corpus_path, delta_path, dest = sys.argv[1], sys.argv[2], sys.argv[3]
    version = sys.argv[sys.argv.index("--version") + 1] if "--version" in sys.argv else None

    corpus, delta = load(corpus_path), load(delta_path)
    if corpus["schema"] != delta["schema"]:
        sys.exit(f"schema mismatch: {corpus['schema']} vs {delta['schema']}")

    records = {(r["mediaType"], r["tmdbId"]): r for r in delta["records"]}
    overlap = sum(1 for r in corpus["records"] if (r["mediaType"], r["tmdbId"]) in records)
    # Corpus wins: it carries the vector and the fuller scrape.
    records.update({(r["mediaType"], r["tmdbId"]): r for r in corpus["records"]})
    merged = [records[k] for k in sorted(records)]

    # Entities and genreMap are id → name maps; the delta's are a superset of nothing in particular, so both
    # are folded with the corpus winning ties, same rule as the records.
    entities = {**delta.get("entities", {}), **corpus.get("entities", {})}
    genre_map = {**delta.get("genreMap", {}), **corpus.get("genreMap", {})}

    # Every input record must appear in the output. The whole reason this script exists is a merge that
    # silently lost 137 of them.
    expected = len({(r["mediaType"], r["tmdbId"]) for r in corpus["records"] + delta["records"]})
    if len(merged) != expected:
        sys.exit(f"merge lost records: {expected} distinct in, {len(merged)} out")

    out = {
        "schema": corpus["schema"],
        "datasetVersion": version or corpus.get("datasetVersion", "unversioned"),
        "genreMap": genre_map,
        "entities": entities,
        "records": merged,
    }
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(out, fh)

    with_vector = sum(1 for r in merged if r.get("hasVector"))
    print(json.dumps({
        "records": len(merged),
        "fromCorpus": len(corpus["records"]),
        "fromDelta": len(delta["records"]),
        "overlap": overlap,
        "hasVector": with_vector,
        "vectorless": len(merged) - with_vector,
        "basedOnKind": sum(1 for r in merged if r.get("basedOnKind")),
        "entities": len(entities),
        "path": dest,
    }))


if __name__ == "__main__":
    main()
