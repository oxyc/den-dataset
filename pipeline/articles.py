#!/usr/bin/env python3
"""The ARTICLE DUMP — each grounded title's whole Wikipedia article as prose, one JSON object per line.

The classify pass reads the article; the embed pass reads the extracted plot. Two consumers with
genuinely different needs, and this feeds the first. Keeping them apart also means a future extractor bug
degrades similarity without silently corrupting labels.

**Why the whole article and not the plot.** The section rules are a heuristic — hard and soft exclusion
lists, parent-scope inheritance, serial-heading detection — and they mis-fire; pre-filtering with a weaker
mechanism to protect a stronger one is backwards. More importantly the LEAD is the only thing that says
what the article IS: given an extracted plot, nothing can tell that prose about Heathcliff came from the
novel's page rather than a film's, which is how six tmdbIds came to share 46,936 characters of Wuthering
Heights. Given the article, the first sentence settles it.

**It is a re-read, not a new scrape.** Every article was already fetched once to find its plot, and
`plotArticle` + `plotLanguage` were recorded per title, so this asks for the same URL and the response
cache answers nearly all of it. That is also why it goes through `lib/wikipedia.py` rather than reading
the cache directly: the key is a hash of the request, so reconstructing one by hand would be a second
implementation of something that already works, and wrong the first time a parameter moves.

**Resumable by re-reading its own output**, so a kill costs at most the titles in flight. Which is the
reason the output is APPENDED rather than rewritten: 445 MB of articles is hours of polite fetching.

**Only a title with a Wikipedia plot is dumped.** That is the ToS rule, not a coverage decision — a title
without one has no article recorded to fetch, and its prose would be TMDB's, which may not reach an LLM.

**The output is byte-stable for one set of batches.** The Swift dumper's was not, in two ways that look
like nothing and are not: rows were written in task-completion order within each group of four, and its
encoder emitted each object's keys in an order that changes per process — two runs over one input
produced two files with identical content and different bytes. That matters because the classify pass
hashes this file into its manifest and costs $20.47, so a re-dump moved the hash for a reason that is not
a change in what any question is asked about. The same defect was found and fixed for the poster sidecar
and left in place here.
"""
import concurrent.futures
import json
import os
import re
import sys

from . import artifacts
# Aliased: this module has its own `fetch`, and the stage that owns the enriched batches is only needed
# for the rule its refusal points at.
from . import fetch as fetch_stage
from .contract import StageError
from lib import http, wikidata, wikipedia

NAME = "articles"

PRODUCER = "pipeline/articles.py"
HOW = "./den stage articles --out-dir <dir>"
#: Appends to a file in the out-dir. A repeat costs only the titles that are not in it yet.
PUBLISHES = False
#: Wikipedia's public API, unbilled — and mostly answered from the cache the plot pass already filled.
SPENDS = False

#: Gentle on the public API, the same width the plot pass used. Not a throughput knob: the politeness is
#: the point, and the cache is what makes the pass fast.
GATE = 4

#: `batch-<n>.json`, read in BATCH-NUMBER order. `sorted()` is lexicographic, so `batch-99` would come
#: after `batch-177`; that matters because a key can appear in more than one batch — 1,855 of 59,218 do,
#: and 505 of them disagree about `hasWikiPlot`, which decides whether a title has an article to dump at
#: all. Newest wins, which is what `finalize` and the embed pass already do.
BATCH = re.compile(r"^batch-(\d+)\.json$")

#: Ids per Wikidata target lookup, the batch size of the pipeline's other per-id queries. Membership is
#: part of the cache key, so it is fixed here rather than taken from the command line.
TARGET_BATCH = 100

#: What a row's `title` and `year` came from. Rows written before it existed carry TMDB's, and nothing
#: else in a row tells the two apart.
TARGET_SOURCE = "wikidata"

INPUTS = (artifacts.ENRICHED,)
OUTPUTS = (artifacts.ARTICLES,)


def ordered_batches(directory):
    """The batch files, oldest first, by batch NUMBER rather than by name."""
    numbered = []
    for name in os.listdir(directory):
        found = BATCH.match(name)
        if found:
            numbered.append((int(found.group(1)), name))
    return [name for _number, name in sorted(numbered)]


def key(row):
    """`"movie:95"`. TMDB's two id spaces overlap — movie 95 is Armageddon, series 95 is Buffy — so a bare
    id conflates them, and this file is joined back on exactly this string."""
    return f"{row['mediaType']}:{row['tmdbId']}"


