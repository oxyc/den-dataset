#!/usr/bin/env python3
"""Record counts for the published JSON blobs — stamp them into the manifest, and refuse a silent shrink.

`publish-dataset.sh` already refuses to drop a declared FILE. It cannot see a file that stays declared and
loses rows, and that is a real failure mode rather than a hypothetical: a facts rebuild once constructed its
records from the scrape checkpoint and silently dropped the 137 facts-only delta titles. Those are exactly the
records nothing else covers — no labels, no vectors, no facets row — so the loss was invisible from every
other artifact, and the only symptom was /recommend quietly losing its ability to judge library titles.

Counting is the cheap check. `--stamp` writes `<key>Records` into the manifest so the NEXT publish has a
baseline; `--compare` reads the published manifest's stamps and prints any blob that would shrink.

    manifest_counts.py --stamp   <meta.json> <out-dir>
    manifest_counts.py --compare <published-meta.json> <meta.json> <out-dir>
    manifest_counts.py --consistent <meta.json> <out-dir>

## Why an absolute count is not enough

`--compare` also watches COVERAGE — a blob's records as a share of the largest published artifact's (see
DENOMINATOR; it was `labelsRecords`, it is `storeRecords`). An absolute count only
falls when rows are lost; it does not move at all when a blob is simply never rebuilt while the corpus
grows around it. `facets.bin` fell 999 titles behind that way: generated once, carried forward by every
publish since, its own count never dropping.

**What coverage does not cover.** `facts-slim` shipped a field set frozen years earlier at full ROW parity
— `factsSlimRecords == factsRecords` — so coverage moves by exactly zero for it. That failure is a shape
failure and `check_facts_schema.py` is what catches it; counting rows never could. And coverage is a
per-publish comparison, so drift spread thinly across many publishes stays under the threshold at every
step. It catches a blob that falls behind in a jump, not one that erodes a tenth of a point at a time.

Coverage cannot be satisfied by renaming a key: a renamed key has no published baseline, so it is skipped
here rather than scored, and the key that disappeared is caught by the dropped-file guard in
`publish-dataset.sh` instead.
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
    "railFacetsFile",
    "facetsFile",
    "storeFile",
)

# Keys whose blob MUST be countable. For these, a file this script cannot read is a failure, not an
# uncounted key: the whole point of counting them is that they are the ones a silent miss hides in, and
# skipping the check for a file that will not parse is the same silent pass in a different place.
# `storeSha256` cannot save us either — `--stamp-meta` computes it from the bytes it just wrote, so a
# wrongly-written store is self-consistently wrong.
MUST_COUNT = ("storeFile", "labelsFile")

STORE_MAGIC = b"DENSTOR1"
FACETS_MAGIC = b"DFI2"

# Coverage is a blob's records as a share of the largest published one. It used to be measured against the
# labels; `data-latest` stopped publishing them (oxyc/den#113 — the store carries what they held), and a
# denominator no manifest names is a guard that returns nothing and says nothing, which is the shape of
# every failure in this file's docstring. So it is the store.
#
# COVERAGE IS DORMANT WHILE THE STORE IS THE ONLY PUBLISHED ARTIFACT. It answers "did this blob keep up
# with the corpus", and with one blob there is nothing to compare it against — the loop below skips the
# denominator itself. What still guards the store is the ABSOLUTE count above (`storeRecords` may not fall)
# and `MUST_COUNT`, which turns an unreadable store into a refusal. Publish a second artifact beside the
# store and it is covered from its second publish, with no change here.
DENOMINATOR = "storeFile"

# The FRACTION of its coverage a blob may keep before the publish is refused — i.e. it may lose 2% of
# whatever share it had, not 2 points of the whole corpus.
#
# Relative, because absolute points cannot see the failure this is for. A blob that is never rebuilt loses
# `c·g/(1+g)` points in a publish where the corpus grows by `g`, so the points it loses scale with its own
# coverage `c`: at c=100% a 2-point threshold needs 2% growth in one publish, but at c=13.85% — where
# plot-facets actually sits — it needs 16.9%, and across the real 38,532 → 44,531 repass a completely
# un-rebuilt plot-facets blob drops 1.87 points and passes. A blob covering under 2% of the corpus could
# never trip a 2-point rule even by going to zero. As a ratio the threshold is the same for all of them.
#
# THE NUMBER AN OPERATOR NEEDS: an un-rebuilt blob keeps `1/(1+g)` of its share when the denominator grows
# by `g`, so it trips as soon as the denominator grows by more than **2.041%** — and it fires for every
# blob behind it at once, so a publish that grows the corpus meaningfully has to rebuild them all, or say
# why not. Nothing is behind it today: the store is the only published artifact (see DENOMINATOR).
COVERAGE_RATIO = 0.98

# A vector blob's row count is arithmetic from its size — see `vector_rows`.
VECTOR_LAYOUTS = (
    # (name, header bytes, bytes per row). DENVEC02 carries a u64 key per row before the rows themselves.
    ("DENVEC02", 16, 8),
    # The blobs that predate the key column. Still readable here because a manifest can outlive a format.
    ("v1", 8, 0),
)


def vector_rows(size, dims):
    """`(rows, layout)` for a vector blob of `size` bytes at `dims`, or `(None, None)`.

    Solved for each layout rather than assumed, because assuming the wrong one misreports the row count by
    under a percent — close enough to read as a real-but-small disagreement instead of as the wrong sum.
    DENVEC02 is tried first: it is what everything writes now, and the two sizes can coincide (a 127-row
    keyed blob and a 128-row v1 blob are both 131,080 bytes at 1024 dims).
    """
    for name, header, per_row in VECTOR_LAYOUTS:
        stride = dims + per_row
        if size >= header and (size - header) % stride == 0:
            return (size - header) // stride, name
    return None, None


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


def facets_rows(path):
    """`count` from a DFI2 facets blob, or None if this is not one (magic + u32, as the deleted
    `build-facets-bin.py` wrote it)."""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return None
    if len(head) < 8 or head[:4] != FACETS_MAGIC:
        return None
    return struct.unpack_from("<I", head, 4)[0]


def count(meta, key, base):
    """Rows in the blob a manifest key names, or None when it cannot be counted cheaply.

    The two binary blobs are counted from their own headers. Restricting this to `.json` meant the two
    artifacts the guard was WRITTEN for were the two it could not see: the store, which holds every title,
    and `facets.bin`, which is the blob that actually fell 999 titles behind the corpus.
    """
    name = meta.get(key)
    if not name:
        return None
    path = os.path.join(base, name)
    if not os.path.exists(path):
        return None
    if name.endswith(".store"):
        return store_rows(path)
    if name.endswith(".bin"):
        return facets_rows(path)
    if not name.endswith(".json"):
        return None
    try:
        with open(path) as f:
            doc = json.load(f)
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


def count_all(meta, base):
    """Every countable blob's real row count, by manifest key."""
    return {key: count(meta, key, base) for key in COUNTED}


