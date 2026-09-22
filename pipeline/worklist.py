#!/usr/bin/env python3
"""The WORKLIST — the universe of titles everything after it is drawn from.

Which ids exist, in which order, and which of them are already published. The rule used to live in
`taxonomy-backfill worklist` and this module handed it files; it is the rule now (oxyc/den-dataset#27).

**The universe is three different questions, and `--mode` asks whichever one is wanted.**

  * `export` — TMDB's daily id dump, parsed. Every id that exists: the full run's universe. Offline, and
    the only mode that is deterministic end to end.
  * `discover` — `/discover` sorted `vote_count.desc`, the highest-vote titles first. The pilot seed.
  * `delta` — titles released since `--since` that clear the discovery floor and have no genres & moods
    yet. The daily freshness pass `scripts/delta-run.sh` drives.

**The mode is not defaulted.** For a full run the cheapest-looking answer means enriching the 500
highest-vote titles and calling that the catalogue; for a delta it means re-enriching everything already
published. Enrichment is the step that costs money per title, so a run that did not say which universe it
is building is refused rather than given one.

**An empty worklist is a refusal in every mode but `delta`.** A truncated dump, an error page saved as
one, or a still-gzipped file parses to nothing and writes `[]` — and `enrich` reads an empty worklist as a
finished run, which is exactly what a successful pass looks like. In `delta` an empty list is the answer
on a quiet day, so refusing it there would fail the daily pass for doing its job. For `export` the same
silence is checked one line finer: the ids written against the dump's own line count, because a dump that
arrived half-written parses to half a catalogue, and half a catalogue looks like a catalogue.

**A `discover` or `delta` row carries the vote count `/discover` stated.** The admission gate needs it
(`pipeline/floors.py`), and this stage is where TMDB already answered the question — so `enrich` judges a
title by the count on its worklist row rather than asking TMDB for the same number again per title. An
`export` row has none: the daily dump states popularity, not votes.

**A delta writes the same two filenames as a full run**, which is why `scripts/delta-run.sh` keeps its
lists in `$OUT_DIR/delta/`. Point the two outputs there with `--set` rather than moving the whole out-dir,
so the genres & moods a delta must skip are still found beside everything else: forty delta rows written over a
47k-title one do not corrupt anything — they end the full run, quietly, as a batch that reports nothing
remaining.
"""
import json
import os
import sys

from . import artifacts, floors as floor_rules, genres_moods
from .contract import StageError, bind
from lib import cache as caching
from lib import tmdb as tmdb_api

NAME = "worklist"

#: The rule this stage runs. It is this file now, so the producer registry names what actually executes.
PRODUCER = "pipeline/worklist.py"
HOW = "./den stage worklist --mode export --out-dir <dir>"
#: Writes two files into the out-dir. Cheap to repeat — the cost is downstream, at `enrich`.
PUBLISHES = False
#: TMDB's own API and its public daily dumps, neither of them billed. What this stage DECIDES is expensive
#: — a universe of 1.2M ids is an enrichment nobody meant to start — which is why the mode is refused
#: rather than defaulted: the size is the choice, not the running.
SPENDS = False

DISCOVER, EXPORT, DELTA = "discover", "export", "delta"
#: In the order the stage's own refusal lists them.
MODES = (DISCOVER, EXPORT, DELTA)

#: The TMDB count `/discover` enumerates at: the LOWEST floor any admission tier uses (`pipeline/floors.py`),
#: not the worldwide 50. Discovery only enumerates; `enrich` admits, on TMDB's count or IMDb's. A title
#: enumerated at 50 could never reach the regional tier's 15, nor be admitted on an IMDb count TMDB
#: undercounts — `Elkürtük` has 44 TMDB votes and 40,939 on IMDb. Below this nothing is looked at at all:
#: a brand-new release with no votes has no plot worth classifying, and every id listed costs a detail call
#: each day it stays below the floors, since below-floor ids are never checkpointed.
VOTE_FLOOR = floor_rules.DEFAULT.lowest_tmdb

#: How many titles a `discover` seed collects. The pilot's number, pinned here for the same reason.
DISCOVER_COUNT = 500

#: Highest-vote first. A discover universe is a BUDGET — whatever it does not reach is not enriched — so
#: the order is the selection, not a presentation choice.
SORT_BY = "vote_count.desc"

