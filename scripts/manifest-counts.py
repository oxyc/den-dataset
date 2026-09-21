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

## Why an absolute count is not enough

`--compare` also watches COVERAGE — a blob's records as a share of `labelsRecords`. An absolute count only
falls when rows are lost; it does not move at all when a blob is simply never rebuilt while the corpus
grows around it. That is the failure that has happened twice here: `facets.bin` fell 999 titles behind the
corpus, and `facts-slim` kept shipping a field set frozen years earlier. Both were generated once, carried
forward by every publish since, and passed every shrink check because their own counts never dropped.

Coverage cannot be satisfied by renaming a key either: a renamed key has no published baseline, so it is
reported as new rather than silently starting over at whatever it happens to be.
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

# Coverage is measured against the labels, which are the closest thing the pipeline has to "the titles the
# corpus knows". The store deliberately holds MORE than this — it is the union of facts and the pass — so
# its coverage reads above 100%. That is fine: the guard watches for a FALL, not for a ceiling.
DENOMINATOR = "labelsFile"

# Percentage points a blob's coverage may fall before the publish is refused. A real rebuild moves coverage
# by fractions of a point; 2 points is about 950 titles at the current corpus size, which is far past any
# honest churn and well inside the 999 titles facets.bin silently fell behind.
COVERAGE_DROP = 2.0


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
        counts = {}
        for key in COUNTED:
            stored = old.get(key[: -len("File")] + "Records")
            now = count(new_meta, key, base)
            counts[key] = (stored, now)
            # No stamp on the published side means this is the first publish since the guard existed.
            if stored is None or now is None:
                continue
            if now < stored:
                print(f"{key}: {stored} -> {now}  ({stored - now} records lost)")

        # COVERAGE, as a share of the labels. Checked after the absolute counts because it answers a
        # different question: not "did this blob lose rows" but "did it keep up with the corpus". A blob
        # nobody rebuilt holds its count exactly while every other artifact grows past it.
        was_total, now_total = counts.get(DENOMINATOR, (None, None))
        if was_total and now_total:
            for key, (stored, now) in counts.items():
                if key == DENOMINATOR or stored is None or now is None:
                    continue
                was, is_now = 100.0 * stored / was_total, 100.0 * now / now_total
                if was - is_now > COVERAGE_DROP:
                    print(
                        f"{key}: coverage {was:.1f}% -> {is_now:.1f}% of {DENOMINATOR} "
                        f"({stored}/{was_total} -> {now}/{now_total}); "
                        f"more than {COVERAGE_DROP:.1f} points"
                    )
        return

    sys.exit(f"unknown mode {mode}")


if __name__ == "__main__":
    main()
