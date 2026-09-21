#!/usr/bin/env python3
"""Record counts for the published JSON blobs — stamp them into the manifest, and refuse a silent shrink.

`publish-dataset.sh` already refuses to drop a declared FILE. It cannot see a file that stays declared and
loses rows, and that is a real failure mode rather than a hypothetical: a facts rebuild once constructed its
records from the scrape checkpoint and silently dropped the 137 facts-only delta titles. Those are exactly the
records nothing else covers — no labels, no vectors, no facets row — so the loss was invisible from every
other artifact, and the only symptom was /recommend quietly losing its ability to judge library titles.

Counting is the cheap check. `--stamp` writes `<key>Records` into the manifest so the NEXT publish has a
baseline; `--compare` reads the published manifest's stamps and prints any blob that would shrink.

    manifest-counts.py --stamp   <meta.json> <out-dir>
    manifest-counts.py --compare <published-meta.json> <meta.json> <out-dir>
"""
import json
import os
import struct
import sys

# Only blobs whose row count is meaningful and cheap to read. Vectors are binary and already length-checked
# against their labels by `finalize`; the gz variants are regenerated from the blobs they mirror.
COUNTED = (
    "factsFile",
    "factsSlimFile",
    "plotFacetsFile",
    "metadataFile",
    "labelsFile",
    "premiseLabelsFile",
    "storeFile",
)

STORE_MAGIC = b"DENSTOR1"


def store_rows(path):
    """`row_count` from a store-v1 header, or None if this is not one.

    den-spec `wire/store-v1.md`: 64-byte header, magic at 0, `row_count` a little-endian u32 at 28.

    Read from the FILE rather than taken from `storeRecords` in the manifest beside it. The manifest's own
    claim cannot validate the manifest — a stamp copied forward from the previous publish would satisfy the
    shrink guard no matter what the store contained, which is the whole failure this script exists to catch.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(32)
    except OSError:
        return None
    if len(head) < 32 or head[:8] != STORE_MAGIC:
        return None
    return struct.unpack_from("<I", head, 28)[0]


def count(meta, key, base):
    """Rows in the blob a manifest key names, or None when it cannot be counted cheaply."""
    name = meta.get(key)
    if not name:
        return None
    path = os.path.join(base, name)
    if not os.path.exists(path):
        return None
    # The store is the one counted blob that is not JSON. This used to return None for anything not ending
    # `.json`, so the artifact the whole serving path reads was the ONE thing the shrink guard could not
    # see — it would have passed a store with every title missing.
    if name.endswith(".store"):
        return store_rows(path)
    if not name.endswith(".json"):
        return None
    try:
        doc = json.load(open(path))
    except Exception:
        # A blob we cannot parse is not this script's problem to report — the sha check will catch it.
        return None
    if isinstance(doc, list):
        return len(doc)
    if isinstance(doc, dict):
        # `records` covers facts/labels; `facets` and `tags` are the map-shaped blobs.
        for field in ("records", "facets", "tags"):
            v = doc.get(field)
            if isinstance(v, (list, dict)):
                return len(v)
    return None


def highest_batch_id(base):
    """The highest enriched batch id the publish dir can see, or None.

    Stamped so a later check can say WHICH enriched records a published dataset was built from. There is no
    other record of it: `out-t02-cc0b/enriched` is a SYMLINK to `../out-t02/enriched`, so a publish reads a
    live directory that keeps growing, and after the fact nothing distinguishes "this batch was included"
    from "this batch was written later". Any invariant relating enriched records to shipped labels needs
    that boundary or it reports every normal enrich-after-embed run as a violation.

    Deliberately stamped HERE and not added to `DatasetMeta`. That struct's `namingSidecar` enumerates every
    field by hand while `ownedKeys` comes from `CodingKeys`, so a new field with a default compiles, is
    treated as owned, and is then dropped on the next `metadata` run — the exact trap `Manifest.swift`
    documents. Unowned keys stamped here are carried forward by `ManifestMerge` instead.
    """
    enriched = os.path.join(base, "enriched")
    if not os.path.isdir(enriched):
        return None
    ids = []
    for name in os.listdir(enriched):
        if name.startswith("batch-") and name.endswith(".json"):
            try:
                ids.append(int(name[len("batch-"):-len(".json")]))
            except ValueError:
                continue
    return max(ids) if ids else None


def main():
    mode = sys.argv[1]
    if mode == "--stamp":
        meta_path, base = sys.argv[2], sys.argv[3]
        meta = json.load(open(meta_path))
        for key in COUNTED:
            n = count(meta, key, base)
            if n is not None:
                meta[key[: -len("File")] + "Records"] = n
        highest = highest_batch_id(base)
        if highest:
            meta["maxBatchId"] = highest
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=1)
            f.write("\n")
        return

    if mode == "--compare":
        old = json.load(open(sys.argv[2]))
        new_meta, base = json.load(open(sys.argv[3])), sys.argv[4]
        for key in COUNTED:
            stored = old.get(key[: -len("File")] + "Records")
            now = count(new_meta, key, base)
            # No stamp on the published side means this is the first publish since the guard existed.
            if stored is None or now is None:
                continue
            if now < stored:
                print(f"{key}: {stored} -> {now}  ({stored - now} records lost)")
        return

    sys.exit(f"unknown mode {mode}")


if __name__ == "__main__":
    main()
