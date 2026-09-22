#!/usr/bin/env python3
"""ONE enrichment batch: the next N un-enriched worklist ids, grounded on Wikipedia, written as a batch.

    python3 -m pipeline.enrich --worklist W --out-dir D [--vote-floor 50] [--regional-vote-floor 15]
                               [--imdb-floor 2000] [--regional-imdb-floor 500] [--limit 150] [--exclude-anime]

The drain (`pipeline/fetch.py`) runs this until nothing remains; the daily delta runs it once per media
and reports what is left. Per id: one TMDB detail+keywords+credits call; per media in the batch: one
Wikidata SPARQL mapping the survivors to their articles, one for their languages and one for whether the
works they are based on are films or series; per title: a live Wikipedia plot, which becomes
the record's `overview`. Every one of those goes through `lib/`, so the responses already on disk under
`.cache/` answer a re-run.

**Admission is decided before any of the expensive work**, on TMDB's vote count OR IMDb's — see
`pipeline/floors.py` for the floors and why there are four. TMDB's count comes off the WORKLIST row, which
`/discover` stated when the universe was built; only an export universe, whose dump states popularity and
not votes, falls back to the detail call's. A title TMDB's count leaves short costs one more thing: its
IMDb id, one Wikidata SPARQL per media for all of them together. IMDb's counts are read, compared and
forgotten; none is written into a batch, a log line or the report.

**TMDB's prose never enters the record.** `lib/tmdb.title_record` keeps none of the overview, and a
title with no Wikipedia plot is written `hasWikiPlot: false` with an empty `overview` — nothing downstream
may ground it on anything else.

**The batch file is read by `articles`, `embed`, `genres-moods`, `scripts/backfill-plot-provenance.py` and
the census**, and it is written in the Swift encoder's exact layout (`swift_json`), so a batch this writes
and one the Swift pass wrote diff as data rather than as formatting. Its TMDB fields are the few those
readers need — see `lib/tmdb.title_record`.
"""
import argparse
import concurrent.futures
import datetime
import json
import os
import re
import sys
import threading

from lib import cache as caching
from lib import enterprise, http, imdb, plot, tmdb as tmdb_api, wikidata, wikipedia

from . import floors as floor_rules
from .contract import StageError

LIMIT = 150

#: Which count admitted a title, for the report.
TMDB, IMDB, BOTH = "tmdb", "imdb", "both"

#: Minimum plot length to ground on, in characters. Was 200, justified as "a one-line logline adds little
#: over the TMDB overview it would replace" — but that comparison is gone: `overview` holds a Wikipedia plot
#: or NOTHING, so the trade is "this versus nothing", and 200 rejected real premises (Would You Marry Me? at
#: 165, Disclaimer at 179, Silo at 189). 120 admits those and still rejects the actual loglines ("The film
#: explores the life and career of John le Carré", 55).
WIKI_PLOT_FLOOR = 120

#: Long enough that the title's English article is not worth supplementing from its other-language ones.
#: It no longer decides whether the P144 source work is read: that is read only when the title has no plot
#: of its own that clears `WIKI_PLOT_FLOOR` on any Wikipedia (see `reground`).
OWN_ARTICLE_SUFFICIENT = 1000

#: Wikidata says anime in the LABEL of a P136 genre or a P31 type: `anime film`, `anime television series`,
#: `<genre> anime and manga`, `anime/manga style`. The vocabulary is open — an editor mints a new
#: `<genre> anime and manga` whenever one is needed — so the rule is the word, not a pinned list of Q-ids.
ANIME = "anime"
#: The one label carrying the word that says the opposite: western animation drawn in the style, which is
#: not what someone excluding anime means. `lib/wikidata_facts.genre_map` sets `live-action/animated` aside
#: from its animation rule for the same reason.
NOT_ANIME = "anime-influenced animation"

#: In-flight TMDB detail calls, and titles grounding at once — the second gentle on the public API.
TMDB_WORKERS, WIKI_WORKERS = 8, 4

_BATCH = re.compile(r"^batch-([0-9]+)\.json$")
_log_lock = threading.Lock()


class Aborted(RuntimeError):
    """A batch that wrote NOTHING and is worth running again — the Wikidata mapping failed after retries."""


def checkpoint_path(out_dir):
    return os.path.join(out_dir, "enrich-checkpoint.json")


def batch_path(out_dir, batch_id):
    return os.path.join(out_dir, "enriched", f"batch-{batch_id}.json")


def batches(directory):
    """`[(number, name)]` in batch-NUMBER order. `sorted()` would put `batch-99` after `batch-177`."""
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    return sorted((int(found.group(1)), name) for name in names if (found := _BATCH.match(name)))


def key(media, tmdb_id):
    """`"movie:95"`. TMDB's movie and series id spaces overlap — movie 95 is Armageddon, series 95 is Buffy
    — so every set, map and log line here is keyed by the pair."""
    return f"{media}:{tmdb_id}"


