#!/usr/bin/env python3
"""FINALIZE — the embed pass's two append-only stores, turned into what the corpus is built from.

`index/labels.jsonl` + `index/vectors.jsonl` → `labels-t02.json`, `vectors-bge-m3.bin` and
`dataset.meta.json` (plus `report.json`, which nothing downstream reads). It sits
after `embed` because those stores are its input, and before `facts` because the corpus facts pass scrapes
the ids in the `labels-t02.json` this writes.

**The stores decide which titles; `genres-moods.json` decides their genres & moods.** A label line in the
stores is what its vector was composed from, which can be older than this run's genres & moods: a title
whose genres & moods changed keeps its vector until `embed --reembed-changed` re-embeds it. What ships is
this run's, so `labels-t02.json` carries each vector title's genres & moods from `genres-moods.json`, and
`datasetVersion` moves when they change. A vector whose title has no genres & moods is refused — shipping
the label line instead would carry forward what the genres & moods stage no longer says.

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
`pipeline/jsonbytes.py`'s `JSONEncoder` spelling; the manifest keeps `JSONSerialization`'s.

**There is no `labels-t02.json.gz`.** It was the precompressed copy den-atlas served to clients sending
`Accept-Encoding: gzip`, and that stopped when the blobs were retired for the store (oxyc/den#113):
`prune_manifest.py` drops every `*GzFile` key, so the release has not named it since, and no reader in
this repo, in den-atlas or in the box's sync looks for it. It went on being written anyway — 11 MB per
run into the out-dir, for nobody. `labelsGzFile` stays in `OWNED` so a rewrite over an older manifest
drops the key rather than inheriting a claim about a file that is no longer there.

`--embedding-version` is gone. It relabelled the blob for an offline FNV run, and the stage declares the
file it writes: a second name for the same output is a declaration the run does not keep.
"""
import dataclasses
import email.utils
import hashlib
import json
import math
import os
import sys
import time

from . import artifacts, jsonbytes
from .contract import REPO, StageError, bind
from lib import cache as caching
from store import vector_blob  # the DENVEC02 layout, one definition shared with the store writer

NAME = "finalize"
PRODUCER = "pipeline/finalize.py"
HOW = "./den stage finalize --out-dir <dir>"
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

#: The enrichment's checkpoint is read for three counters in `report.json` and nothing else. The Swift
#: also read `classify-checkpoint.json` for a fourth, `noPrimary`; the only thing that wrote that file was
#: the vote-pass `assemble`, deleted in #48, so it is not read here and the counter is gone with it.
INPUTS = (artifacts.EMBED_LABELS, artifacts.EMBED_VECTORS, artifacts.GENRES_MOODS, artifacts.EMBEDDER,
          artifacts.EMBEDDING_SPACE, artifacts.ENRICH_CHECKPOINT)
OUTPUTS = (artifacts.VECTOR_LABELS, artifacts.VECTORS, artifacts.MANIFEST, artifacts.FINALIZE_REPORT)

#: What the enrichment's checkpoint tallies. The Swift decoded all four, so a malformed one of them made the
#: whole checkpoint unreadable, which the report shows as zeros. `noOverview` was the fourth and is gone
#: with the TMDB stub check that counted it (oxyc/den-dataset#53): a checkpoint written before that still
#: carries the number, and reporting a counter no rule can move again would read as a rule still running.
ENRICH_TOTALS = ("belowFloor", "anime", "failures")

#: Every key the manifest this stage writes is authoritative for — including when it leaves one out. The
#: three `metadata*` keys belong to the retired poster sidecar, and `labelsGzFile` to the precompressed
#: labels this stage no longer writes; both stay OWNED so a rewrite drops them rather than inheriting a
#: previous run's, which would vouch for a file that is not there and a sha both consumers hard-verify.
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
    """One labels-store line as the record that ships. See `parse_record`."""
    try:
        raw = json.loads(line)
    except ValueError:
        raise StageError(f"finalize: {where} is not a labels-store record: {line[:200]}") from None
    return parse_record(raw, where, line)


