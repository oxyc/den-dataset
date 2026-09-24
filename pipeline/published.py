#!/usr/bin/env python3
"""What the live dataset was built from, published beside it — and a fresh out-dir seeded from it.

    pipeline/published.py bundle OUT_DIR DEST     # the bundle of OUT_DIR's generation, into DEST
    ./den daily --out-dir DIR                     # seeds DIR from DIR/published/ when DIR is empty

The daily job (oxyc/den-dataset#27) runs on a machine that keeps nothing between runs. It starts from what
the last publish put on the `corpus-<ver>` release: the store's own inputs, and the few records the stages
resume from, all of them derived and none of them prose — the corpus rows (every judgement, each title's
`source`), the facts and their Wikidata checkpoints, both vector blobs and their titles, the genres & moods,
the doc facts, and which embedder, space and document shape the vectors were made in. `bundle` gathers them
for the publish; `seed` lays them back out as an out-dir the stages run over as if they had built it:

  * `enriched/batch-<maxBatchId>.json` — one record per title with a `source`, its fields and its plot's
    digest in place of the plot. The change set diffs today's batches against it (`pipeline/changes.py`
    compares `plotSha256` as the digest of the text), the refresh asks for each title's current revision,
    and the grounding census and the plot-vector gate read it as they read any batch;
  * `enrich-checkpoint.json` — every title processed and the next batch after it, so the drain asks only
    what is new;
  * `index/labels.jsonl` and `index/vectors.jsonl` — every published plot vector with its title's genres &
    moods, in the blob's order, so `finalize` writes the whole blob again with today's vectors after them;
  * the facts, doc facts, genres & moods, premise blob and embedder records under the names the stages
    read, and the live corpus and genres & moods under `published/`, as the corpus join's and the genres &
    moods stage's base.

Everything a title's judgement needs from before today is in that base; everything bought today is in
today's shards. The two-day test (`den_daily_test.py`) holds a seeded out-dir to building the same corpus
and store as the out-dir that kept everything.
"""
import hashlib
import json
import os
import shutil
import sys

if not __package__:
    # Run as a file by the publisher: the repo, not pipeline/, is the import root.
    sys.path[0] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from lib import cache as caching  # noqa: E402
from pipeline import artifacts, enrich, jsonbytes  # noqa: E402
from pipeline.contract import StageError  # noqa: E402
from store import vector_blob  # noqa: E402

#: `(path in the out-dir, asset name on the release, required)`. `{version}` is the generation's. A release
#: asset name cannot hold a `/`, so a nested path is spelled with dots.
BUNDLE = (
    ("corpus-{version}.jsonl.gz", "corpus.jsonl.gz", True),
    ("corpus-{version}-entities.json.gz", "entities.json.gz", True),
    ("facts-{version}.json", "facts.json", True),
    ("labels-t02.json", "labels-t02.json", True),
    ("vectors-bge-m3.bin", "vectors-bge-m3.bin", True),
    ("labels-premise.json", "labels-premise.json", True),
    ("vectors-premise.bin", "vectors-premise.bin", False),
    ("genres-moods.json", "genres-moods.json", True),
    ("doc-facts.json", "doc-facts.json", True),
    ("index/composition.json", "index.composition.json", True),
    ("index/embedder.json", "index.embedder.json", False),
    ("index/embedding-space.json", "index.embedding-space.json", False),
    ("facts-fields.json", "facts-fields.json", True),
    ("facts-entities.json", "facts-entities.json", True),
    ("facts-source-types.json", "facts-source-types.json", False),
    ("facts-delta/facts-fields.json", "facts-delta.facts-fields.json", False),
    ("facts-delta/facts-entities.json", "facts-delta.facts-entities.json", False),
    ("facts-delta/facts-source-types.json", "facts-delta.facts-source-types.json", False),
)

