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
import sys

# Only blobs whose row count is meaningful and cheap to read. Vectors are binary and already length-checked
# against their labels by `finalize`; the gz variants are regenerated from the blobs they mirror.
COUNTED = ("factsFile", "factsSlimFile", "plotFacetsFile", "metadataFile", "labelsFile", "premiseLabelsFile")


def count(meta, key, base):
    """Rows in the blob a manifest key names, or None when it cannot be counted cheaply."""
    name = meta.get(key)
    if not name or not name.endswith(".json"):
        return None
    path = os.path.join(base, name)
    if not os.path.exists(path):
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


def main():
    mode = sys.argv[1]
    if mode == "--stamp":
        meta_path, base = sys.argv[2], sys.argv[3]
        meta = json.load(open(meta_path))
        for key in COUNTED:
            n = count(meta, key, base)
            if n is not None:
                meta[key[: -len("File")] + "Records"] = n
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
