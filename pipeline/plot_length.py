#!/usr/bin/env python3
"""Fit the one plot-vector direction that tracks plot length.

The fit is deliberately a build artifact, not a runtime guess.  It is fitted once for an embedding
space/document shape and then travels in the corpus bundle: a stateless daily seed carries plot digests,
not the old plot prose, and new rows must use the same direction as old rows or every daily generation
would move the whole matrix.

The canonical fit is the #109 method, stated without a BLAS implementation dependency: normalize the raw
int8 rows, regress every component on ``ln(embedded plot characters)`` over English source plots, and
normalize the slope.  The observations and every input digest are kept beside the direction, so the 4 KB
answer is auditable without duplicating the 240 MB raw vector store it names by hash.
"""
import base64
import hashlib
import json
import math
import os
import struct

from . import artifacts, compose, genres_moods
from .articles import key as article_key, ordered_batches
from .contract import StageError
from lib import cache as caching

NAME = "plot_length"
PRODUCER = "pipeline/plot_length.py"
HOW = "./den stage plot_length --out-dir <dir>"
PUBLISHES = False
SPENDS = False

INPUTS = (
    artifacts.GENRES_MOODS,
    artifacts.ENRICHED.called("enriched_dir"),
    artifacts.DOC_FACTS,
    artifacts.PLOT_TRANSLATIONS,
    artifacts.EMBED_LABELS,
    artifacts.EMBED_VECTORS,
    artifacts.COMPOSITION,
    artifacts.EMBEDDING_SPACE,
)
OUTPUTS = (artifacts.PLOT_LENGTH_TRANSFORM,)

SCHEMA = 1
ALGORITHM = "unit-orthogonal-projection-v1"
FIT_METHOD = "ols-unit-int8-on-ln-english-plot-chars-v1"
ENCODING = "base64-f32-le"


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_sha256(path):
    """Digest a directory as sorted relative-name/NUL/file-digest records."""
    digest = hashlib.sha256()
    for root, dirs, names in os.walk(path):
        dirs.sort()
        for name in sorted(names):
            full = os.path.join(root, name)
            rel = os.path.relpath(full, path).replace(os.sep, "/")
            digest.update(rel.encode("utf-8") + b"\0" + bytes.fromhex(sha256(full)))
    return digest.hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _identity(path, field):
    try:
        with open(path, encoding="utf-8") as fh:
            value = json.load(fh)
        found = value[field]
        if not isinstance(found, (str, int, bool)):
            raise ValueError
        return value
    except (OSError, ValueError, KeyError, TypeError):
        raise StageError(f"plot_length: {path} does not record a usable {field}") from None


def latest_rows(labels_path, vectors_path):
    """Newest ``key -> (record, raw vector, doc sha)`` from the paired append stores."""
    # Local because finalize consumes this stage's artifact.  Its strict append-store parser is the
    # contract we want here, without making the two stage modules import each other while initialising.
    from . import finalize

    label_lines, vector_lines = finalize.lines(labels_path), finalize.lines(vectors_path)
    if len(label_lines) != len(vector_lines):
        raise StageError(f"plot_length: store misaligned: {len(label_lines)} labels vs "
                         f"{len(vector_lines)} vectors")
    out = {}
    for n, (label_line, vector_line) in enumerate(zip(label_lines, vector_lines), 1):
        rec = finalize.record(label_line, f"{labels_path}:{n}")
        try:
            row = json.loads(vector_line)
            values = row["v"]
            ok = (row["tmdbId"] == rec["tmdbId"] and isinstance(values, list)
                  and all(isinstance(x, int) and not isinstance(x, bool) for x in values))
        except (ValueError, KeyError, TypeError):
            ok = False
        if not ok:
            raise StageError(f"plot_length: {vectors_path}:{n} is not the vector for its paired label")
        title = f"{rec['mediaType']}:{rec['tmdbId']}"
        doc_sha = row.get("docSha256")
        out[title] = (rec, [max(-128, min(127, x)) for x in values],
                      doc_sha if isinstance(doc_sha, str) else None)
    return out


