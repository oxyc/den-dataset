#!/usr/bin/env python3
"""The POSTER SIDECAR — what a card needs to render a title the index returns as a bare id.

`metadata-<datasetVersion>.json`: one row per shipped title with its name, poster path and year, so the
app can draw a semantic or ANN neighbour without a per-result TMDB detail call. It ships as a ≤6-month
SYNCED cache and is never bundled — a frozen poster snapshot would break TMDB's caching allowance.

**Its filename carries the dataset version, so it is built after every finalize.** Skipping it leaves the
manifest naming the PREVIOUS sidecar, which still hashes correctly — so both consumers accept it, never
re-sync, and every title the run added renders with no poster. Forever, and silently. This stage refuses a
run whose `--dataset-version` is not the one the manifest names, which is the same failure caught one step
earlier.

**A partial result is not a result.** Every fetch used to be discarded on failure, so an expired key or a
rate-limit storm produced an EMPTY sidecar, written over the good one with its sha stamped into the
manifest — and the app folds that sha into its syncKey, so every device re-synced to a file with no
posters in it. Below `COVERAGE_FLOOR` the run refuses and leaves what is there; every miss is logged with
its reason, because the floor asserting "TMDB is failing" without having looked at a single error is what
made it undiagnosable.

**The row order is a TOTAL order.** `metadataSha256` is folded into the app's syncKey, so an order that
moves costs every device a ~4.6 MB re-download of a file that did not change. `tmdbId` alone is not a
total order here: 940 ids in the corpus are both a movie and a series.

**There is no `--skip-fetch`.** The Swift command had one, to patch the manifest from an existing sidecar
without re-fetching; the response cache does that job better. Measured on this corpus, 47,541 of 47,542
titles are already on disk under the enrichment key, so a re-run is one request rather than none — and it
re-checks the coverage floor, which the skip path could not.
"""
import concurrent.futures
import hashlib
import json
import os
import sys

from . import artifacts
from .contract import StageError, bind
from lib import http, tmdb as tmdb_api

NAME = "metadata"

PRODUCER = "pipeline/metadata.py"
HOW = "./den stage metadata --out-dir <dir> --dataset-version <ver>"
#: Writes a file into the out-dir and patches the manifest there. Nothing leaves the machine.
PUBLISHES = False
#: TMDB's own API, unbilled.
SPENDS = False

#: Below this share of titles returning a row, the run is a failure rather than a thin result. Real
#: coverage is ~99% — a title without a poster still returns a row — so anything near zero is auth or
#: rate-limiting, not the catalogue.
COVERAGE_FLOOR = 0.90

#: In-flight detail requests. The cache answers nearly all of them; this bounds what reaches TMDB when it
#: does not.
CONCURRENCY = 8

INPUTS = (artifacts.MANIFEST, artifacts.VECTOR_LABELS.called("labels"))
OUTPUTS = (artifacts.METADATA,)

BOUND = {bind(entry).name: bind(entry) for entry in INPUTS}


def manifest(path, dataset_version):
    """The manifest, as a dict, checked against the version this run is building for.

    Read as JSON rather than through a model: the shipped manifest carries keys no model here declares —
    the facet blob's, the premise index's — and a decode-then-re-encode drops every one of them. That has
    already taken two shipped features down.
    """
    with open(path, encoding="utf-8") as handle:
        meta = json.load(handle)
    named = meta.get("datasetVersion")
    if named != dataset_version:
        raise StageError(
            f"metadata: {path} describes dataset {named!r} and this run is building {dataset_version!r}. "
            f"The sidecar's filename carries the version, so writing one under the other name leaves the "
            f"manifest pointing at the previous sidecar — which still hashes correctly, so both consumers "
            f"accept it and never re-sync. Run finalize first, or name the version it wrote.")
    return meta


def titles(path):
    """The shipped records, as `(mediaType, tmdbId)` in the labels' own order."""
    with open(path, encoding="utf-8") as handle:
        labels = json.load(handle)
    records = labels.get("records")
    if not records:
        raise StageError(f"metadata: {path} names no records, so there would be no cards to render. "
                         f"Build it with: taxonomy-backfill finalize")
    return [(record["mediaType"], record["tmdbId"]) for record in records]


def order(rows):
    """A TOTAL order over the sidecar's rows.

    `tmdbId` alone is not one: 940 ids in the corpus are both a movie and a series, and the sha of this
    file is folded into the app's syncKey — so a tie broken by whichever request finished first costs
    every device a re-download of a file that did not change.
    """
    return sorted(rows, key=lambda row: (row["tmdbId"], row["mediaType"]))