def parse_record(raw, where, line=None):
    """A label record as it ships — its declared fields and nothing else.

    Strict: a record missing a field refuses the run, as the Swift's decode did. Extra keys are dropped
    rather than carried, which is also what keeps a `plot` an importer left on a row out of the artifact.
    """
    try:
        ok = (isinstance(raw["tmdbId"], int) and not isinstance(raw["tmdbId"], bool)
              and isinstance(raw["mediaType"], str) and isinstance(raw["primaryGenre"], str)
              and isinstance(raw["subgenres"], list) and isinstance(raw["moods"], list)
              and raw["source"] in SOURCES and isinstance(raw["animated"], bool))
    except (KeyError, TypeError):
        ok = False
    if not ok:
        shown = line if line is not None else json.dumps(raw)
        raise StageError(f"finalize: {where} is not a labels-store record: {shown[:200]}")
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


def bucket(confidence):
    low = math.floor(confidence * 10) / 10
    return "%.1f-%.1f" % (low, low + 0.1)


def _int_or_absent(value):
    return value is None or (isinstance(value, int) and not isinstance(value, bool))


def enrichment_totals(path):
    """The enrichment checkpoint's tallies, for the report and nothing else.

    Read the way the Swift decoded the checkpoint: it is one only with a `processed` list (keys, or the
    movie pilot's bare ids), an integer `nextBatch` if any, and integer tallies. Anything else — absent,
    unparseable, or some other file under the name — reports zeros, as it always did: this is a diagnostic.
    """
    if path is None:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            found = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(found, dict):
        return {}
    processed, totals = found.get("processed"), found.get("totals")
    is_checkpoint = (isinstance(processed, list)
                     and (all(isinstance(key, str) for key in processed)
                          or all(isinstance(key, int) and not isinstance(key, bool) for key in processed))
                     and _int_or_absent(found.get("nextBatch"))
                     and (totals is None or (isinstance(totals, dict)
                                             and all(_int_or_absent(totals.get(k)) for k in ENRICH_TOTALS))))
    if not is_checkpoint:
        return {}
    return {k: totals[k] for k in ENRICH_TOTALS if totals and totals.get(k) is not None}


def report(records, enrich_checkpoint):
    by_genre, histogram = {}, {}
    for rec in records:
        by_genre[rec["primaryGenre"]] = by_genre.get(rec["primaryGenre"], 0) + 1
        for item in rec["subgenres"] + rec["moods"]:
            key = bucket(item["confidence"])
            histogram[key] = histogram.get(key, 0) + 1
    totals = enrichment_totals(enrich_checkpoint)
    return {"anime": totals.get("anime", 0),
            "report": {"byPrimaryGenre": by_genre, "confidenceHistogram": histogram,
                       "fetchFailures": totals.get("failures", 0), "llmCalls": 0,
                       "processed": len(records), "skippedBelowVoteFloor": totals.get("belowFloor", 0)}}


#: What `genres-moods.json` decides about a title. The rest of a record — its key and `source` — is the
#: store's.
GENRES_MOODS_FIELDS = ("animated", "moods", "primaryGenre", "subgenres")


def current(records, genres_moods_path):
    """Each stored record with its title's genres & moods from `genres-moods.json`, in the stores' order."""
    from . import genres_moods  # it imports this module for `parse_record`
    known = genres_moods.read(genres_moods_path)
    keys = [f"{rec['mediaType']}:{rec['tmdbId']}" for rec in records]
    missing = [key for key in keys if key not in known]
    if missing:
        raise StageError(f"finalize: {len(missing)} title(s) have a vector and no genres & moods in "
                         f"{genres_moods_path}, e.g. {missing[:5]}. Their vectors were composed from genres "
                         f"& moods this run no longer has; give them genres & moods again (the curated file, "
                         f"or `./den stage genres_moods --spend`), or rebuild the stores without them.")
    return [dict(rec, **{field: known[key][field] for field in GENRES_MOODS_FIELDS})
            for rec, key in zip(records, keys)]


