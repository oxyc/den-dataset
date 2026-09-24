#!/usr/bin/env python3
"""The weekly REFRESH: re-fetch only the grounded titles whose Wikipedia article changed.

    ./den stage fetch --refresh [--plan] --out-dir DIR

A full re-enrichment is ~7h of network, and measured over ten weeks almost all of it discovers that
nothing changed (oxyc/den-dataset#5). Every grounded title records the article it was read from
(`plotArticle`, `plotLanguage`) and that article's revision (`plotRevId`), so the question "which of these
changed" is one batched metadata request per 50 articles (`lib/wikipedia.revisions`), and only the answers
that moved cost a fetch.

**What counts as changed.** The current revision differs from the recorded one, the page is gone, or the
recorded revision is unknown. Unknown is ~17k titles on out-repass, the ones the Enterprise path served:
it names no revision. Before counting one as changed, the body the action API left in the response cache
is asked (`lib/plot.cached_plot`): a body whose plot is EXACTLY the record's text is the revision that text
came from, and that revision is recorded instead of a re-fetch. Anything short of an exact match is unknown.

**A changed title is enriched again, not patched.** `enrich.reground` runs over its candidates — the English
article, its other-language sitelinks, the source work — as it did the first time, with every Wikipedia
read going to the network (`Fresh`): the cache would answer with the revision that was just found stale.
What comes back replaces the record in a NEW batch, which every reader already resolves newest-first.
Titles the refresh only recorded a revision for go into the same batches, so the next refresh has it.

**What it hands downstream**, per run under `refresh/<UTC stamp>/`:

  * `changed.txt`  — the titles whose plot text is different now. `./den stage embed --reembed-keys` takes
    it as it is; the classify pass takes it through a supersede run (`pipeline/consolidate_corpus.py`:
    the run that started later wins, by key). Which of them to re-classify is a cosine decision #5 leaves
    to the embedding, not to this list.
  * `plotless.txt` — titles that had a plot and have none now. Their classify and critique rows were read
    from an article they no longer have and no new run will answer them, so they are withdrawn:
    `pipeline/consolidate_corpus.py withdraw --keys …/plotless.txt`.
  * `report.json`  — the counts.

A title whose revision moved and whose plot did not (an edit to its Reception section) is in neither
list: its record takes the new revision and nothing downstream has anything to redo.

Keys are appended to the lists BEFORE the batch they describe is written, so a kill can list a title whose
batch never landed — re-embedding it then finds the same document and does nothing — but never land a
batch whose change no list names.
"""
import concurrent.futures
import datetime
import json
import os
import sys

from lib import cache as caching, http, plot, wikidata, wikipedia

from . import artifacts, enrich
from .contract import StageError

#: Titles per batch the refresh writes: the fetch stage's batch size, for the same reasons.
BATCH = 500

#: What a refreshed record keeps from the one it replaces: its key, its `animated` flag and the Wikidata
#: item it was resolved to. Everything else is `reground`'s to write again — and a field left over from the
#: old grounding (a `plotArticle` on a title that lost its plot) would be a lie about the new one. Older
#: batches carry TMDB fields, `genreIDs` among them (oxyc/den-dataset#53); they are not copied, and a record
#: only given its backfilled revision sheds them through `enrich.written` too.
KEPT = ("tmdbId", "mediaType", "animated", "wikidataItem", "wikidataCandidates")

UNCHANGED, MOVED, GONE, UNKNOWN = "unchanged", "moved", "gone", "unknown"


class Fresh:
    """A response cache with its reads switched off: every request is asked again, and its answer replaces
    the body on disk, so a later reader of the same request sees the new answer too. The refresh re-reads
    articles through it; the facts and doc-facts stages re-ask the change set's titles through it, since an
    answer cached under the same query text is exactly the one being checked."""

    def __init__(self, cache):
        self.cache = cache

    def key(self, path, query=None):
        return self.cache.key(path, query)

    def read(self, _key):
        return None

    def write(self, key, body):
        self.cache.write(key, body)


def latest(enriched_dir):
    """`key -> record`, the newest batch's record for each title — the rule every batch reader applies."""
    out = {}
    for _number, name in enrich.batches(enriched_dir):
        with open(os.path.join(enriched_dir, name), encoding="utf-8") as handle:
            for record in json.load(handle):
                out[enrich.key(record["mediaType"], record["tmdbId"])] = record
    return out