def english_plots(enriched_dir, vector_keys, cap):
    """Newest English source plot per vector key, counted exactly after the embedded plot cap."""
    seen, found = set(), {}
    for name in reversed(ordered_batches(enriched_dir)):
        with open(os.path.join(enriched_dir, name), encoding="utf-8") as fh:
            batch = json.load(fh)
        for row in batch:
            title = article_key(row)
            if title in seen:
                continue
            seen.add(title)
            if title not in vector_keys or (row.get("plotLanguage") or "en") != "en":
                continue
            plot = compose.source_plot(row, cap)
            if plot:
                # This is intentionally Python's code-point count: it is what #109 fitted after the
                # grapheme-aware cap had selected the embedded text.
                found[title] = len(plot)
    return found


def unit(row):
    norm = math.sqrt(math.fsum(float(x) * float(x) for x in row))
    return [float(x) / norm for x in row] if norm else [0.0 for _ in row]


def fit(rows, lengths):
    """The deterministic f64 spelling of the #109 component-wise least-squares slope."""
    keys = sorted(lengths)
    if len(keys) < 2:
        raise StageError("plot_length: fewer than two English plot vectors cannot fit a direction")
    dims = {len(rows[k][1]) for k in keys}
    if len(dims) != 1 or next(iter(dims)) <= 0:
        raise StageError(f"plot_length: fitted vectors have non-uniform dimensions {sorted(dims)}")
    dim = next(iter(dims))
    xs = [math.log(lengths[k]) for k in keys]
    x_mean = math.fsum(xs) / len(xs)
    means = [0.0] * dim
    for title in keys:
        for j, value in enumerate(unit(rows[title][1])):
            means[j] += value
    means = [value / len(keys) for value in means]
    slope, denominator = [0.0] * dim, math.fsum((x - x_mean) ** 2 for x in xs)
    if denominator == 0:
        raise StageError("plot_length: every fitted plot has the same length")
    for title, x in zip(keys, xs):
        xc = x - x_mean
        for j, value in enumerate(unit(rows[title][1])):
            slope[j] += (value - means[j]) * xc
    slope = [value / denominator for value in slope]
    norm = math.sqrt(math.fsum(value * value for value in slope))
    if not norm:
        raise StageError("plot_length: fitted direction is the zero vector")
    return keys, [value / norm for value in slope]


def direction_bytes(direction):
    return b"".join(struct.pack("<f", value) for value in direction)


def decode_direction(record):
    try:
        raw = base64.b64decode(record["directionBase64"], validate=True)
        dims = record["dims"]
    except (KeyError, TypeError, ValueError):
        raise StageError("plot_length: transform has no valid base64 f32 direction") from None
    if not isinstance(dims, int) or isinstance(dims, bool) or dims <= 0:
        raise StageError("plot_length: transform has no positive integer dimension")
    if record.get("directionEncoding") != ENCODING or len(raw) != dims * 4:
        raise StageError(f"plot_length: transform direction is not {dims} little-endian f32 values")
    if hashlib.sha256(raw).hexdigest() != record.get("directionSha256"):
        raise StageError("plot_length: transform direction digest does not match its bytes")
    direction = list(struct.unpack(f"<{dims}f", raw))
    norm = math.sqrt(sum(value * value for value in direction))
    if not all(math.isfinite(value) for value in direction) or abs(norm - 1.0) > 1e-5:
        raise StageError(f"plot_length: transform direction is not a finite unit vector (norm {norm})")
    return direction


def public_record(artifact, artifact_sha=None):
    """The small transform descriptor embedded in the serving manifest."""
    keys = ("schema", "algorithm", "dims", "inputEmbeddingSpace", "directionEncoding",
            "directionBase64", "directionSha256", "fitMethod")
    out = {key: artifact[key] for key in keys}
    out["fitArtifactSha256"] = artifact_sha
    return out


def read(path, composition_path=None, space_path=None):
    try:
        with open(path, "rb") as fh:
            blob = fh.read()
        record = json.loads(blob)
    except (OSError, ValueError, TypeError):
        raise StageError(f"plot_length: {path} is not a readable transform artifact") from None
    if (record.get("schema"), record.get("algorithm"), record.get("fitMethod")) != \
            (SCHEMA, ALGORITHM, FIT_METHOD):
        raise StageError("plot_length: transform names an unsupported schema, algorithm, or fit method")
    decode_direction(record)
    if composition_path and record.get("compositionSha256") != sha256(composition_path):
        raise StageError("plot_length: transform was fitted for another document composition")
    if space_path and os.path.exists(space_path):
        space = _identity(space_path, "spaceId")["spaceId"]
        if record.get("inputEmbeddingSpace") != space:
            raise StageError("plot_length: transform was fitted for another embedding space")
    return record, hashlib.sha256(blob).hexdigest()