def already_dumped(path):
    """The keys the output already holds. A line that does not parse is not a key; it is also not a reason
    to re-dump the corpus, so it is skipped rather than raised on."""
    done = set()
    if not os.path.exists(path):
        return done
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
                done.add(f"{row['mediaType']}:{row['tmdbId']}")
            except (ValueError, KeyError, TypeError):
                continue
    return done


def wanted(enriched_dir, done):
    """The grounded titles still to dump, newest record per title, in batch order.

    Newest wins because the same key appears in several batches disagreeing about whether a plot was
    found: `tv:37854` (One Piece) is false in batch-175 and true in batch-176. Read oldest-first,
    first-wins, the stale no-plot record beats the later one that found the article.
    """
    out, seen = [], set()
    for name in reversed(ordered_batches(enriched_dir)):
        with open(os.path.join(enriched_dir, name), encoding="utf-8") as handle:
            records = json.load(handle)
        for record in records:
            item = key(record)
            if item in seen:
                continue
            seen.add(item)
            # ToS: only a title with a Wikipedia plot may reach an LLM, and only such a title has a
            # recorded article to fetch.
            if not record.get("hasWikiPlot") or not record.get("plotArticle"):
                continue
            if item in done:
                continue
            out.append(record)
    return out


def line(row):
    """One row as the file's bytes.

    Compact, UTF-8 and with `/` escaped, which is not decoration: `articles.jsonl` is 445 MB that the
    Swift pass wrote and the classify pass HASHES INTO ITS MANIFEST, and this stage appends to it. A row
    serialised in a second style is the same JSON and different bytes, so the file would carry two
    spellings and the hash would move for a reason that is not a change in what any question is asked
    about. `/` only ever appears inside a string in JSON, so escaping it globally is safe.
    """
    return json.dumps(row, ensure_ascii=False, separators=(",", ":")).replace("/", r"\/")


def targets(records, cache):
    """`key -> {"title", "year"}` from Wikidata for every record about to be dumped.

    `title` and `year` are the classify pass's requested target — what it judges "is this article about
    this work?" against — and every row once carried TMDB's, which is how this file came to hold TMDB
    Content. They come from Wikidata instead: the item's label, and its earliest publication or start year.

    Asked before anything is written. A lookup that fails is a refusal, not a row with no target: the
    classify pass would pay to judge an article against a blank name.
    """
    ids = {}
    for record in records:
        ids.setdefault(record["mediaType"], set()).add(record["tmdbId"])
    out = {}
    for media in sorted(ids):
        ordered = sorted(ids[media])
        for start in range(0, len(ordered), TARGET_BATCH):
            try:
                found = wikidata.targets(ordered[start:start + TARGET_BATCH], media, cache)
            except (http.HTTPError, wikidata.WikidataError) as error:
                raise StageError(f"articles: the Wikidata lookup that names each title failed ({error}). "
                                 f"Nothing was written; re-run once Wikidata answers.") from None
            out.update({f"{media}:{tmdb_id}": target for tmdb_id, target in found.items()})
    return out


def row(record, found, target):
    """One output line. The key order is the reader's: `scripts/v2/` joins this file by `mediaType` and
    `tmdbId`, and `extractorArticleRevId` is what says whether the article moved since the plot was
    taken.

    `title` and `year` are Wikidata's (`target`), never the enriched record's, which are TMDB's. Both are
    written even when unknown: `run_combined.py` fills an ABSENT `year` from the enriched batches — TMDB's
    year again.

    A field the enrichment did not record is written as an explicit `null`, never left out — and for
    `extractorArticleRevId` that is a decision, not a serialiser default. `run_combined.py` and
    `audit_combined.py` read it as `rec.get("extractorArticleRevId", rec.get("revId"))`: an OMITTED key
    falls back to the revision this dump read, so a title with no `plotRevId` (~17k of the grounded corpus)
    would claim its plot was extracted from the very revision the classifier reads, and
    `sectionAuditSameRevision` would vouch for it. That is unknown, not true. `null` says unknown, and it
    is what the shipped classify pass actually computed from. The Swift dumper omitted the key.
    """
    return {
        "mediaType": record["mediaType"],
        "tmdbId": record["tmdbId"],
        "title": target.get("title"),
        "year": target.get("year"),
        "targetSource": TARGET_SOURCE,
        "article": record["plotArticle"],
        "language": record.get("plotLanguage") or "en",
        "resolvedArticle": found["resolvedArticle"],
        "revId": found["revId"],
        "extractorArticleRevId": record.get("plotRevId"),
        "sections": found["sections"],
        "plotSections": record.get("plotSections") or [],
        # CODE POINTS, which is what every consumer of this number already counts:
        # `scripts/v2/run_combined.py` records `articleChars` as `len(rec["text"])` and
        # `audit_combined.py` — the only thing between a corrupted bundle and a published dataset —
        # refuses a row whose recorded count does not equal that. The Swift dumper counted GRAPHEME
        # CLUSTERS instead, so its number disagreed with the auditor's on any article carrying a
        # combining mark: measured, 7 rows in 3,000.
        "chars": len(found["text"]),
        "text": found["text"],
    }