def recorded(record, cache):
    """`(revision, backfilled)`: the revision this record's plot was read from, or None when nothing says.

    `plotRevId` when the enrichment recorded one. Otherwise the cached action-API body for the same article,
    but only when its plot is byte-for-byte the record's: the Enterprise text and the wikitext split differ
    in their cleaning, and a body that merely resembles the record proves nothing about where it came from.
    """
    revision = record.get("plotRevId")
    if isinstance(revision, int) and not isinstance(revision, bool):
        return revision, False
    article, language = record["plotArticle"], record.get("plotLanguage") or "en"
    found = plot.cached_plot(article, language, cache)
    if (found is not None and found["revId"] is not None and found["resolvedArticle"] == article
            and found["text"] == record.get("overview")):
        return found["revId"], True
    return None, False


def survey(records, cache, ask=wikipedia.revisions):
    """`{key: (verdict, record, revision)}` for every grounded title, and the requests it took.

    One request per `lib/wikipedia.REVISION_BATCH` articles of one Wikipedia, asked in language order. The
    revision is the one to record: the backfilled one for an unchanged title, the stored one otherwise.
    """
    grounded = {label: record for label, record in records.items()
                if record.get("hasWikiPlot") and record.get("plotArticle")}
    by_language = {}
    for record in grounded.values():
        by_language.setdefault(record.get("plotLanguage") or "en", set()).add(record["plotArticle"])
    current, requests = {}, 0
    for language in sorted(by_language):
        titles = sorted(by_language[language])
        requests += -(-len(titles) // wikipedia.REVISION_BATCH)
        for title, revision in ask(titles, language).items():
            current[(language, title)] = revision
    out = {}
    for label, record in sorted(grounded.items()):
        revision, backfilled = recorded(record, cache)
        now = current.get((record.get("plotLanguage") or "en", record["plotArticle"]))
        if revision is None:
            verdict = UNKNOWN
        elif now is None:
            verdict = GONE
        elif now != revision:
            verdict = MOVED
        else:
            verdict = UNCHANGED
        out[label] = (verdict, record, revision if backfilled and verdict == UNCHANGED else None)
    return out, requests


def counts(surveyed, requests):
    tally = {verdict: 0 for verdict in (UNCHANGED, MOVED, GONE, UNKNOWN)}
    for verdict, _record, _backfill in surveyed.values():
        tally[verdict] += 1
    return dict(tally, grounded=len(surveyed), revisionRequests=requests,
                toFetch=tally[MOVED] + tally[GONE] + tally[UNKNOWN],
                toRecord=sum(backfill is not None for _v, _r, backfill in surveyed.values()))


def base(record):
    return {name: record[name] for name in KEPT if name in record}


def excluded_items(records):
    """`media -> {tmdbId: [Q-id]}`: the claimants a record's own provenance says were set aside — what
    `lib/wikidata.set_aside` gave the enrichment, read back off the row as `pipeline/articles` does."""
    out = {}
    for record in records:
        others = [q for q in record.get("wikidataCandidates") or () if q != record.get("wikidataItem")]
        if others:
            out.setdefault(record["mediaType"], {})[record["tmdbId"]] = others
    return out


def refetch(records, cache):
    """`[(key, outcome, record)]` for one chunk: `changed`, `revised`, `plotless`, or `deferred` — a
    transient failure or a definitive one, neither of which replaces a record that had a plot.

    Raises `enrich.Aborted` when Wikidata cannot say what the candidates are: nothing of the chunk is
    written, and the next refresh finds the same titles changed.
    """
    titles = [base(record) for record in records]
    try:
        facts = enrich.candidates(titles, cache, excluded_items(titles))
    except (http.HTTPError, wikidata.WikidataError) as error:
        raise enrich.Aborted(f"refresh: the Wikidata candidate lookup failed ({error}); nothing of this "
                             f"chunk was written — the next refresh finds it again") from error
    fresh = Fresh(cache) if cache is not None else None
    out = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=enrich.WIKI_WORKERS) as pool:
        outcomes = pool.map(lambda r: enrich.reground(r, facts.get(enrich.key(r["mediaType"], r["tmdbId"])),
                                                      fresh, None), titles)
        for old, (verdict, found, _detail) in zip(records, outcomes):
            label = enrich.key(old["mediaType"], old["tmdbId"])
            if verdict in ("deferred", "missed"):
                out.append((label, "deferred", None))
            elif verdict == "noPlot":
                out.append((label, "plotless", enrich.written(found)))
            else:
                same = found["overview"] == old.get("overview")
                out.append((label, "revised" if same else "changed", enrich.written(found)))
    return out