def fetch(client, media, tmdb_id, misses):
    """One row, or None with the reason recorded.

    Discarding the error is what made the coverage floor undiagnosable: it asserts TMDB is failing without
    having looked at a single one.
    """
    try:
        return client.poster_meta(media, tmdb_id)
    except (http.HTTPError, tmdb_api.TMDBError, ValueError) as failure:
        misses.append(f"{media}:{tmdb_id} ({failure})")
        return None


def sidecar(client, wanted, log_path):
    """Every row TMDB answered for, in a total order, or a refusal when too few came back."""
    rows, misses = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        for row in pool.map(lambda item: fetch(client, item[0], item[1], misses), wanted):
            if row is not None:
                rows.append(row)
    if misses:
        with open(log_path, "a", encoding="utf-8") as handle:
            for miss in misses:
                handle.write(f"metadata-miss {miss}\n")
    coverage = 1.0 if not wanted else len(rows) / len(wanted)
    if coverage < COVERAGE_FLOOR:
        raise StageError(
            f"metadata: only {len(rows)} of {len(wanted)} titles returned a row ({int(coverage * 100)}%, "
            f"floor {int(COVERAGE_FLOOR * 100)}%) — that is TMDB failing, not titles without posters. "
            f"Nothing written; the existing sidecar and manifest are unchanged. The reasons are in "
            f"{log_path}.")
    return order(rows)


def write(path, rows):
    """The sidecar, byte-stable for one set of rows.

    Sorted keys, not the order a row happened to be built in: two runs over identical data produced three
    sha256 over the same 5,933,843 bytes and a parsed diff showing zero differing rows — only the key
    order inside each object had moved — and every one of them re-synced every device.
    """
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(rows, sort_keys=True, separators=(",", ":"),
                                ensure_ascii=False).replace("/", r"\/"))


def declare(meta_path, meta, name, blob):
    """Name the sidecar in the manifest, leaving every other key exactly as it was.

    The three keys this owns and nothing else. The shipped manifest carries twelve keys no model in this
    repo declares — the facet blob's, the premise index's — and rewriting it through a closed struct
    dropped all of them: the publisher still uploaded the blobs, its pre-flight only checks that the files
    the manifest NAMES exist, and den-atlas lost premise search and facets with no error on either side.
    """
    meta["metadataFile"] = name
    meta["metadataSha256"] = hashlib.sha256(blob).hexdigest()
    meta["metadataBytes"] = len(blob)
    with open(meta_path, "w", encoding="utf-8") as handle:
        json.dump(meta, handle, indent=1, sort_keys=True)
        handle.write("\n")
    return meta


def run(ctx, client=None):
    """Build the sidecar and declare it. Returns its path."""
    meta_path = ctx.require(artifacts.MANIFEST)
    meta = manifest(meta_path, ctx.dataset_version)
    wanted = titles(ctx.require(BOUND[artifacts.VECTOR_LABELS.name].artifact))
    path = ctx.path(artifacts.METADATA)

    try:
        client = client or tmdb_api.TMDB()
    except tmdb_api.TMDBError as refusal:
        raise StageError(f"metadata: {refusal}") from None

    # A PROBE: fetch a handful, report, write nothing. It used to truncate the record list before the
    # coverage floor was computed, so coverage was always ~100% and the floor could never fire — and the N
    # rows were then written over the shipped 37.5k-row sidecar with their sha stamped into the manifest.
    if ctx.limit:
        probe = sidecar(client, wanted[:ctx.limit], os.path.join(ctx.out_dir, "enrich-log.txt"))
        with_poster = sum(1 for row in probe if row.get("posterPath"))
        print(f"  metadata: probe {len(probe)} of {len(wanted)} ({with_poster} with a poster), "
              f"wrote nothing", file=sys.stderr)
        return f"{len(probe)} probed, nothing written (--limit is a probe)"

    rows = sidecar(client, wanted, os.path.join(ctx.out_dir, "enrich-log.txt"))
    write(path, rows)
    with open(path, "rb") as handle:
        blob = handle.read()
    declare(meta_path, meta, os.path.basename(path), blob)
    with_poster = sum(1 for row in rows if row.get("posterPath"))
    print(f"  metadata: {len(rows)} rows ({with_poster} with a poster), {len(blob)} bytes -> {path}",
          file=sys.stderr)
    return path