def run(ctx, now=None):
    """Write the shipped artifacts from the index stores. Returns the manifest's path."""
    records, vectors = read_store(ctx.require(artifacts.EMBED_LABELS), ctx.require(artifacts.EMBED_VECTORS))
    records = current(records, ctx.require(artifacts.GENRES_MOODS))
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
    report_path = ctx.path(artifacts.FINALIZE_REPORT)
    for path in (labels_path, vectors_path, meta_path, report_path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    caching.write_atomically(labels_path, labels_blob)
    caching.write_atomically(vectors_path, vectors_blob)

    labels_sha = hashlib.sha256(labels_blob).hexdigest()
    vectors_sha = hashlib.sha256(vectors_blob).hexdigest()
    version = hashlib.sha256(f"{labels_sha}:{vectors_sha}".encode()).hexdigest()[:12]
    stamp = int(time.time() if now is None else now)
    meta = {"datasetVersion": version, "taxonomyVersion": TAXONOMY, "embeddingModel": EMBEDDING_MODEL,
            "dims": dim, "count": len(records), "quantization": QUANTIZATION,
            "labelsFile": os.path.basename(labels_path), "vectorsFile": os.path.basename(vectors_path),
            "labelsSha256": labels_sha, "labelsBytes": len(labels_blob), "vectorsSha256": vectors_sha, "vectorsBytes": len(vectors_blob),
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

    summary = report(records, ctx.require(artifacts.ENRICH_CHECKPOINT))
    caching.write_atomically(report_path, jsonbytes.pretty(summary).encode("utf-8"))
    print(f"  finalize: {len(records)} titles · labels={labels_path} vectors={vectors_path} "
          f"meta={meta_path} dataset={version}", file=sys.stderr)
    genres = sorted(summary["report"]["byPrimaryGenre"].items(), key=lambda kv: (-kv[1], kv[0]))
    print("  primary-genre dist: " + " ".join(f"{g}:{n}" for g, n in genres), file=sys.stderr)
    return meta_path


def manifest_version(ctx):
    """`datasetVersion` from this out-dir's manifest, or None when there is no manifest yet.

    The version names the files of the stages after this one, and it is derived here from what this stage
    wrote, so an operator cannot know it before the run. A `--dataset-version` is therefore only a check:
    one that disagrees with the manifest is refused rather than obeyed, because the stages would then look
    for each other's files under two names.
    """
    path = ctx.path(artifacts.MANIFEST)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            version = json.load(handle).get("datasetVersion")
    except (OSError, ValueError, AttributeError) as broken:
        raise StageError(f"{path} is not a readable manifest ({broken}), so the dataset version is "
                         f"unknown.") from None
    if not isinstance(version, str) or not version:
        raise StageError(f"{path} names no datasetVersion. Re-run: {HOW}")
    if ctx.dataset_version and ctx.dataset_version != version:
        raise StageError(f"--dataset-version {ctx.dataset_version} is not this out-dir's generation — "
                         f"{path} says {version}, and every versioned file is named after the labels and "
                         f"vectors it describes. Pass --dataset-version {version}, or leave it off.")
    return version


def versioned(module, ctx):
    """`ctx` with the manifest's dataset version, for a stage that names a file with it; `ctx` unchanged
    for one that does not, or when there is no manifest to read one from."""
    declared = (bind(e).artifact for e in tuple(module.INPUTS) + tuple(module.OUTPUTS))
    if not any("{version}" in artifact.filename for artifact in declared):
        return ctx
    version = manifest_version(ctx)
    return dataclasses.replace(ctx, dataset_version=version) if version else ctx