def swift_json(value, indent=""):
    """`value` as Swift's `JSONEncoder([.prettyPrinted, .sortedKeys])` wrote it, byte for byte.

    ` : ` between key and value, two-space indent, `/` escaped, UTF-8 raw, an empty list as `[`, a blank
    line and the closing bracket at the parent's indent, and no trailing newline. A batch the Swift pass
    wrote and one this writes must diff as data, and every existing batch is in this layout.
    """
    inner = indent + "  "
    if isinstance(value, dict):
        if not value:
            return "{\n\n" + indent + "}"
        items = [f'{inner}{_string(name)} : {swift_json(value[name], inner)}' for name in sorted(value)]
        return "{\n" + ",\n".join(items) + "\n" + indent + "}"
    if isinstance(value, list):
        if not value:
            return "[\n\n" + indent + "]"
        return "[\n" + ",\n".join(inner + swift_json(item, inner) for item in value) + "\n" + indent + "]"
    if isinstance(value, str):
        return _string(value)
    return json.dumps(value)


def _string(text):
    return json.dumps(text, ensure_ascii=False).replace("/", "\\/")


def compact(value):
    """Keys sorted, no spaces, `/` escaped — `JSON.write`/`JSON.line`'s bytes, but in ONE key order: the
    Swift report serialised a `Dictionary`, whose order changed from process to process."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).replace("/", "\\/")


def log(out_dir, message):
    """One line of `enrich-log.txt`. `lib/http` errors name the URL without its query, so the TMDB key —
    which rides in the query string — cannot reach this file."""
    with _log_lock:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "enrich-log.txt"), "a", encoding="utf-8") as handle:
            handle.write(message + "\n")


def read_checkpoint(path):
    """The resume state. ABSENT is a first run; present-but-unreadable is a refusal, not a reset — a bare
    fallback would reset a truncated checkpoint to empty and re-enrich the whole universe."""
    if not os.path.exists(path):
        return {"processed": set(), "nextBatch": 1, "totals": {}, "judgedBelow": {}}
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        processed = raw["processed"]
        # The movie-only pilot stored bare ints; they are movie keys.
        processed = {item if isinstance(item, str) else key("movie", int(item)) for item in processed}
        totals = raw.get("totals") or {}
        judged = raw.get("judgedBelow") or {}
        if not isinstance(judged, dict) or not all(isinstance(v, dict) for v in judged.values()):
            raise TypeError("judgedBelow is not a map of verdicts")
        return {"processed": processed, "nextBatch": int(raw.get("nextBatch", 1)),
                "totals": {name: int(totals.get(name, 0)) for name in TOTALS}, "judgedBelow": judged}
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise StageError(f"enrich checkpoint at {path} is unreadable ({error}); refusing to reset progress — "
                         f"restore it, or delete it to intentionally start fresh")


#: `noOverview` is gone with the stub check that counted it — see `run`. A checkpoint that carries one is
#: read without it; the counter counted a rule that no longer exists.
TOTALS = ("anime", "belowFloor", "failures")


def floor_values(floors):
    """The four floors as the checkpoint records a verdict's: a list, so it compares equal after a JSON
    round trip."""
    return [floors.tmdb, floors.regional_tmdb, floors.imdb, floors.regional_imdb]


def below_floor(votes, floors, today, imdb_on):
    """One below-floor verdict as the checkpoint keeps it: the UTC day it was made, the TMDB count and the
    floors it was judged by, and whether IMDb's half of the gate took part. Never IMDb's count — its
    licence keeps that in this process (`lib/imdb`)."""
    return {"on": today, "votes": votes, "floors": floor_values(floors), "imdb": imdb_on}


def standing(judged, today, floors, worklist_votes):
    """The keys whose below-floor verdict still holds for this batch, and so are not asked about again.

    A below-floor verdict is about one day's counts. A vote count only climbs, so it must be judged again
    later — checkpointing it as processed made the rejection permanent, and a 180-day cached detail record
    widened that to months. But left entirely unrecorded it was asked again by every batch of the same
    drain, `remaining` never reached 0, and a universe holding one below-floor title could never drain.

    So a verdict holds only while nothing it was judged on can have moved: it was made TODAY (IMDb's dump
    and `/discover`'s counts are daily), by the SAME floors (a run that lowers one re-judges at once), on
    the TMDB count the worklist row still states (a row that states none — an export row — is judged on the
    detail call's count, which the date covers), and with IMDb's half of the gate on unless it is still off.
    Any of those changing re-asks the title, and a title that now clears a floor is admitted like any other.
    """
    same = {label for label, held in judged.items()
            if held.get("on") == today and held.get("floors") == floor_values(floors)
            and (label not in worklist_votes or worklist_votes[label] == held.get("votes"))}
    # A verdict made without IMDb's counts stands only while there are still none to judge it by. Asked
    # only when such a verdict would otherwise stand: the dump is read once per process anyway.
    if any(not judged[label].get("imdb") for label in same) and imdb_counts(floors)[0] is not None:
        same = {label for label in same if judged[label].get("imdb")}
    return same


def is_anime(labels):
    """Whether Wikidata's P136 genres and P31 types say this title is anime.

    Read off TMDB's keyword 210024 and its Japanese-language Animation catch-all before. Measured against
    that rule over the 47,548 corpus titles with a facts row: 1,129 agree, 99 are TMDB's alone and 16
    Wikidata's. Almost every one of the 99 is anime TMDB tags but Wikidata's P136 does not (`Devilman
    Crybaby`), and the 16 are anime co-productions whose `original_language` is not `ja` — `Ulysses 31`
    (French-Japanese), `Dogtanian` (Spanish-Japanese), `Ox Tales` (Dutch-Japanese). The flag is opt-IN and
    `fetch` never passes it, so nothing shipped turns on the 115 either way.
    """
    return any(ANIME in label.lower() and label.lower() != NOT_ANIME for label in labels)


def is_transient(error):
    """429/408/5xx, or no answer at all. A 404 on a deleted id is an ANSWER, and retrying it forever would
    keep a dead title pending for good."""
    return isinstance(error, http.HTTPError) and (error.status == 0 or http.is_transient(error.status))


def fetch_title(client, media, tmdb_id):
    """`(verdict, record-or-reason)` for one worklist id. Raises `StageError` when TMDB refuses the KEY.

    A 401 or 403 is not an answer about the title. Read as a per-title failure it was checkpointed: one
    batch reported 150 of 150 as failures with `remaining` falling, so a revoked key marched the drain
    through the whole universe marking every title dead.
    """
    try:
        body = client.get(f"/{media}/{tmdb_id}", {"append_to_response": tmdb_api.APPEND})
        return "ok", tmdb_api.title_record(body, tmdb_id, media)
    except http.HTTPError as error:
        if error.status in (401, 403):
            raise StageError(f"enrich: TMDB answered HTTP {error.status} for {error.url} — TMDB_API_KEY is "
                             f"revoked or wrong. Nothing from this batch was written or checkpointed; fix the "
                             f"key and run again.") from error
        return ("deferred" if is_transient(error) else "failure"), str(error)
    except ValueError as error:
        return "failure", f"undecodable detail body: {error}"


def grounded(record, found, article, role):
    """The record grounded on `found`. `plotArticleRole` is the CANDIDATE's role, carried with it rather
    than read off its position: for the 4% of titles with no English article the source work is the only
    candidate and sits first."""
    resolved = found["resolvedArticle"]
    return dict(record, overview=found["text"], hasWikiPlot=True, noPlotReason=None,
                # The RESOLVED article, so a revision refresh compares the page the text came from.
                plotArticle=resolved if resolved is not None else article, plotRevId=found["revId"],
                plotSections=found["sections"], plotLanguage=found["language"], plotArticleRole=role,
                # Unknown — None, written as an absent key — when the source names no page (Enterprise).
                # Absent is UNKNOWN to every reader, never "did not redirect".
                plotArticleRedirected=None if resolved is None else resolved != article)


def source_work(facts):
    """`(article, refused)`: the P144 work the fallback may read, and whether one was refused.

    A novel, a play or a manga tells the story an adaptation tells. A FILM or a SERIES does not stand in
    for another production: it is the original a remake re-shoots (`BÚÉK` and `Stranger in My Pocket` on
    *Perfect Strangers (2016)*, `La oficina` on *The Office*) or the parent a spin-off leaves (`Zen – Grogu
    and Dust Bunnies` on *The Mandalorian*), and its plot, cast and tone describe that one. Of the 543
    titles that stay on their source work after the own-articles-first rule, this refuses 35: 28 that Jev
    judged `other-screen-work` (every one of those), 4 `multi-work-overview`, 2 `correct-screen-work` and
    1 `source-work`.

    Where the mapping's pick is a screen work and the title names another P144 work that is not, that one
    is read instead. A pick `sources` knows nothing about is read as before: its type is unknown, not bad.
    """
    article = facts.get("sourceArticle")
    works = facts.get("sourceWorks") or {}
    if not article or not works.get(article):
        return article, False
    others = sorted(name for name, screen in works.items() if not screen)
    return (others[0], False) if others else (None, True)


def reground(record, facts, cache, token):
    """`(verdict, record, detail)` for one title: `grounded`, `noPlot`, `missed` — a definitive fetch failure,
    kept as a plotless record — or `deferred`, which is not written and not checkpointed. `detail` is the
    error for `missed` and `deferred`, and for `grounded` which source served the plot (`plot.ENTERPRISE`
    or `plot.ACTION_API`).

    The title's OWN articles first: English, then — when English gives less than `OWN_ARTICLE_SUFFICIENT`
    — its other-language sitelinks, the longest of them winning. The Wikidata P144 work it is based on is
    read only when none of those yields a plot that clears `WIKI_PLOT_FLOOR`. That source article is about
    a DIFFERENT work: a novel, or for a spin-off or remake the parent screen work (Gen V is based on The
    Boys, the 2015 Limitless series on the novel The Dark Fields). Letting it compete on length made it win
    wherever the title's own premise was short, and the classify pass judged 1.0% of the 1,986 texts it
    won to be about the requested title, against 98.7% for own-article text — at every plot length,
    including under 300 characters. It stays as the last resort for an adaptation whose own article has no
    story in it ("Attack on Titan (TV series)"), recorded as `source-work` so every reader can tell.

    A sitelink that REDIRECTS is not the title's article either: Wikidata links an item to a redirect when
    its page was merged into another work's (`Jarhead 2: Field of Fire` → `Jarhead (film)`, `Naruto:
    Shippūden` → `Naruto (TV series)`), and 7 of 99 such texts were judged the requested title. It is
    treated as no article on that wiki. Only a redirect the fetch SAW can be refused — the Enterprise path
    names no page, so its answer is taken as it comes.

    A source work that is itself a film or a series is not read at all (`source_work`). A title left with
    no plot by that refusal is `sourceIsScreenWork`, whatever its own articles held: no heading rule or
    threshold changes it, only an own plot being written.
    """
    facts = facts or {}
    source, refused = source_work(facts)
    # Runtime and creators ride the same hop, so they fold in for EVERY title, plot or not. `createdBy` is
    # Wikidata's P170 or nothing: it is composed into the embedding document, and a TMDB fallback here put
    # TMDB's names into the shipped vectors of 3,353 titles. `overview` starts EMPTY — it holds a Wikipedia
    # plot or nothing.
    record = dict(record, overview="", hasWikiPlot=False, plotSections=[],
                  createdBy=facts.get("creators") or [],
                  runtimeMinutes=facts.get("runtimeMinutes"))
    candidates = [(facts[name], role) for name, role in (("article", "own"), ("sourceArticle", "source-work"))
                  if facts.get(name)]
    by_language = facts.get("articlesByLang") or {}
    # `noArticle` only when there is no article ANYWHERE to read. The Swift pass returned it as soon as both
    # English candidates were missing, before the other-language fallback — so the fallback never ran for
    # the titles it was written for, the ones with no English article: in the replay, 215 of 348 `noArticle`
    # titles had a sitelink on a wiki it reads.
    if not candidates and not by_language:
        return "noPlot", dict(record, noPlotReason="noArticle"), None
    best, saw_section, saw_article = None, False, False

    def read(article, language):
        """The plot, or None — and neither a page the wiki does not have nor a sitelink that redirects into
        another page is an article that was read."""
        nonlocal saw_article
        try:
            found = plot.plot(article, language, cache, token)
        except plot.NoPage:
            return None
        if found is not None and found["resolvedArticle"] not in (None, article):
            return None
        saw_article = True
        return found

    def consider(article, language, role):
        """Read one candidate into `best`; True once it alone is enough to stop looking."""
        nonlocal best, saw_section
        found = read(article, language)
        if found is None:
            return False
        saw_section = True
        # Strictly longer: at equal length the earlier candidate stays.
        if best is None or len(found["text"]) > len(best[0]["text"]):
            best = (found, article, role)
        return len(found["text"]) >= OWN_ARTICLE_SUFFICIENT

    try:
        enough = bool(facts.get("article")) and consider(facts["article"], "en", "own")
        # No English article, or a thin one: two thirds of the plotless films have none, and half of THOSE
        # have one elsewhere. The title's own languages first — right 8 times in 15 — then the rest, since
        # four of the misses were English-language films covered by the German or Italian Wikipedia.
        # Still this title's OWN article — `articlesByLang` is its sitelinks, never the source work's.
        #
        # The languages are Wikidata's P364, ALL of them, where this read TMDB's single `original_language`.
        # A co-production states several and TMDB picks one, so the list is the better ordering as well as
        # the CC0 one. Measured over the 12,611 corpus titles grounded this way: 11,142 keep TMDB's code
        # inside the preferred block, 1,164 have no P364 and fall through to code order, and 305 lose it —
        # of which 87 were actually served by the language P364 does not name. The order only decides which
        # wiki is READ first; the longest article still wins, so a miss costs a fetch, not a plot.
        if not enough and by_language:
            preferred = list(facts.get("languages") or ())
            for language in preferred + sorted(code for code in by_language if code not in preferred):
                article = by_language.get(language)
                if article and consider(article, language, "own-other-language"):
                    break
        if source and (best is None or len(best[0]["text"]) < WIKI_PLOT_FLOOR):
            consider(source, "en", "source-work")
    except http.HTTPError as error:
        if is_transient(error):
            return "deferred", None, error
        # Definitive — a 4xx that is not a throttle, and not the action API's "no such page" (a 200, read
        # above). Kept, plotless, and worth nothing more than a record.
        return "missed", dict(record, noPlotReason="fetchFailed"), error
    if best is None or len(best[0]["text"]) < WIKI_PLOT_FLOOR:
        # Whether ANY candidate had a describing section is the difference between "a heading rule would
        # reach this" and "the floor rejected it", and whether any candidate EXISTED is the difference
        # between those and "no article": every sitelink stale. Each wants a different re-run — a heading
        # rule, a threshold, a fresh Wikidata mapping — and a retry fixes none of them, so a missing page is
        # never `fetchFailed`.
        reason = ("sourceIsScreenWork" if refused else "belowFloor" if saw_section else "noSection" if saw_article
                  else "noArticle")
        return "noPlot", dict(record, noPlotReason=reason), None
    found, article, role = best
    return "grounded", grounded(record, found, article, role), found.get("source")


def written(record):
    """The record as the batch carries it. An unknown is an ABSENT key, as the Swift encoder wrote it."""
    return {name: value for name, value in record.items() if value is not None}


def recovered(out_dir, batch_id, survivors):
    """Keys this batch holds that an EARLIER batch already holds — warned about, not refused.

    Re-covering is legitimate when a title is deliberately re-enriched, so a refusal would block the pass
    that fixes a stale record. But silence is how 1,855 keys came to sit in more than one batch, 505 of them
    disagreeing about `hasWikiPlot`.
    """
    seen = {}
    directory = os.path.join(out_dir, "enriched")
    for number, name in batches(directory):
        if number == batch_id:
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as handle:
                rows = json.load(handle)
            for row in rows:
                seen[key(row["mediaType"], row["tmdbId"])] = number
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return [f"{key(r['mediaType'], r['tmdbId'])} (batch {seen[key(r['mediaType'], r['tmdbId'])]})"
            for r in survivors if key(r["mediaType"], r["tmdbId"]) in seen]


def unrecorded(out_dir, next_batch):
    """Keys in batches numbered `next_batch` or later: written, and never recorded by the checkpoint.

    The batch is written before the checkpoint, and a run killed between the two left its batch on disk
    with the checkpoint still naming it as next — so the next run took the same ids again and wrote them
    into the batch after it, 300 keys twice. Those keys ARE enriched; treating them as processed makes that
    death harmless. What the lost checkpoint would have added to `totals` stays lost: they are counters.
    """
    found = set()
    directory = os.path.join(out_dir, "enriched")
    for number, name in batches(directory):
        if number < next_batch:
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as handle:
                found.update(key(row["mediaType"], row["tmdbId"]) for row in json.load(handle))
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise StageError(f"enrich: {name} is on disk and not in the checkpoint, and it cannot be read "
                             f"({error}) — so nothing says which titles it holds. Restore or remove it.")
    return found


def read_worklist(path):
    """`([(media, tmdbId)], {key: voteCount})`, in the worklist's order.

    The counts are the TMDB votes `/discover` stated when the universe was built (`pipeline/worklist.entry`)
    — what the admission gate judges by. A row that states none is absent from the map, never zero: the
    export dump carries no count, and zero is below every floor.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            rows = json.load(handle)
        entries = [("tv" if row["mediaType"] == "tv" else "movie", int(row["tmdbId"])) for row in rows]
        votes = {key(*pair): int(row["voteCount"]) for pair, row in zip(entries, rows)
                 if row.get("voteCount") is not None}
        return entries, votes
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise StageError(f"enrich: {path} is not a worklist of {{tmdbId, mediaType}} rows ({error})")


def imdb_counts(floors):
    """`(ratings, note)` — IMDb's counts for this batch, or `None` and why not.

    A dump that cannot be had turns the IMDb half of the gate OFF for the batch; it does not refuse it. The
    union's TMDB half is exactly the floor this pipeline ran on before IMDb was asked, so nothing it admits
    is lost, and a title only IMDb would have admitted is judged below the floor — a verdict recorded as
    made without IMDb, so the next batch with a dump judges it again (`standing`). Refusing instead would
    stop the daily pass on a third party's outage for titles that are merely deferred. Off is never SILENT:
    the note goes into the report, the log and stderr.
    """
    try:
        ratings = imdb.ratings(floors.lowest_imdb)
    except imdb.Unavailable as error:
        return None, f"off: {error}"
    return ratings, ratings.note


def tmdb_votes(label, record, worklist_votes):
    """The TMDB count the gate judges one title by, or None when nothing states one.

    The WORKLIST's count first: `/discover` answered it when the universe was built, and it is the number
    the query selected on. Only an `export` row has none — the daily dump states popularity — and those
    fall back to the detail call's count while that call is still made (oxyc/den-dataset#53).

    None where nothing states one, and the caller compares nothing rather than a number: a title neither
    names a count for is judged on IMDb's count alone, which is the half of the union that exists for the
    titles TMDB undercounts. The worklist is where the distinction is load-bearing — a row written with a
    zero would refuse a title the detail call admits (`pipeline/worklist.entry`).
    """
    if label in worklist_votes:
        return worklist_votes[label]
    return record.get("voteCount")


def identities(records, client, cache):
    """`(key -> resolution, media -> items to leave out)`: which ONE Wikidata item answers for each title
    (`lib/wikidata.resolve`), and the claimants every Wikidata query of this batch must set aside.

    Two items can state one TMDB id, and every query here is keyed by that id, so without this a title's
    fields came from both: series 2559 shipped Boon's IMDb id under Bonn's name. TMDB's own IMDb id and year
    are what choose, read from the detail call this batch has just made — a series asks it once more with
    `external_ids`, since its record names no IMDb id otherwise — and only for the contested titles.
    """
    found, excluded = {}, {}
    for media in sorted({record["mediaType"] for record in records}):
        ids = [record["tmdbId"] for record in records if record["mediaType"] == media]
        resolved = wikidata.resolve(ids, media, cache,
                                    lambda tmdb_id, media=media: tmdb_api.title_identity(client, media, tmdb_id))
        found.update({key(media, tmdb_id): value for tmdb_id, value in resolved.items()})
        excluded[media] = wikidata.set_aside(resolved)
    return found, excluded


def admit(records, floors, ratings, cache, worklist_votes=None, excluded=None):
    """`(admitted, unidentified, from_worklist)`: key → which count admitted it (`TMDB`, `IMDB` or `BOTH`),
    how many of the titles TMDB left short have no IMDb id and were therefore judged on TMDB's count alone,
    and how many titles were judged on a count their worklist row carried.

    A key absent from `admitted` is below every floor its tiers set. With `ratings` None the IMDb half is
    off and this is TMDB's floor alone. Raises what the IMDb-id lookup raises — the caller aborts on it, as
    it does on the mapping, which asks the same service.
    """
    worklist_votes, excluded = worklist_votes or {}, excluded or {}
    admitted, unidentified, from_worklist = {}, 0, 0
    ids = {}
    if ratings is not None:
        for media in sorted({record["mediaType"] for record in records}):
            found = wikidata.imdb_ids([r["tmdbId"] for r in records if r["mediaType"] == media], media, cache,
                                      excluded=excluded.get(media))
            ids.update({key(media, tmdb_id): imdb_id for tmdb_id, imdb_id in found.items()})
    for record in records:
        label = key(record["mediaType"], record["tmdbId"])
        tmdb_floor, imdb_floor = floors.of(record)
        votes = tmdb_votes(label, record, worklist_votes)
        from_worklist += label in worklist_votes
        by_tmdb = votes is not None and votes >= tmdb_floor
        imdb_id = ids.get(label)
        by_imdb = imdb_id is not None and ratings.get(imdb_id) >= imdb_floor
        if by_tmdb or by_imdb:
            admitted[label] = BOTH if by_tmdb and by_imdb else TMDB if by_tmdb else IMDB
        if not by_tmdb and ratings is not None and imdb_id is None:
            unidentified += 1
    return admitted, unidentified, from_worklist


def run(worklist_path, out_dir, floors=floor_rules.DEFAULT, limit=LIMIT, exclude_anime=False, client=None,
        cache=None, token=None, today=None):
    """One batch. Returns the report the drain reads by key.

    `client` is a `lib/tmdb.TMDB`, built from the environment when not given — and only once there is
    something to fetch, so a drained worklist needs no key. `cache` is the `wiki` cache, from the
    environment when not given; `token` the Enterprise bearer, None for the free action API. `today` is
    the UTC date a below-floor verdict is recorded under (see `standing`), today's when not given.

    A batch that admits no title writes no batch file and takes no batch number: an empty `batch-N.json`
    holds nothing any reader wants, and each one moved the numbering on.

    Raises `Aborted` for a batch that wrote nothing and should simply run again, and `StageError` for one
    that running again cannot fix.

    A worklist may hold BOTH media. The Swift command refused one that did, citing vote files that carried
    no media type and readers that keyed a row by a bare id — `loadVotePasses`, the escalation and
    `assemble`. Those readers are deleted, and every set, map and query here is keyed by the pair. So is
    every reader of a batch today — the `articles`, `embed` and `genres-moods` stages, `run_combined.py`'s
    evidence fallback, the census (`scripts/check-plot-invariants.py`), the provenance backfill and
    `scripts/v2/premise_mine_candidates.py` all key by `mediaType:tmdbId` — and a mixed batch is safe only
    while that holds: a reader keyed by a bare id would read series 95 as movie 95.
    """
    if limit < 1:
        raise StageError(f"enrich: --limit {limit} takes nothing, and would report the worklist as drained")
    if token:
        # Read now, so a malformed reserve refuses the run instead of being swallowed as a failed fast path.
        enterprise.gate.headroom()
    worklist, worklist_votes = read_worklist(worklist_path)
    ck_path = checkpoint_path(out_dir)
    present = os.path.exists(ck_path)
    checkpoint = read_checkpoint(ck_path)
    # An ABSENT checkpoint is not proof of a first run. `out-t02` had 153 batches and none, so a delta into
    # it numbered from 1 and overwrote batch-1 and batch-2 — 640 records replaced by 235. The directory is
    # the floor: whatever is on disk has already been written.
    on_disk = max((number for number, _name in batches(os.path.join(out_dir, "enriched"))), default=0)
    batch_id = max(checkpoint["nextBatch"], on_disk + 1)
    processed = checkpoint["processed"]
    if present:
        processed |= unrecorded(out_dir, checkpoint["nextBatch"])
    today = today or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    judged = checkpoint["judgedBelow"]
    held = standing(judged, today, floors, worklist_votes)
    settled = processed | held
    pending = [entry for entry in worklist if key(*entry) not in settled][:limit]
    if not pending:
        return {"remaining": 0, "count": 0}

    client = client or tmdb_api.TMDB()
    cache = wikipedia.cache_for() if cache is None else cache
    counts = dict.fromkeys(TOTALS, 0)
    records, titles, deferred, below = [], [], set(), set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=TMDB_WORKERS) as pool:
        verdicts = pool.map(lambda entry: fetch_title(client, *entry), pending)
        for (media, tmdb_id), (verdict, found) in zip(pending, verdicts):
            label = key(media, tmdb_id)
            if verdict == "deferred":
                # Transient: NOT checkpointed, so a blip cannot drop a title for good.
                deferred.add(label)
                log(out_dir, f"fetch-deferred id={label} (transient: {found})")
            elif verdict == "failure":
                counts["failures"] += 1
                log(out_dir, f"fetch-failure id={label} {found}")
            else:
                records.append(found)

    ratings, gate = imdb_counts(floors) if records else (None, None)
    if records and ratings is None:
        log(out_dir, f"imdb-gate {gate} — batch {batch_id} admits on TMDB's count alone")
        print(f"  warning: the IMDb half of the admission gate is {gate}. This batch admits on TMDB's count "
              f"alone; a title only IMDb would admit stays pending for the next batch.", file=sys.stderr)
    try:
        resolved, excluded = identities(records, client, cache)
    except (http.HTTPError, wikidata.WikidataError) as error:
        raise Aborted(f"choosing each title's Wikidata item failed for batch {batch_id} after retries ({error}); "
                      f"nothing written — re-run to retry this batch") from error
    try:
        admitted, unidentified, from_worklist = admit(records, floors, ratings, cache, worklist_votes, excluded)
    except (http.HTTPError, wikidata.WikidataError) as error:
        raise Aborted(f"Wikidata IMDb-id lookup failed for batch {batch_id} after retries ({error}); nothing "
                      f"written — re-run to retry this batch") from error
    # What each admitted title IS, for the anime rule — and ONLY when a run asked to exclude anime, since
    # the flag is opt-in and this is a whole extra query per media otherwise.
    kinds = {}
    if exclude_anime:
        admitted_records = [r for r in records if key(r["mediaType"], r["tmdbId"]) in admitted]
        try:
            for media in sorted({record["mediaType"] for record in admitted_records}):
                ids = [r["tmdbId"] for r in admitted_records if r["mediaType"] == media]
                for tmdb_id, labels in wikidata.kinds(ids, media, cache, excluded=excluded.get(media)).items():
                    kinds[key(media, tmdb_id)] = labels
        except (http.HTTPError, wikidata.WikidataError) as error:
            raise Aborted(f"Wikidata genre/type lookup failed for batch {batch_id} after retries ({error}); "
                          f"nothing written — re-run to retry this batch") from error
    for found in records:
        label = key(found["mediaType"], found["tmdbId"])
        if label not in admitted:
            # Not processed: recorded as a verdict about today's counts, which a later day re-judges — see
            # `standing`. The cache makes the re-judging nearly free.
            counts["belowFloor"] += 1
            below.add(label)
            judged[label] = below_floor(tmdb_votes(label, found, worklist_votes), floors, today,
                                        imdb_on=ratings is not None)
        elif exclude_anime and is_anime(kinds.get(label, ())):
            # Opt-IN: excluding anime by default silently cost the corpus 1,498 titles, the entire
            # Ghibli catalogue among them.
            counts["anime"] += 1
        else:
            # No stub check here any more. It dropped a title whose TMDB overview was under 20 characters,
            # judged on a length `lib/tmdb.title_record` carried across the boundary for that one reader.
            # `overview` holds a Wikipedia plot or nothing, so the length said nothing about what this
            # title would be grounded on, and every admitted title is grounded on Wikipedia or written
            # plotless regardless: over the whole repass it refused 3 titles out of 59,209.
            titles.append(dict(found, **wikidata.provenance(resolved.get(label))))

    # ONE mapping query per media type, keyed by both — see `lib/wikidata.mapping` — and one language query
    # beside it. P364 is its own request rather than another OPTIONAL on the mapping, whose text is the
    # cache key for ~770 bodies already on disk. So is the source-work query, asked only about the titles
    # the mapping names a source work for.
    facts = {}
    try:
        for media in sorted({record["mediaType"] for record in titles}):
            ids = [record["tmdbId"] for record in titles if record["mediaType"] == media]
            spoken = wikidata.languages(ids, media, cache, excluded=excluded.get(media))
            mapped = wikidata.mapping(ids, media, plot.HEADINGS_BY_LANGUAGE, cache, excluded=excluded.get(media))
            works = wikidata.sources(sorted(i for i, found in mapped.items() if found.get("sourceArticle")),
                                     media, cache, excluded=excluded.get(media))
            for tmdb_id, found in mapped.items():
                facts[key(media, tmdb_id)] = dict(found, languages=spoken.get(tmdb_id, []),
                                                  sourceWorks=works.get(tmdb_id, {}))
    except (http.HTTPError, wikidata.WikidataError) as error:
        raise Aborted(f"Wikidata mapping failed for batch {batch_id} after retries ({error}); nothing "
                      f"written — re-run to retry this batch") from error

    survivors, with_plot, from_enterprise = [], 0, 0
    requests_before = enterprise.gate.sent_this_run
    with concurrent.futures.ThreadPoolExecutor(max_workers=WIKI_WORKERS) as pool:
        outcomes = pool.map(lambda r: reground(r, facts.get(key(r["mediaType"], r["tmdbId"])), cache, token),
                            titles)
        for record, (verdict, found, detail) in zip(titles, outcomes):
            label = key(record["mediaType"], record["tmdbId"])
            if verdict == "deferred":
                deferred.add(label)
                log(out_dir, f"plot-deferred id={label} (transient: {detail})")
                continue
            if verdict == "missed":
                log(out_dir, f"plot-miss id={label} ({detail})")
            with_plot += verdict == "grounded"
            from_enterprise += verdict == "grounded" and detail == plot.ENTERPRISE
            survivors.append(written(found))
    survivors.sort(key=lambda row: row["tmdbId"])

    path = batch_path(out_dir, batch_id)
    if survivors:
        # Never write over an existing batch: whatever was derived from it belongs to the titles it USED to
        # hold.
        if os.path.exists(path):
            raise StageError(f"refusing to overwrite {path}: it already holds an enriched batch, and anything "
                             f"derived from batch {batch_id} belongs to those titles. The enrich checkpoint's "
                             f"nextBatch is out of step with the batches on disk — fix it rather than "
                             f"clobbering.")
        again = recovered(out_dir, batch_id, survivors)
        if again:
            sample = ", ".join(again[:5]) + (" …" if len(again) > 5 else "")
            log(out_dir, f"re-covered {len(again)} key(s) already in earlier batches: {sample}")
            print(f"  warning: {len(again)} key(s) here already exist in earlier batches — the newest wins on "
                  f"read, but the older records remain. {sample}", file=sys.stderr)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not present:
            # With no checkpoint there is nothing for `unrecorded` to measure a batch against, so the number
            # is reserved first: a death between the batch and the checkpoint below then leaves a checkpoint
            # that says this batch was never recorded.
            reserved = {"nextBatch": batch_id, "processed": sorted(checkpoint["processed"]), "totals": {}}
            caching.write_atomically(ck_path, compact(reserved).encode("utf-8"))
        caching.write_atomically(path, swift_json(survivors).encode("utf-8"))

    # Every pending id EXCEPT those still owed another look: transient failures, and below-floor titles,
    # which are recorded as today's verdicts instead.
    processed.update(key(*entry) for entry in pending if key(*entry) not in deferred | below)
    # Only today's verdicts are kept: an older one no longer holds (`standing`), so dropping it re-asks
    # nothing that would not be re-asked anyway, and the map stays one day's size.
    judged = {label: kept for label, kept in judged.items()
              if kept.get("on") == today and label not in processed}
    totals = {name: checkpoint["totals"].get(name, 0) + counts[name] for name in TOTALS}
    state = {"nextBatch": batch_id + 1 if survivors else batch_id, "processed": sorted(processed),
             "judgedBelow": judged, "totals": totals}
    caching.write_atomically(ck_path, compact(state).encode("utf-8"))

    # Which source SERVED each plot, not which was asked: a bearer can be throttled or expire mid-run, and the
    # two record different things (the Enterprise path names no revision and cannot see a redirect).
    settled = processed | held | below
    report = {"count": len(survivors), "belowFloor": counts["belowFloor"],
              "anime": counts["anime"], "failures": counts["failures"],
              "deferred": len(deferred), "remaining": sum(key(*e) not in settled for e in worklist),
              "wikiPlot": with_plot, "tagsOnly": len(survivors) - with_plot,
              "plotsFromEnterprise": from_enterprise, "plotsFromActionApi": with_plot - from_enterprise,
              "enterpriseRequests": enterprise.gate.sent_this_run - requests_before,
              # Which count admitted each title that cleared the gate. Counts of titles, never a vote count:
              # IMDb's numbers stay in this process. `imdbGate` says how fresh the dump was, or why the IMDb
              # half was off; `shortOfImdbId` are titles TMDB left short that Wikidata names no IMDb id
              # for, so TMDB's count alone decided them.
              "admittedByTmdb": sum(v == TMDB for v in admitted.values()),
              "admittedByImdb": sum(v == IMDB for v in admitted.values()),
              "admittedByBoth": sum(v == BOTH for v in admitted.values()),
              "shortOfImdbId": unidentified, "imdbGate": gate,
              # How many titles were judged on the count their worklist row carried rather than on the
              # detail call's. It is the whole batch for a discover or delta universe, and none of it for
              # an export one — so this is what says how much of the gate still depends on that call.
              "votesFromWorklist": from_worklist}
    if survivors:
        report.update(batchId=batch_id, batch=path)
    if token and enterprise.gate.off:
        report["enterpriseOff"] = enterprise.gate.off
    return report


def parser():
    parser = argparse.ArgumentParser(prog="python3 -m pipeline.enrich", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--worklist", required=True, help="the worklist JSON to draw ids from")
    parser.add_argument("--out-dir", required=True, help="the enriched batches and the resumable checkpoint")
    parser.add_argument("--vote-floor", type=int,
                        help=f"the worldwide tier's TMDB vote floor (default {floor_rules.DEFAULT.tmdb}); a "
                             f"title below every floor it is judged by stays pending")
    parser.add_argument("--regional-vote-floor", type=int,
                        help=f"the regional tier's TMDB vote floor (default {floor_rules.DEFAULT.regional_tmdb})")
    parser.add_argument("--imdb-floor", type=int,
                        help=f"the worldwide tier's IMDb vote floor (default {floor_rules.DEFAULT.imdb})")
    parser.add_argument("--regional-imdb-floor", type=int,
                        help=f"the regional tier's IMDb vote floor (default {floor_rules.DEFAULT.regional_imdb})")
    parser.add_argument("--limit", type=int, default=LIMIT, help="un-enriched ids to take (default 150)")
    parser.add_argument("--exclude-anime", action="store_true",
                        help="drop anime. Opt-IN: excluding it by default silently cost the corpus 1,498 titles")
    return parser


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        floors = floor_rules.given(args.vote_floor, args.regional_vote_floor, args.imdb_floor,
                                   args.regional_imdb_floor)
        report = run(args.worklist, args.out_dir, floors, args.limit, args.exclude_anime,
                     token=os.environ.get("WIKIMEDIA_ENTERPRISE_TOKEN") or None)
    except (StageError, Aborted, tmdb_api.TMDBError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(compact(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