#: The bundle's own record: the generation and each file's digest.
RECORD = "bundle.json"


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bundle(out_dir, dest):
    """Copy the bundle of `out_dir`'s generation into `dest`, with `bundle.json`. Returns the record."""
    with open(os.path.join(out_dir, artifacts.MANIFEST.filename), encoding="utf-8") as fh:
        version = json.load(fh)["datasetVersion"]
    os.makedirs(dest, exist_ok=True)
    files, missing = {}, []
    for path, asset, required in BUNDLE:
        source = os.path.join(out_dir, path.format(version=version))
        if not os.path.exists(source):
            if required:
                missing.append(path.format(version=version))
            continue
        shutil.copyfile(source, os.path.join(dest, asset))
        files[asset] = sha256(source)
    if missing:
        raise StageError(f"published: {out_dir} holds no {', '.join(missing)}, so the next run could not start "
                         f"from this generation. Every one is written by a stage of the run that built it.")
    record = {"datasetVersion": version, "files": files}
    with open(os.path.join(dest, RECORD), "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=1, sort_keys=True)
    return record


def records(corpus_path):
    """The seeded batch: every title the corpus records a `source` for, as the record that source came from."""
    out = []
    with caching_open(corpus_path) as fh:
        for line in fh:
            if line.strip():
                row = json.loads(line)
                if row.get("source"):
                    out.append({"tmdbId": row["tmdbId"], "mediaType": row["mediaType"], **row["source"]})
    return out


def caching_open(path):
    import gzip
    return gzip.open(path, "rt", encoding="utf-8") if path.endswith(".gz") else open(path, encoding="utf-8")


def seed(out_dir):
    """Lay the bundle in `out_dir/published/` out as an out-dir. Returns what it wrote."""
    published = os.path.join(out_dir, os.path.dirname(artifacts.PUBLISHED_META.filename))
    with open(os.path.join(out_dir, artifacts.PUBLISHED_META.filename), encoding="utf-8") as fh:
        meta = json.load(fh)
    with open(os.path.join(published, RECORD), encoding="utf-8") as fh:
        record = json.load(fh)
    version, through = meta.get("datasetVersion"), meta.get("maxBatchId")
    if record.get("datasetVersion") != version:
        raise StageError(f"published: the bundle is {record.get('datasetVersion')} and the live dataset is "
                         f"{version} — download the corpus-{version} release")
    for asset, digest in record["files"].items():
        if sha256(os.path.join(published, asset)) != digest:
            raise StageError(f"published: {asset} is not the file the bundle records")
    if os.listdir(os.path.join(out_dir, "enriched")) if os.path.isdir(os.path.join(out_dir, "enriched")) else False:
        raise StageError(f"published: {out_dir} already holds enriched batches; a seed is for an empty out-dir")
    if not isinstance(through, int):
        raise StageError("published: the live manifest records no maxBatchId, so no batch number is the baseline")

    for path, asset, _required in BUNDLE:
        if asset in record["files"] and asset != "corpus.jsonl.gz":
            target = os.path.join(out_dir, path.format(version=version))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(os.path.join(published, asset), target)
    # The two bases, under the names the corpus join and the genres & moods stage read them by — which
    # are where the bundle already lays them, unless those names ever part.
    for asset, artifact in (("corpus.jsonl.gz", artifacts.PUBLISHED_CORPUS),
                            ("genres-moods.json", artifacts.PUBLISHED_GENRES_MOODS)):
        source, target = os.path.join(published, asset), os.path.join(out_dir, artifact.filename)
        if os.path.abspath(source) != os.path.abspath(target):
            shutil.copyfile(source, target)
    # The out-dir's own manifest: the live one, which `finalize` merges over as it merges over any.
    shutil.copyfile(os.path.join(out_dir, artifacts.PUBLISHED_META.filename),
                    os.path.join(out_dir, artifacts.MANIFEST.filename))

    baseline = records(os.path.join(out_dir, artifacts.PUBLISHED_CORPUS.filename))
    os.makedirs(os.path.join(out_dir, "enriched"), exist_ok=True)
    caching.write_atomically(enrich.batch_path(out_dir, through), enrich.swift_json(baseline).encode("utf-8"))
    state = {"nextBatch": through + 1, "processed": sorted(enrich.key(r["mediaType"], r["tmdbId"]) for r in baseline),
             "totals": {}, "judgedBelow": {}}
    caching.write_atomically(enrich.checkpoint_path(out_dir), enrich.compact(state).encode("utf-8"))

    # The embed stores, in the blob's row order: each vector beside its title's published label record.
    with open(os.path.join(out_dir, artifacts.VECTOR_LABELS.filename), encoding="utf-8") as fh:
        labels = {f"{r['mediaType']}:{r['tmdbId']}": r for r in json.load(fh)["records"]}
    _count, dims, keys, blob, base = vector_blob.read(os.path.join(out_dir, artifacts.VECTORS.filename))
    label_lines, vector_lines = [], []
    for row, key in enumerate(keys):
        if key not in labels:
            raise StageError(f"published: the vector blob holds {key} and labels-t02.json does not")
        values = blob[base + row * dims: base + (row + 1) * dims]
        label_lines.append(jsonbytes.compact(labels[key]) + "\n")
        vector_lines.append(jsonbytes.compact({"tmdbId": labels[key]["tmdbId"],
                                               "v": [b - 256 if b > 127 else b for b in values]}) + "\n")
    for artifact, lines in ((artifacts.EMBED_LABELS, label_lines), (artifacts.EMBED_VECTORS, vector_lines)):
        caching.write_atomically(os.path.join(out_dir, artifact.filename), "".join(lines).encode("utf-8"))
    return {"datasetVersion": version, "baseline": len(baseline), "vectors": len(keys), "batch": through}


def main(argv):
    if len(argv) == 3 and argv[0] == "bundle":
        try:
            record = bundle(argv[1], argv[2])
        except StageError as refusal:
            print(f"error: {refusal}", file=sys.stderr)
            return 1
        print(f"bundle: {len(record['files'])} files of {record['datasetVersion']} in {argv[2]}")
        return 0
    print("usage: pipeline/published.py bundle OUT_DIR DEST", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
