"""Reading what the build is handed, and recording what it read.

The readers refuse a shape they do not recognise rather than guessing at one — guessing is how `labels`
came back as the wrapper dict and every lookup silently missed.
"""
import gzip
import hashlib
import json
import os
import sys

#: Every argument that names a file or directory this READS. The record below is built from it, and
#: `check-producers.py` maps each entry to the producer that builds it — so adding an input here is what
#: makes the new input owned and checked. `test_build_store.py` asserts this covers the parser's inputs
#: and `test_check_producers.py` asserts every one of them has a producer.
#:
#: `metadata` is gone from here because it is gone from the parser: the TMDB sidecar supplied the title,
#: the year and the poster path, and the store now takes the first two from Wikidata and publishes no
#: third. The writer reads no TMDB artifact at all.
INPUT_ARGS = ("corpus", "entities", "facts", "vectors", "vector_labels",
              "premise_vectors", "premise_labels")


def file_sha256(path):
    """A file's hash, read in blocks — the inputs run to 135 MB and there is no reason to hold one."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def input_digest(path):
    """`(sha256, bytes, mtime)` for one build input.

    Every input is a file now. It used to handle a DIRECTORY too, digested over its listing, because
    `--enriched` named the batch tree the vote counts came from — and that tree is TMDB Content, which
    left with the column (oxyc/den#118).
    """
    return file_sha256(path), os.path.getsize(path), int(os.path.getmtime(path))


def build_inputs(args):
    """What this build READ: path, hash, size and mtime for every input argument.

    The store's inputs stopped being published artifacts when `data-latest` went store-only
    (oxyc/den#113), and the ownership guard went with them: `check-producers.py` walks the keys the
    MANIFEST names, so once the manifest named only the store, a store built from a labels file its
    producer had outgrown published perfectly clean. Every guard passed and none of them was looking at
    the thing that was stale.

    Recording it here is what puts them back in reach. `--stamp-meta` writes this into the manifest, so
    the publisher can re-hash each input against the tree and ask `check-producers.py` about its
    producer — and the mtime is recorded rather than read, so the producer question is still answerable
    in a publish dir holding nothing but the store and the manifest.
    """
    out = []
    for arg in INPUT_ARGS:
        path = getattr(args, arg)
        if not path:
            continue
        sha, size, mtime = input_digest(path)
        out.append({"arg": arg, "path": path, "sha256": sha, "bytes": size, "mtime": mtime})
    return out


def corpus_rows(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def read_json(path):
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def labels_by_key(path, label):
    """Rows keyed by `media:tmdbId`. Shapes are NAMED, never guessed — guessing is how `labels` came back
    as the wrapper dict and every lookup silently missed."""
    blob = read_json(path)
    rows = None
    if isinstance(blob, dict):
        for key in ("records", "labels", "tags"):
            if isinstance(blob.get(key), (list, dict)):
                rows = blob[key]
                break
        else:
            rows = blob if all(":" in k for k in list(blob)[:8]) else None
    else:
        rows = blob
    if rows is None:
        sys.exit(f"{label}: {path} has no recognisable rows (keys: {sorted(blob)[:6]})")
    if isinstance(rows, dict):
        return rows
    out = {f"{r['mediaType']}:{r['tmdbId']}": r for r in rows if isinstance(r, dict) and "tmdbId" in r}
    if not out:
        sys.exit(f"{label}: {path} produced no keyed rows")
    return out