def run(ctx):
    destination = ctx.path(artifacts.PLOT_LENGTH_TRANSFORM)
    composition_path, space_path = ctx.require(artifacts.COMPOSITION), ctx.require(artifacts.EMBEDDING_SPACE)
    # Reuse is the stable daily path. The corpus bundle carries this file; fitting again would both lack
    # the old prose and move every vector when a handful of titles arrived.
    if os.path.exists(destination):
        read(destination, composition_path, space_path)
        print(f"  plot_length: reusing {destination}", file=os.sys.stderr)
        return destination

    labels_path, vectors_path = ctx.require(artifacts.EMBED_LABELS), ctx.require(artifacts.EMBED_VECTORS)
    rows = latest_rows(labels_path, vectors_path)
    composition = _identity(composition_path, "plotCap")
    lengths = english_plots(ctx.require(artifacts.ENRICHED), rows,
                            composition["plotCap"])

    # Prove every checkable fit observation still describes the document its vector came from.
    labels = genres_moods.read(ctx.require(artifacts.GENRES_MOODS))
    with open(ctx.require(artifacts.DOC_FACTS), encoding="utf-8") as fh:
        doc_facts = json.load(fh)
    translated_path = ctx.require(artifacts.PLOT_TRANSLATIONS)
    translations = compose.read_translations(translated_path) if translated_path else {}
    wanted = set(lengths)
    documents = compose.documents(ctx.require(artifacts.ENRICHED), labels,
                                  doc_facts, set(labels) - wanted, composition["plotCap"], {}, translations)
    current_sha = {title: hashlib.sha256(document.encode("utf-8")).hexdigest()
                   for title, _record, document in documents}
    mismatched = [title for title in sorted(wanted) if rows[title][2] and current_sha.get(title) != rows[title][2]]
    if mismatched:
        raise StageError(f"plot_length: {len(mismatched)} fitted vector(s) were made from another document, "
                         f"e.g. {mismatched[:5]}; re-embed changed rows before fitting")

    keys, direction = fit(rows, lengths)
    raw = direction_bytes(direction)
    space = _identity(space_path, "spaceId")["spaceId"]
    observations = [{"key": title, "plotChars": lengths[title]} for title in keys]
    inputs = {
        "compositionSha256": sha256(composition_path),
        "docFactsSha256": sha256(ctx.require(artifacts.DOC_FACTS)),
        "enrichedTreeSha256": tree_sha256(ctx.require(artifacts.ENRICHED)),
        "genresMoodsSha256": sha256(ctx.require(artifacts.GENRES_MOODS)),
        "labelsStoreSha256": sha256(labels_path),
        "translationsSha256": sha256(translated_path) if translated_path else None,
        "vectorsStoreSha256": sha256(vectors_path),
    }
    record = {
        "schema": SCHEMA, "algorithm": ALGORITHM, "fitMethod": FIT_METHOD,
        "dims": len(direction), "inputEmbeddingSpace": space,
        "directionEncoding": ENCODING, "directionBase64": base64.b64encode(raw).decode("ascii"),
        "directionSha256": hashlib.sha256(raw).hexdigest(),
        "compositionSha256": inputs["compositionSha256"], "inputs": inputs,
        "fit": {"language": "en", "observations": len(keys),
                "verifiedDocSha256": sum(rows[k][2] is not None for k in keys),
                "legacyWithoutDocSha256": sum(rows[k][2] is None for k in keys)},
        "observations": observations,
    }
    os.makedirs(os.path.dirname(os.path.abspath(destination)), exist_ok=True)
    caching.write_atomically(destination, canonical(record) + b"\n")
    print(f"  plot_length: fitted {len(keys)} English plot rows · {len(direction)} dims · {destination}",
          file=os.sys.stderr)
    return destination