class Unreachable:
    """A fetch that failed in transport: an outage, a rate limit that outlasted the retries, a body that is
    not JSON. Kept apart from None — an article that is not there — because the two call for opposite
    things: one is a fact about the title, the other says nothing about it."""

    def __init__(self, error):
        self.error = error


def fetch(record, cache):
    """One article; None when there is nothing readable there; `Unreachable` when Wikipedia could not be
    asked.

    Neither miss is written as an empty article — a row with no prose in it is a title the classify pass
    would pay to read and learn nothing from — so both are left for a later run. They are COUNTED apart,
    because an outage reported as "no article" is a normal-looking run over an empty file.
    """
    try:
        return wikipedia.article_prose(record["plotArticle"], record.get("plotLanguage") or "en", cache)
    except (http.HTTPError, ValueError) as error:
        return Unreachable(error)


def run(ctx, cache=None):
    """Dump every grounded article that is not already dumped. Returns the output path."""
    enriched = ctx.require(artifacts.ENRICHED)
    if not os.path.isdir(enriched):
        raise StageError(f"articles: {enriched} is not a directory of enriched batches. This stage reads "
                         f"which article each title was grounded on; nothing else records that.")
    out = ctx.path(artifacts.ARTICLES)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)

    done = already_dumped(out)
    todo = wanted(enriched, done)
    if not todo and not done:
        raise StageError(
            f"articles: {enriched} holds no title with a recorded Wikipedia article, so there is nothing "
            f"to dump and the classify pass would have nothing to read. Enrichment records `plotArticle` "
            # Read off the stage that owns the batches rather than spelled here. It was
            # an enrich wrapper script until the drain became a stage, and a copy of that string would have
            # gone on sending an operator to a script the pipeline no longer runs.
            f"when it grounds a title — build the batches with: {fetch_stage.HOW}")
    # Applied after the refusal above, so `--limit 0` asks for nothing rather than reading as an empty
    # enrichment — and 0 means zero here, as it does to `enrich` and the embed stage.
    if ctx.limit is not None:
        todo = todo[:ctx.limit]
    print(f"  articles: {len(done)} already dumped, {len(todo)} to fetch", file=sys.stderr)

    cache = wikipedia.cache_for() if cache is None else cache
    named = targets(todo, cache)
    written = missing = failed = 0
    last_error = None
    with open(out, "a", encoding="utf-8") as handle:
        with concurrent.futures.ThreadPoolExecutor(max_workers=GATE) as pool:
            # Submitted in batch order and consumed in the SAME order, so two runs over one set of batches
            # write one file. Completion order would be a different file every time, for identical rows.
            for index in range(0, len(todo), GATE):
                slice_ = todo[index:index + GATE]
                for record, found in zip(slice_, pool.map(lambda r: fetch(r, cache), slice_)):
                    if isinstance(found, Unreachable):
                        failed += 1
                        last_error = found.error
                        continue
                    if found is None:
                        missing += 1
                        continue
                    handle.write(line(row(record, found, named.get(key(record), {}))) + "\n")
                    written += 1
                if written and written % 2000 < GATE:
                    print(f"  articles: dumped {written} (no article {missing}, unreachable {failed})…",
                          file=sys.stderr)
    print(f"  articles: {written} written, {missing} with no article, {failed} unreachable -> {out}",
          file=sys.stderr)
    # The classify pass reads this file next and pays per title. A run where most fetches never reached
    # Wikipedia is an outage, and finishing it normally hands the paid step a short file as if it were
    # the corpus.
    if failed > written + missing:
        raise StageError(
            f"articles: {failed} of {written + missing + failed} fetches failed in transport (last: "
            f"{last_error}). That is Wikipedia being unreachable, not {failed} titles without an article. "
            f"The {written} that did arrive are in {out}; re-run to fetch the rest once it answers.")
    return out