def inconsistencies(meta, counts):
    """Where the manifest's ADVERTISED numbers disagree with the files they describe.

    A different question from `--compare`, which asks whether a blob moved since the last publish. This
    asks whether the manifest is internally true right now — and it is the question that was going
    unasked: `premiseCount` advertised 38,532 to the app for months while `labels-premise.json` held
    44,531 and `vectors-premise.bin` was 44,531 rows. Nothing counted it, because nothing models it, so
    `ManifestMerge` carried it forward from whenever the premise index was first published.

    A vector blob is `16 + rows * (8 + dims)` bytes, so its row count is checkable from the manifest alone
    — no need to read 45 MB to know it disagrees with the labels beside it.
    """
    out = []

    def claim(label, claimed, actual, why):
        if claimed is not None and actual is not None and claimed != actual:
            out.append(f"{label}: manifest says {claimed}, {why} says {actual}")

    # A `<x>Sha256` or `<x>Bytes` whose `<x>File` is gone. Dropping a key leaves its metadata behind —
    # the Step-3 publish removed `railFacetsFile` and `railFacetsGzFile` and left `railFacetsSha256` and
    # `railFacetsBytes` describing a file no longer named or shipped. Nothing reads them and nothing
    # cleans them: the gz pass pops only the `GzFile` twin, the filename guard iterates `*File` only, and
    # `ManifestMerge` carries unowned keys forward forever. So they accumulate, each one a claim about an
    # artifact that is not there.
    named = {k[: -len("File")] for k in meta if k.endswith("File")}
    for key in sorted(meta):
        for suffix in ("Sha256", "Bytes", "Records"):
            if key.endswith(suffix) and key[: -len(suffix)] not in named:
                out.append(f"{key}: describes {key[: -len(suffix)]}File, which the manifest does not name")

    # DORMANT, and worth knowing: `count` is what den-atlas serves to the app as the dataset's size, and it
    # means "titles carrying labels" — `labelsRecords`, 47,539 on the live manifest, not the store's 47,618
    # rows (the store is the union of facts and the pass, so it holds more). With the labels no longer
    # published there is nothing here to check it against, and asserting it against `storeRecords` instead
    # would refuse every publish over a difference that is correct. It is an unvalidated claim until
    # whatever reads it decides which number it wants.
    claim("count", meta.get("count"), counts.get("labelsFile"), "labelsFile")
    claim("premiseCount", meta.get("premiseCount"), counts.get("premiseLabelsFile"), "premiseLabelsFile")
    # Spelled out rather than built from a prefix: `f"{prefix}dims"` gives `premisedims`, which no
    # manifest has, so the check silently did nothing for the half it was written for.
    for label, dims_key, size_key, rows_key in (
        ("plot vectors", "dims", "vectorsBytes", "count"),
        ("premise vectors", "premiseDims", "premiseVectorsBytes", "premiseCount"),
    ):
        dims, size = meta.get(dims_key), meta.get(size_key)
        if not dims or not size:
            continue
        rows, layout = vector_rows(size, dims)
        if rows is None:
            out.append(f"{label}: {size} bytes at {dims} dims is not a whole number of rows in any "
                       f"vector blob layout — the manifest describes something that is not one")
            continue
        claim(label, rows, meta.get(rows_key),
              f"{rows_key} (blob is {size} bytes at {dims} dims, {layout})")
    return out


