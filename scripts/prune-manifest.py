#!/usr/bin/env python3
"""Cut `dataset.meta.json` down to the one artifact the release still carries.

    prune-manifest.py --prune   <meta.json>   # rewrite it, dropping every retired blob's keys
    prune-manifest.py --retired <meta.json>   # print the keys --prune would drop, one per line

The store (den-spec `wire/store-v1`) holds what the per-blob artifacts used to: facts, labels, cards,
facets, rail facets, the entity table, alias titles, and both vector matrices as sections. So `data-latest`
publishes `den-<ver>.store` and `dataset.meta.json`, and nothing else (oxyc/den#113).

The keys have to be pruned HERE rather than never written, because the producers that write them are still
the producers that build the store's INPUTS: `taxonomy-backfill finalize` writes `labels-t02.json` and
declares it in the same pass, `ManifestMerge` carries every unowned key forward, and `build_store.py` then
reads those files. The blobs keep being built; they stop being published.

## A keep-list, not a list of things to delete

Everything shaped like a per-blob claim — `<prefix>File`, `GzFile`, `Sha256`, `Bytes`, `Records` — is
dropped unless `<prefix>` is PUBLISHED. A deny-list would have to be extended every time a producer invents
a key, and the one it missed would be published with no hash, no count and no consumer. This way a key
nobody has thought of yet is dropped by default, and the publisher prints every key it drops.

Keys that are not blob claims are left alone: `datasetVersion`, `taxonomyVersion`, `embeddingModel`,
`dims`, `count`, `quantization`, `builtAt`, `lastModifiedHttp`, `embedderRuntime`, `embedderMaxTokens`,
`maxBatchId`, `signature`. They describe the dataset, not a file. The corpus is not in here at all — it
ships under its own `corpus-<ver>` tag, deliberately outside the serving manifest, because
`fetch-dataset.sh` pulls every `*File` key and the box would download 44 MB it never reads.

Every `<prefix>GzFile` is pruned, the store's included: the precompressed copies existed because atlas
served those JSON blobs to clients that send `Accept-Encoding: gzip`. The store is read from disk and never
served, and atlas MMAPS it — a compressed file cannot be mapped.
"""
import json
import sys

# The blob prefixes `data-latest` still carries.
PUBLISHED = ("store",)

# Numbers that describe a retired blob rather than the dataset. They are not shaped like blob claims, so
# the suffix rule below cannot see them: `premiseCount` and `premiseDims` are the premise index's row count
# and geometry, and `premiseEmbeddingModel` names the space it was embedded in. With no premise blob
# published they describe nothing — and den-atlas served `premiseCount` to the app for months while it sat
# a generation behind the file it described, which is what an unowned number does.
RETIRED_SCALARS = ("premiseCount", "premiseDims", "premiseEmbeddingModel")

# Every suffix a per-blob claim can carry.
BLOB_SUFFIXES = ("File", "Sha256", "Bytes", "Records")


def retired(meta):
    """The keys that describe an artifact this release no longer publishes."""
    out = []
    for key in meta:
        if key in RETIRED_SCALARS:
            out.append(key)
            continue
        # A gz twin goes whatever it is a twin OF. The precompressed copies existed for the JSON blobs
        # den-atlas served to the app; the store is not served, and it is MMAPPED, so a compressed one
        # could not be read even if it were. `storeGzFile` is therefore retired like the rest.
        if key.endswith("GzFile"):
            out.append(key)
            continue
        for suffix in BLOB_SUFFIXES:
            if not key.endswith(suffix):
                continue
            prefix = key[: -len(suffix)]
            if prefix and prefix not in PUBLISHED:
                out.append(key)
            break
    return sorted(out)


def main():
    mode, path = sys.argv[1], sys.argv[2]
    with open(path, encoding="utf-8") as fh:
        meta = json.load(fh)
    gone = retired(meta)

    if mode == "--retired":
        for key in gone:
            print(key)
        return 0

    if mode == "--prune":
        for key in gone:
            meta.pop(key)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=1)
            fh.write("\n")
        return 0

    print(f"unknown mode {mode}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
