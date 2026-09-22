#!/usr/bin/env python3
"""FINALIZE — the embed pass's two append-only stores, turned into what the corpus is built from.

`index/labels.jsonl` + `index/vectors.jsonl` → `labels-t02.json`, `vectors-bge-m3.bin` and
`dataset.meta.json` (plus `labels-t02.json.gz` and `report.json`, which nothing downstream reads). It sits
after `embed` because those stores are its input, and before `facts` because the corpus facts pass scrapes
the ids in the `labels-t02.json` this writes.

**Every refusal here is a way a corpus shipped, or nearly shipped, wrong while looking healthy:**

  * the two stores are zipped by POSITION from here on — the vector row carries no mediaType — so equal
    line counts are not enough: line *i* of each must name the same title, or every title after a tear
    holds its neighbour's vector;
  * every vector must share one length, and it must be the one `bge-m3` implies. A store assembled from
    two embedders, or an FNV blob labelled bge-m3, loads fine and ranks nonsense;
  * `index/embedder.json` and `index/embedding-space.json` are optional — a store from before either was
    recorded is legitimate — but one that is there and will not parse is a refusal, because skipping it
    drops the identity from the manifest and looks exactly like a store that never had one;
  * the labels artifact is checked for prose field names before it is written (`SHIP_GUARD`).

**A re-embed supersedes, it does not duplicate.** A key that appears twice keeps its LAST record and that
record's vector, which is what makes an incremental top-up an append rather than a rebuild.

**The bytes are the Swift's.** `labels-t02.json`'s sha is half of `datasetVersion`, so it is written by
`pipeline/jsonbytes.py`'s `JSONEncoder` spelling; the manifest keeps `JSONSerialization`'s. The gzip is
`/usr/bin/gzip -k`'s framing — the input's name and mtime in the header, deflate at level 6 — built here
rather than shelled out to, which is the same deflate stream byte for byte.

`--embedding-version` is gone. It relabelled the blob for an offline FNV run, and the stage declares the
file it writes: a second name for the same output is a declaration the run does not keep.
"""
import email.utils
import hashlib
import json
import math
import os
import struct
import sys
import time
import zlib

from . import artifacts, jsonbytes
from .contract import REPO, StageError
from lib import cache as caching

sys.path.insert(0, os.path.join(REPO, "scripts", "v2"))
import vector_blob  # noqa: E402  — the DENVEC02 layout, one definition shared with the store writer

NAME = "finalize"
PRODUCER = "pipeline/finalize.py"
HOW = "./den stage finalize --out-dir <dir> --dataset-version <ver>"
#: Writes into the out-dir; a repeat over the same stores writes the same bytes, bar two timestamps.
PUBLISHES = False
#: Local files in, local files out.
SPENDS = False

#: What names the blob, and the dimension that name implies. Checked against the vectors, so a store
#: embedded by something else is refused rather than published under this name.
EMBEDDING_MODEL = "bge-m3"
DIMS = 1024
TAXONOMY = "t02"
QUANTIZATION = "int8-symmetric-x127"

INPUTS = (artifacts.EMBED_LABELS, artifacts.EMBED_VECTORS, artifacts.EMBEDDER, artifacts.EMBEDDING_SPACE)
OUTPUTS = (artifacts.VECTOR_LABELS, artifacts.VECTORS, artifacts.MANIFEST)

#: Every key the manifest this stage writes is authoritative for — including when it leaves one out. The
#: three `metadata*` keys belong to the retired poster sidecar and stay OWNED so a rewrite drops them
#: rather than inheriting a previous run's, which would vouch for a sha both consumers hard-verify.
OWNED = frozenset((
    "datasetVersion", "taxonomyVersion", "embeddingModel", "dims", "count", "quantization", "labelsFile",
    "vectorsFile", "labelsGzFile", "labelsSha256", "labelsBytes", "vectorsSha256", "vectorsBytes",
    "builtAt", "lastModifiedHttp", "metadataFile", "metadataSha256", "metadataBytes", "embedderRuntime",
    "embedderMaxTokens", "embeddingSpace"))