#: media -> (its slice of the daily dump, the worklist built from it). `enrich` takes one media at a time
#: and refuses a mixed list, so the universe is built once per entry here.
MEDIA = {
    "movie": (artifacts.EXPORT_MOVIE, artifacts.UNIVERSE_MOVIE),
    "tv": (artifacts.EXPORT_TV, artifacts.UNIVERSE_TV),
}

#: `genres-moods.json` is `--known` here: the titles already labelled, which a delta skips. Only a delta
#: reads it, and it is the previous run's — a delta extends an out-dir, while an export or discover universe
#: reads nothing, so a fresh out-dir starts.
INPUTS = (
    artifacts.EXPORT_MOVIE.called("file"),
    artifacts.EXPORT_TV.called("file"),
    artifacts.GENRES_MOODS.called("known"),
)

OUTPUTS = (artifacts.UNIVERSE_MOVIE, artifacts.UNIVERSE_TV)

BOUND = {bind(entry).name: bind(entry) for entry in INPUTS}


def mode(ctx):
    """The universe this run builds, as the operator named it."""
    if ctx.mode not in MODES:
        raise StageError(
            f"worklist: --mode is {ctx.mode or 'unset'}, and it decides which universe this builds: "
            f"{EXPORT} (every id in TMDB's daily dump — the full run), {DISCOVER} (the highest-vote "
            f"titles, the pilot seed), {DELTA} (what is new since --since and not already published). "
            f"Enrichment is billed per title, so this is not defaulted.")
    return ctx.mode


def entry(tmdb_id, media, votes=None):
    """One row of a worklist. `enrich` refuses a list that mixes media, so the type rides on every row.

    `voteCount` is TMDB's count as `/discover` stated it when this universe was built — the number the
    admission gate judges the title by (`pipeline/floors.py`). It rides here because `/discover` already
    answered it: reading it back off a per-title detail call asks TMDB the same question a second time,
    which is the call oxyc/den-dataset#53 is emptying. A row built from the daily export dump carries no
    count, because the dump states popularity and not votes; the key is then ABSENT rather than zero,
    since zero is below every floor and would read as a title TMDB refused.
    """
    row = {"tmdbId": int(tmdb_id), "mediaType": media}
    if votes is not None:
        row["voteCount"] = int(votes)
    return row


def parse_export(path, media):
    """TMDB's daily id dump — one JSON object per line — as worklist rows, and the lines it offered.

    The count comes back alongside because a line that cannot be read as an id is DROPPED, and dropping
    them quietly is how a dump that arrived half-written becomes half a catalogue with no complaint
    anywhere. The caller compares the two.
    """
    rows, offered = [], 0
    with open(path, "rb") as handle:
        for line in handle:
            if not line.strip():
                continue
            offered += 1
            try:
                tmdb_id = json.loads(line)["id"]
            except (ValueError, KeyError, TypeError):
                continue
            if isinstance(tmdb_id, int):
                rows.append(entry(tmdb_id, media))
    return rows, offered


def known_ids(path, media):
    """The tmdbIds of this media that already have genres & moods — the titles a delta must skip.

    Without them the pass re-enriches the whole published catalogue, at the per-title price the vote floor
    exists to bound, and nothing in its output says that is what happened. A file with no titles is not
    "nothing published" — it is the wrong file, and read as an empty set it skips nothing, so `read`
    refuses it.
    """
    try:
        records = genres_moods.read(path)
    except StageError as refusal:
        raise StageError(f"worklist: {refusal}. A delta skips the titles it names, so it cannot run "
                         f"without them.") from None
    return {record["tmdbId"] for record in records.values() if record["mediaType"] == media}


def collect(client, media, params, limit=None):
    """Page a `/discover` query, newest page last, de-duped, in the order TMDB returned them.

    Stops at TMDB's 500-page ceiling or at `limit`. A page whose body carries no `results` is a refusal
    inside the client, not an empty page — see `lib/tmdb.py`.

    Each row's `vote_count` is kept. It is the number the query SELECTED on, so it is already the gate's
    answer for that title; a row that states none is written without one rather than as zero.
    """
    rows, seen, page = [], set(), 1
    while page <= tmdb_api.MAX_PAGES and (limit is None or len(rows) < limit):
        items, _page, total = client.discover(media, params, page)
        for item in items:
            tmdb_id = item.get("id")
            if isinstance(tmdb_id, int) and tmdb_id not in seen:
                seen.add(tmdb_id)
                votes = item.get("vote_count")
                rows.append(entry(tmdb_id, media,
                                  votes if isinstance(votes, int) and not isinstance(votes, bool) else None))
        if page >= total:
            break
        page += 1
    return rows[:limit] if limit is not None else rows