def write_batch(out_dir, rows):
    """One new batch after every batch on disk, and the checkpoint's next number moved past it. Returns its
    path. The number is `enrich.run`'s rule — the directory is the floor — so a drain after this does not
    reuse it, and an existing batch is never written over."""
    directory = os.path.join(out_dir, "enriched")
    on_disk = max((number for number, _name in enrich.batches(directory)), default=0)
    checkpoint = enrich.checkpoint_path(out_dir)
    state = None
    if os.path.exists(checkpoint):
        enrich.read_checkpoint(checkpoint)          # refuses an unreadable one before anything is written
        with open(checkpoint, encoding="utf-8") as handle:
            state = json.load(handle)
    number = max(int((state or {}).get("nextBatch", 1)), on_disk + 1)
    path = enrich.batch_path(out_dir, number)
    if os.path.exists(path):
        raise StageError(f"refresh: refusing to overwrite {path}")
    os.makedirs(directory, exist_ok=True)
    caching.write_atomically(path, enrich.swift_json(sorted(rows, key=lambda r: r["tmdbId"])).encode("utf-8"))
    if state is not None:
        state["nextBatch"] = number + 1
        caching.write_atomically(checkpoint, enrich.compact(state).encode("utf-8"))
    return path


def append(path, keys):
    with open(path, "a", encoding="utf-8") as handle:
        handle.writelines(f"{key}\n" for key in keys)


def stamp(now=None):
    return (now or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y%m%dT%H%M%SZ")


def run(ctx, cache=None, ask=wikipedia.revisions, now=None):
    """Survey, and unless `ctx.plan`, re-fetch what changed. Returns the report."""
    enriched = ctx.require(artifacts.ENRICHED)
    cache = wikipedia.cache_for() if cache is None else cache
    try:
        surveyed, requests = survey(latest(enriched), cache, ask)
    except (http.HTTPError, ValueError) as error:
        raise StageError(f"refresh: asking Wikipedia for the current revisions failed ({error}). Nothing "
                         f"was fetched or written; run it again.") from None
    report = counts(surveyed, requests)
    if ctx.plan:
        print(json.dumps(dict(report, plan=True), sort_keys=True), flush=True)
        return report

    run_dir = os.path.join(ctx.path(artifacts.REFRESH), stamp(now))
    os.makedirs(run_dir, exist_ok=False)
    lists = {name: os.path.join(run_dir, f"{name}.txt") for name in ("changed", "plotless")}
    for path in lists.values():
        open(path, "w", encoding="utf-8").close()
    report.update(changed=0, revised=0, plotless=0, deferred=0, recorded=0, batches=[])

    stale = [record for verdict, record, _b in surveyed.values() if verdict != UNCHANGED]
    for start in range(0, len(stale), BATCH):
        rows, listed = [], {"changed": [], "plotless": []}
        try:
            outcomes = refetch(stale[start:start + BATCH], cache)
        except enrich.Aborted as error:
            raise StageError(f"{error}. The {len(report['batches'])} batch(es) before it are written and "
                             f"listed in {run_dir}.") from None
        for label, outcome, record in outcomes:
            report[outcome] += 1
            if outcome == "deferred":
                continue
            rows.append(record)
            if outcome in listed:
                listed[outcome].append(label)
        for name, keys in listed.items():
            append(lists[name], keys)
        if rows:
            report["batches"].append(os.path.basename(write_batch(ctx.out_dir, rows)))
        print(f"  refresh: {min(start + BATCH, len(stale))} of {len(stale)} re-fetched", file=sys.stderr)

    backfilled = [enrich.written(dict(record, plotRevId=revision))
                  for _v, record, revision in surveyed.values() if revision is not None]
    for start in range(0, len(backfilled), BATCH):
        report["batches"].append(os.path.basename(write_batch(ctx.out_dir, backfilled[start:start + BATCH])))
    report["recorded"] = len(backfilled)
    report["run"] = run_dir
    caching.write_atomically(os.path.join(run_dir, "report.json"),
                             (json.dumps(report, indent=1, sort_keys=True) + "\n").encode("utf-8"))
    print(json.dumps(report, sort_keys=True), flush=True)
    return report
