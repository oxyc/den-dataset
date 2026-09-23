#!/usr/bin/env python3
"""Refuse a manifest that names a blob from a DEAD `datasetVersion`.

    check_filename_version.py <meta.json>

The producer stamps the version into a filename — `facts-<ver>.json`, `metadata-<ver>.json`,
`den-<ver>.store` — so a blob that was not rebuilt keeps the name of the generation that made it. The
manifest then declares an artifact from a superseded corpus beside artifacts from the current one, and
every other check passes: the file exists, its sha matches, its record count has not moved (because
nothing touched it), and its producer is registered.

This is not hypothetical. At the time this was written the live manifest read:

    datasetVersion   5b1c3213b6a1
    plotFacetsFile   plot-facets-c85c707b0b18.json     <- a generation old

`c85c707b0b18` had been superseded and the plot-facet rows were being served from it. Nothing in the
publish path could see it, because nothing compared the NAME against the version.

## The exempt set

A blob whose name carries no version is unversioned BY DESIGN and is skipped: `labels-t02.json` and
`labels-premise.json` (named for the taxonomy), `vectors-bge-m3.bin` and `vectors-premise.bin` (named for
the embedding model), `facets.bin`. Those are rewritten in place every publish, so a stale one is the
record-count guard's problem, not this one.

The version is matched as `-<12 hex>` immediately before a suffix, which is the shape the producer emits.
A looser search for any 12-hex run would match a filename that merely contains one.

**What this cannot do.** The exemption is derived from the NAME, so republishing a stale blob without its
version — `plot-facets.json` — makes it permanently exempt. Nothing else would see it either: its record
count never moves, and coverage only catches it once the corpus grows past the 2.041% in
`manifest_counts.py`. This guard is a tripwire for the ordinary case (a blob not rebuilt keeps its old
name), not a defence against someone routing around it.

There is also no `DEN_ALLOW_DROPPING_BLOBS` escape here, unlike the record-count and ownership guards.
That is deliberate — a blob from a dead generation is not a judgement call — but it means a stale blob
BLOCKS publishing until it is rebuilt or its key is dropped.
"""
import json
import re
import sys

# `-<12 hex>` followed by the end or a dot: `facts-5b1c3213b6a1.json`, `…json.gz`, `den-<ver>.store`.
VERSIONED = re.compile(r"-([0-9a-f]{12})(?=\.|$)")


def stale(meta):
    """`[(key, filename, version-in-the-name)]` for every blob from another generation."""
    want = meta.get("datasetVersion")
    if not want:
        return [("datasetVersion", "", "")]
    found = []
    for key, value in sorted(meta.items()):
        if not (isinstance(value, str) and key.endswith("File")):
            continue
        for version in VERSIONED.findall(value):
            if version != want:
                found.append((key, value, version))
    return found


def main():
    with open(sys.argv[1], encoding="utf-8") as fh:
        meta = json.load(fh)
    found = stale(meta)
    if not found:
        print(f"filename-version: ok (every versioned blob is {meta.get('datasetVersion')})")
        return 0
    if found[0][0] == "datasetVersion":
        print("filename-version: the manifest has no datasetVersion", file=sys.stderr)
        return 1
    want = meta["datasetVersion"]
    print(f"filename-version: {len(found)} blob(s) from a dead generation (this one is {want}):",
          file=sys.stderr)
    for key, value, version in found:
        print(f"       {key}: {value}  <- {version}", file=sys.stderr)
    print("       Rebuild them, or drop the key if nothing reads it. A blob keeps the name of the",
          file=sys.stderr)
    print("       generation that made it, so this is the only check that can see one go stale.",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
