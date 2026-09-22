#!/usr/bin/env python3
"""DOC FACTS — director and genre for the shipped corpus, from Wikidata.

The two clauses of the embedding document that still came from TMDB. Everything else in the CC0 shape is
already clean — the plot is Wikipedia, the themes are our own tags, `Created by` rides along on the
mapping hop — so these two are what stands between the vectors and a corpus with no TMDB Content in it at
all.

**A stage of its own rather than part of the embed pass.** The scrape is ~770 SPARQL requests and the
embed is hours; pay each once, and let a failure in one not cost the other. That was the reason the Swift
command was separate and it is still the reason.

**Resumable, and it writes after every batch.** A 38.5k-title scrape WILL be interrupted, and re-running
from zero each time is how a polite scrape turns into an impolite one. An id already in the file is not
re-queried.

**An id Wikidata states neither fact for is recorded EMPTY, not skipped.** Absent and empty mean different
things one level up — `embed-corpus` reads an empty row as "no clause", while a missing key is a title the
scrape never reached — and recording the empty row is also what stops the resume re-querying it forever.

**There is a cheaper path when the facts sidecar already exists.**
`scripts/v2/derive_doc_facts.py` builds the same file out of `facts-<version>.json`, which already holds
both properties as QIDs plus a label map. Validated against a partial scrape of 25,366 titles at 100.00%
on directors and 99.98% on genres. This stage is what runs when there is no sidecar to derive from.
"""
import concurrent.futures
import json
import os
import sys

from . import artifacts
from .contract import StageError, bind
from lib import http, wikidata

NAME = "docfacts"

PRODUCER = "pipeline/docfacts.py"
HOW = "./den stage docfacts --out-dir <dir> --dataset-version <ver>"
#: Writes one file into the out-dir.
PUBLISHES = False
#: Wikidata's public query service, unbilled.
SPENDS = False

#: Ids per SPARQL request. Pinned rather than taken from the command line: the batch membership is part of
#: the cache key, so changing it re-asks for everything already on disk under a different name.
BATCH = 100

#: Batches in flight. WDQS allows a handful of concurrent queries per client and answers 429 beyond that;
#: this is deliberately well under the documented limit, because the thing being optimised is a scrape
#: that must be able to run again, not one run's wall clock.
CONCURRENCY = 3

#: `labels-t02.json` is `--labels` here and to the corpus join, `--vector-labels` to the store writer.
INPUTS = (artifacts.VECTOR_LABELS.called("labels"),)

OUTPUTS = (artifacts.DOC_FACTS,)

BOUND = {bind(entry).name: bind(entry) for entry in INPUTS}


def existing(path):
    """What a previous run already paid for. A file that will not parse is a refusal rather than an empty
    dict: silently starting over is ~770 requests nobody asked for, and it would also overwrite the rows
    that ARE there."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            rows = json.load(handle)
    except ValueError as broken:
        raise StageError(f"docfacts: {path} exists and does not parse ({broken}). Refusing to start over "
                         f"— that is the whole scrape again. Restore it, or delete it deliberately.")
    if not isinstance(rows, dict):
        raise StageError(f"docfacts: {path} is not an object keyed mediaType:tmdbId.")
    return rows


def outstanding(labels_path, have):
    """`{media: [tmdbId]}` for the titles not in the file yet, in the labels' own order."""
    with open(labels_path, encoding="utf-8") as handle:
        labels = json.load(handle)
    records = labels.get("records")
    if not records:
        raise StageError(f"docfacts: {labels_path} names no records, so there is no corpus to scrape "
                         f"facts for. Build it with: taxonomy-backfill finalize")
    todo = {}
    for record in records:
        media, tmdb_id = record["mediaType"], record["tmdbId"]
        if f"{media}:{tmdb_id}" not in have:
            todo.setdefault(media, []).append(tmdb_id)
    return todo


def batches(todo):
    """`(media, [ids])` slices, media in a fixed order so two runs ask for the same batches.

    The membership of a slice is part of the SPARQL cache key, so a run that grouped the ids differently
    would re-ask for every title already on disk under a name the cache has never seen.
    """
    out = []
    for media in sorted(todo):
        ids = todo[media]
        for start in range(0, len(ids), BATCH):
            out.append((media, ids[start:start + BATCH]))
    return out


def write(path, rows):
    """The whole file, keys sorted, compact — byte-stable for one set of rows."""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(rows, sort_keys=True, separators=(",", ":"),
                                ensure_ascii=False).replace("/", r"\/"))


def run(ctx, cache=None):
    """Scrape what is missing. Returns the file."""
    labels = ctx.require(BOUND[artifacts.VECTOR_LABELS.name].artifact)
    path = ctx.path(artifacts.DOC_FACTS)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    rows = existing(path)
    before = len(rows)
    todo = outstanding(labels, rows)
    wanted = sum(len(ids) for ids in todo.values())
    print(f"  doc-facts: {before} cached, {wanted} to fetch", file=sys.stderr)

    cache = wikidata.cache_for() if cache is None else cache
    done = 0
    work = batches(todo)
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        for index in range(0, len(work), CONCURRENCY):
            group = work[index:index + CONCURRENCY]
            try:
                answers = list(pool.map(lambda job: wikidata.doc_facts(job[1], job[0], cache), group))
            except (wikidata.WikidataError, http.HTTPError) as refusal:
                raise StageError(
                    f"docfacts: the scrape stopped at {done}/{wanted} ({refusal}). Everything it had "
                    f"already paid for is in {path}; re-run to continue from there.") from None
            for (media, ids), found in zip(group, answers):
                for tmdb_id in ids:
                    fact = found.get(tmdb_id) or {}
                    rows[f"{media}:{tmdb_id}"] = {"directors": fact.get("directors") or [],
                                                  "genres": fact.get("genres") or []}
                done += len(ids)
            # Written per group, not at the end: an interrupted scrape keeps everything it paid for.
            write(path, rows)
            if done % 1000 < BATCH * CONCURRENCY:
                print(f"  doc-facts {done}/{wanted}…", file=sys.stderr)

    if not rows:
        raise StageError(f"docfacts: nothing was written to {path}, so the embed pass would compose the "
                         f"FULL document shape instead of the CC0 one — a different vector space, with "
                         f"nothing in the output saying so.")
    with_director = sum(1 for row in rows.values() if row["directors"])
    with_genre = sum(1 for row in rows.values() if row["genres"])
    print(f"  doc-facts: {len(rows)} rows ({with_director} with a director, {with_genre} with a genre) "
          f"-> {path}", file=sys.stderr)
    return path