#: Field names that carry expressive prose. A published artifact holds labels, ids and numbers — TMDB's
#: terms bar shipping their text, and a CC0 plot belongs in the corpus and the embedding, not in an
#: artifact served to devices. Matched on every key at every depth, case- and separator-insensitively, so
#: a `plot_summary` is caught the same as a `plotSummary`; never by substring over the blob, which a film
#: titled "Overview" would trip.
SHIP_GUARD = frozenset(("overview", "summary", "synopsis", "description", "plot", "plotsummary", "tagline",
                        "storyline", "premise", "abstract", "blurb", "logline", "review"))

SOURCES = ("llm", "recipe", "wikidata", "cluster")


def lines(path):
    """Non-empty lines, as the Swift's `readLines` split them."""
    with open(path, encoding="utf-8") as handle:
        return [line for line in handle.read().splitlines() if line]


def _label(item, where):
    if not isinstance(item, dict) or not isinstance(item.get("label"), str) \
            or isinstance(item.get("confidence"), bool) \
            or not isinstance(item.get("confidence"), (int, float)):
        raise StageError(f"finalize: {where} holds a label that is not {{label, confidence}}: {item!r}")
    return {"confidence": float(item["confidence"]), "label": item["label"]}


def record(line, where):
    """One labels-store line as the record that ships — its declared fields and nothing else.

    Strict: a line missing a field refuses the run, as the Swift's decode did. Extra keys are dropped
    rather than carried, which is also what keeps a `plot` an importer left on a row out of the artifact.
    """
    try:
        raw = json.loads(line)
        ok = (isinstance(raw["tmdbId"], int) and not isinstance(raw["tmdbId"], bool)
              and isinstance(raw["mediaType"], str) and isinstance(raw["primaryGenre"], str)
              and isinstance(raw["subgenres"], list) and isinstance(raw["moods"], list)
              and raw["source"] in SOURCES and isinstance(raw["animated"], bool))
    except (ValueError, KeyError, TypeError):
        ok = False
    if not ok:
        raise StageError(f"finalize: {where} is not a labels-store record: {line[:200]}")
    return {"animated": raw["animated"], "mediaType": raw["mediaType"],
            "moods": [_label(item, where) for item in raw["moods"]],
            "primaryGenre": raw["primaryGenre"], "source": raw["source"],
            "subgenres": [_label(item, where) for item in raw["subgenres"]], "tmdbId": raw["tmdbId"]}


def vector_row(line, where):
    try:
        raw = json.loads(line)
        tmdb_id, values = raw["tmdbId"], raw["v"]
        ok = isinstance(tmdb_id, int) and isinstance(values, list) and all(
            isinstance(x, int) and not isinstance(x, bool) for x in values)
    except (ValueError, KeyError, TypeError):
        ok = False
    if not ok:
        raise StageError(f"finalize: {where} is not a vector-store row: {line[:200]}")
    # int8, clamped: the service quantises and this stores its answer, never re-quantising it.
    return tmdb_id, [max(-128, min(127, x)) for x in values]


def read_store(labels_path, vectors_path):
    """`(records, vectors)`, paired, newest record per title."""
    label_lines, vector_lines = lines(labels_path), lines(vectors_path)
    if len(label_lines) != len(vector_lines):
        raise StageError(f"finalize: store misaligned: {len(label_lines)} labels vs "
                         f"{len(vector_lines)} vectors")
    records = [record(line, f"{labels_path}:{n}") for n, line in enumerate(label_lines, 1)]
    rows = [vector_row(line, f"{vectors_path}:{n}") for n, line in enumerate(vector_lines, 1)]
    for n, (rec, (tmdb_id, _)) in enumerate(zip(records, rows), 1):
        if rec["tmdbId"] != tmdb_id:
            raise StageError(
                f"finalize: store misaligned at line {n}: labels say tmdbId {rec['tmdbId']}, vectors say "
                f"{tmdb_id} — refusing to ship. Re-run the embed stage, which reconciles the stores "
                f"before appending.")
    last = {}
    for index, rec in enumerate(records):
        last[f"{rec['mediaType']}:{rec['tmdbId']}"] = index
    keep = sorted(last.values())
    return [records[i] for i in keep], [rows[i][1] for i in keep]