def highest_batch_id(base):
    """The highest enriched batch id the publish dir can see, or None.

    Stamped so a later check can say WHICH enriched records a published dataset was built from. There is no
    other record of it: `out-t02-cc0b/enriched` is a SYMLINK to `../out-t02/enriched`, so a publish reads a
    live directory that keeps growing, and after the fact nothing distinguishes "this batch was included"
    from "this batch was written later". Any invariant relating enriched records to shipped labels needs
    that boundary or it reports every normal enrich-after-embed run as a violation.

    Deliberately stamped HERE and not added to `DatasetMeta`. Every key that struct declares is OWNED, and
    `ManifestMerge` drops an owned key the new manifest omits — so a field `finalize` does not itself
    compute would be erased by the next `finalize`. Unowned keys stamped here are carried forward instead.
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
        with open(meta_path) as f:
            meta = json.load(f)
        for key in COUNTED:
            n = count(meta, key, base)
            if n is not None:
                meta[key[: -len("File")] + "Records"] = n
        # `premiseCount` is what the SERVED descriptor advertises to the app, and nothing in the pipeline
        # wrote it: `DatasetMeta` does not model it, so `ManifestMerge` carried it forward from whenever
        # the premise index was first published. Found live at 38,532 while the labels file and the vector
        # blob both held 44,531 — the advertised number a generation behind the files it describes, which
        # is this script's entire subject matter appearing in the one field it was not stamping.
        #
        # Derived here rather than merged, so it cannot drift again.
        premise_rows = count(meta, "premiseLabelsFile", base)
        if premise_rows is not None:
            meta["premiseCount"] = premise_rows
        highest = highest_batch_id(base)
        if highest:
            meta["maxBatchId"] = highest
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=1)
            f.write("\n")
        return

    if mode == "--compare":
        with open(sys.argv[2]) as f:
            old = json.load(f)
        with open(sys.argv[3]) as f:
            new_meta = json.load(f)
        base = sys.argv[4]
        counts = {}
        for key in COUNTED:
            stored = old.get(key[: -len("File")] + "Records")
            now = count(new_meta, key, base)
            counts[key] = (stored, now)
            # A blob that MUST be countable and is not: report it rather than skipping the guard for it.
            # Uncountable used to mean unguarded, so a truncated store — or one with a wrong magic —
            # published with no record check at all, which is the silent pass this script exists to close.
            if now is None and key in MUST_COUNT and new_meta.get(key):
                print(f"{key}: {new_meta[key]} will not be read — it must be countable, and is not")
                continue
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
                if was > 0 and is_now / was < COVERAGE_RATIO:
                    print(
                        f"{key}: coverage {was:.1f}% -> {is_now:.1f}% of {DENOMINATOR} "
                        f"({stored}/{was_total} -> {now}/{now_total}); "
                        f"kept {100.0 * is_now / was:.1f}% of its share, floor is "
                        f"{100.0 * COVERAGE_RATIO:.0f}%"
                    )
        return

    if mode == "--consistent":
        meta_path, base = sys.argv[2], sys.argv[3]
        with open(meta_path) as f:
            meta = json.load(f)
        for line in inconsistencies(meta, count_all(meta, base)):
            print(line)
        return

    sys.exit(f"unknown mode {mode}")


if __name__ == "__main__":
    main()