def universe(ctx, media, client=None):
    """The rows for one media, in the mode this run asked for."""
    chosen = mode(ctx)
    export, _out = MEDIA[media]
    if chosen == EXPORT:
        dump = ctx.require(export)
        rows, offered = parse_export(dump, media)
        if len(rows) != offered:
            raise StageError(
                f"worklist: {dump} holds {offered} lines and {len(rows)} of them read as an id. A line "
                f"that cannot be read as one is dropped without a word, so the difference is a universe "
                f"with a hole in it, not a filter.")
        return rows

    # A missing key or a dead upstream is a REFUSAL, not a traceback: `den` prints a StageError and stops,
    # and an operator reading a stack trace from inside an HTTP client learns nothing about which stage
    # could not run.
    try:
        client = client or tmdb_api.TMDB()
    except tmdb_api.TMDBError as refusal:
        raise StageError(f"worklist: {refusal}") from None
    if chosen == DISCOVER:
        params = tmdb_api.discover_params(media, vote_count_gte=VOTE_FLOOR, sort_by=SORT_BY)
        return collect(client, media, params, limit=DISCOVER_COUNT)

    if not ctx.since:
        raise StageError("worklist: --mode delta needs --since YYYY-MM-DD — the window it collects titles "
                         "from. There is no default window: a delta with no date is either every title "
                         "ever released or none of them.")
    # `/discover` with a release-date window rather than `/movie/changes`. `/changes` is a firehose of ids
    # with no vote or date signal, so it costs one detail lookup PER id just to discover that almost all
    # of them are below the floor. A dated `vote_count.desc` slice selects the same titles in a few paged
    # calls. The trade: a re-release or a late metadata fix on an OLD title is not picked up — acceptable,
    # because a periodic full pass covers drift and paying per id daily does not scale.
    known = known_ids(ctx.require(BOUND[artifacts.GENRES_MOODS.name].artifact), media)
    params = tmdb_api.discover_params(media, vote_count_gte=VOTE_FLOOR, release_date_gte=ctx.since,
                                      sort_by=SORT_BY)
    return [row for row in collect(client, media, params) if row["tmdbId"] not in known]


def write(path, rows):
    """The worklist, pretty-printed with sorted keys and ` : ` — the Swift encoder's layout.

    Byte-stable across runs: the file is read by a human as often as by `enrich`, and a key order that
    moves makes every diff of two universes unreadable. Swift's layout rather than Python's so a universe
    diffs cleanly against every one the Swift command wrote. The one place it does not follow Swift is an
    empty list: `[]`, where Swift wrote `[\\n\\n]`. Nothing hashes a worklist, both parse to the same
    nothing, and an empty one only ever comes from a quiet delta, never from a list worth diffing.

    Swapped in whole rather than written in place: an interrupted write leaves a truncated universe, and
    `enrich` drains a short worklist as a finished run.
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    body = json.dumps(rows, indent=2, separators=(",", " : "), sort_keys=True, ensure_ascii=False)
    caching.write_atomically(path, body.encode("utf-8"))


def run(ctx, client=None):
    """Build the universe for both media. Returns the two worklists.

    One media at a time, each written before the next is built: a second media that cannot be built is not
    a reason to leave the first one unwritten.
    """
    chosen = mode(ctx)
    built = []
    for media, (_export, artifact) in MEDIA.items():
        rows = universe(ctx, media, client)
        if not rows and chosen != DELTA:
            raise StageError(
                f"worklist: {chosen} built an empty {media} universe, so there is nothing to enrich. "
                f"`enrich` reads an empty worklist as a finished run rather than as a failure.")
        path = ctx.path(artifact)
        write(path, rows)
        print(f"worklist: {len(rows)} {media} ids -> {path}", file=sys.stderr)
        built.append(path)
    return ", ".join(built)