def read_identity(path, required_fields):
    """A record the embed pass left, or None when there is none. Unreadable is a refusal."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            found = json.load(handle)
        if not all(isinstance(found.get(field), kind) for field, kind in required_fields):
            raise ValueError(f"it lacks one of {', '.join(f for f, _ in required_fields)}")
    except (ValueError, AttributeError) as broken:
        raise StageError(f"finalize: {path} is unreadable ({broken}) — it records what this store's vectors "
                         f"are, so shipping without it would misdescribe the corpus") from None
    return found


def prohibited(value, found=None):
    """The `SHIP_GUARD` names this document uses as a key, anywhere in it, sorted."""
    found = set() if found is None else found
    if isinstance(value, dict):
        for key, child in value.items():
            if "".join(c for c in key.lower() if c.isalnum()) in SHIP_GUARD:
                found.add(key)
            prohibited(child, found)
    elif isinstance(value, list):
        for child in value:
            prohibited(child, found)
    return sorted(found)


def gzip_like_the_cli(path):
    """`gzip -k <path>`: the input's basename and mtime in the header, level-6 deflate, OS byte Unix.

    Byte-for-byte what `/usr/bin/gzip` wrote beside the labels, so the only thing two runs disagree on is
    the four mtime bytes — the input's, which `gzip` records and which moves with every write.
    """
    with open(path, "rb") as handle:
        body = handle.read()
    mtime = int(os.stat(path).st_mtime)
    deflate = zlib.compressobj(6, zlib.DEFLATED, -zlib.MAX_WBITS, 8)
    header = (b"\x1f\x8b\x08\x08" + struct.pack("<I", mtime) + b"\x00\x03"
              + os.path.basename(path).encode("utf-8") + b"\x00")
    trailer = struct.pack("<II", zlib.crc32(body) & 0xFFFFFFFF, len(body) & 0xFFFFFFFF)
    out = path + ".gz"
    caching.write_atomically(out, header + deflate.compress(body) + deflate.flush() + trailer)
    os.utime(out, (mtime, mtime))
    return out


def bucket(confidence):
    low = math.floor(confidence * 10) / 10
    return "%.1f-%.1f" % (low, low + 0.1)


def counters(out_dir):
    """The enrichment's and the retired classify checkpoint's totals, for the report and nothing else.
    A file that is absent or will not parse reports zeros, as it always did: this is a diagnostic."""
    totals = {}
    for name, needs in (("enrich-checkpoint.json", "processed"), ("classify-checkpoint.json", "done")):
        try:
            with open(os.path.join(out_dir, name), encoding="utf-8") as handle:
                found = json.load(handle)
            if needs in found:
                totals.update(found.get("totals") or {})
        except (OSError, ValueError, TypeError):
            continue
    return totals


def report(records, out_dir):
    by_genre, histogram = {}, {}
    for rec in records:
        by_genre[rec["primaryGenre"]] = by_genre.get(rec["primaryGenre"], 0) + 1
        for item in rec["subgenres"] + rec["moods"]:
            key = bucket(item["confidence"])
            histogram[key] = histogram.get(key, 0) + 1
    totals = counters(out_dir)
    return {"anime": totals.get("anime", 0), "noPrimary": totals.get("noPrimary", 0),
            "report": {"byPrimaryGenre": by_genre, "confidenceHistogram": histogram,
                       "fetchFailures": totals.get("failures", 0), "llmCalls": 0,
                       "processed": len(records), "skippedBelowVoteFloor": totals.get("belowFloor", 0)}}


def run(ctx, now=None):
    """Write the shipped artifacts from the index stores. Returns the manifest's path."""
    records, vectors = read_store(ctx.require(artifacts.EMBED_LABELS), ctx.require(artifacts.EMBED_VECTORS))
    lengths = sorted({len(v) for v in vectors})
    if len(lengths) != 1 or lengths[0] <= 0:
        raise StageError(f"finalize: vectors have non-uniform length {lengths} — a mixed-embedder store; "
                         f"refusing to ship. Re-assemble the batches with a single embedder.")
    dim = lengths[0]
    if dim != DIMS:
        raise StageError(f"finalize: {EMBEDDING_MODEL} implies dim {DIMS} but the vectors are {dim}-dim — "
                         f"a mislabelled artifact; refusing to ship.")
    embedder = read_identity(ctx.path(artifacts.EMBEDDER),
                             (("model", str), ("dims", int), ("runtime", str), ("maxTokens", int)))
    if embedder and embedder["dims"] > 0 and embedder["dims"] != dim:
        raise StageError(f"finalize: the store was embedded by {embedder['model']}/{embedder['dims']} "
                         f"({embedder['runtime']}) but its vectors are {dim}-dim — refusing to ship a "
                         f"manifest that would misdescribe them.")
    space = read_identity(ctx.path(artifacts.EMBEDDING_SPACE), (("spaceId", str),))

    labels = {"count": len(records), "records": records, "taxonomyVersion": TAXONOMY}
    words = prohibited(labels)
    if words:
        raise StageError(f"finalize: REFUSING to ship: the labels artifact carries prose field(s) "
                         f"{', '.join(words)}. A published artifact holds labels, ids and numbers.")
    labels_blob = jsonbytes.compact(labels).encode("utf-8")
    keys = [vector_blob.pack_key(f"{rec['mediaType']}:{rec['tmdbId'] & 0xFFFFFFFF}") for rec in records]
    if len(set(keys)) != len(keys):
        raise StageError("finalize: the key column repeats a title — two rows claiming one title is not a "
                         "join any reader can resolve")
    vectors_blob = vector_blob.header(keys, dim) + bytes(x & 0xFF for row in vectors for x in row)

    labels_path, vectors_path = ctx.path(artifacts.VECTOR_LABELS), ctx.path(artifacts.VECTORS)
    meta_path = ctx.path(artifacts.MANIFEST)
    for path in (labels_path, vectors_path, meta_path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    caching.write_atomically(labels_path, labels_blob)
    caching.write_atomically(vectors_path, vectors_blob)
    labels_gz = gzip_like_the_cli(labels_path)

    labels_sha = hashlib.sha256(labels_blob).hexdigest()
    vectors_sha = hashlib.sha256(vectors_blob).hexdigest()
    version = hashlib.sha256(f"{labels_sha}:{vectors_sha}".encode()).hexdigest()[:12]
    stamp = int(time.time() if now is None else now)
    meta = {"datasetVersion": version, "taxonomyVersion": TAXONOMY, "embeddingModel": EMBEDDING_MODEL,
            "dims": dim, "count": len(records), "quantization": QUANTIZATION,
            "labelsFile": os.path.basename(labels_path), "vectorsFile": os.path.basename(vectors_path),
            "labelsGzFile": os.path.basename(labels_gz), "labelsSha256": labels_sha,
            "labelsBytes": len(labels_blob), "vectorsSha256": vectors_sha, "vectorsBytes": len(vectors_blob),
            "builtAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp)),
            "lastModifiedHttp": email.utils.formatdate(stamp, usegmt=True)}
    if embedder:
        meta["embedderRuntime"] = embedder["runtime"]
        meta["embedderMaxTokens"] = embedder["maxTokens"]
    if space:
        meta["embeddingSpace"] = space["spaceId"]
    existing = None
    if os.path.exists(meta_path):
        with open(meta_path, "rb") as handle:
            existing = handle.read()
    merged = jsonbytes.merge_manifest(meta, existing, OWNED)
    caching.write_atomically(meta_path, jsonbytes.manifest(merged).encode("utf-8"))

    summary = report(records, ctx.out_dir)
    caching.write_atomically(os.path.join(ctx.out_dir, "report.json"),
                             jsonbytes.pretty(summary).encode("utf-8"))
    print(f"  finalize: {len(records)} titles · labels={labels_path} vectors={vectors_path} "
          f"meta={meta_path} dataset={version}", file=sys.stderr)
    genres = sorted(summary["report"]["byPrimaryGenre"].items(), key=lambda kv: (-kv[1], kv[0]))
    print("  primary-genre dist: " + " ".join(f"{g}:{n}" for g, n in genres), file=sys.stderr)
    return meta_path
