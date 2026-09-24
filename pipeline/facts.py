#!/usr/bin/env python3
"""FACTS — the CC0 facts den-atlas /recommend ranks on: both Wikidata scrapes, and the merge that ships.

**One stage owns the whole file, scrape and merge.** The scrape used to be a Swift command typed twice
and the merge a stage, and between them sat a hand step: both passes and the merge all write
`facts-<version>.json`, so the corpus pass had to be moved aside before the delta pass overwrote it. A
merged file missing a pass is not a smaller merge, it is the published facts minus every title only that
pass covers — which is how a rebuild once dropped the 137 delta records, noticed only as /recommend quietly
losing library titles. Here each pass is written straight to the name the merge reads, so there is no
step to forget, and a scrape re-run after a finished one costs nothing: every id is in its checkpoint.

**The scrape runs twice and cannot run once.** The corpus pass covers the ids in `labels-t02.json` and is
stamped `hasVector`; the delta pass covers the ids in `facts-delta-ids.txt` — titles with no vector, no
labels and no facets row — and is not. /recommend must never let a vectorless record into an ANN path, so
`hasVector` is per record and one pass cannot state both. Each pass keeps its own checkpoint: sharing one
would write every corpus record into the delta file, stamped vectorless. The corpus pass keeps the Swift's
(`facts-fields.json`, `facts-entities.json`, `facts-source-types.json` in the out-dir), so an out-dir it
scraped resumes, beside `facts-source-authors.json`; the delta pass keeps its four under `facts-delta/`.

**Resumable, and a batch that fails does not end the pass.** A corpus pass is thousands of WDQS requests
over hours, and one of them WILL time out. A failed batch is dropped whole — a row holding the properties
that landed before the timeout would read as finished to the resume and ship short by the rest — and the
pass goes on. The stage then REFUSES rather than merging: a pass that skipped batches is not a finished
scrape, and publishing it is the short-merge failure again. Re-running sweeps the skipped ids up.

**The version is the manifest's, not the operator's.** Every file this stage writes is named by the dataset
version, and that version is the one `finalize` derived from the labels and vectors these facts describe —
`dataset.meta.json`'s `datasetVersion`, which the Swift read too. Taken from a flag it could be anything: the
shipped `facts-5b1c3213b6a1.json` says `c85c707b0b18` inside, a file renamed by hand to the generation it
was not built for. So `--dataset-version` may be left off, and one that disagrees with the manifest is
refused rather than obeyed: the corpus join and the store look the facts up by the flag, and a facts file
under the manifest's name would be missing to them. It is passed to the merge too, so the name on the file
and the version inside it are one value.
"""
import dataclasses
import json
import os
import re
import subprocess
import sys
import time

from . import artifacts, finalize, jsonbytes
from .contract import REPO, StageError, bind
from lib import cache as caching
from lib import http, wikidata
from lib import wikidata_facts as wd

NAME = "facts"

#: The scrape is this module; the merge it ends with is `SCRIPT`, which `facts_test.py` holds this stage's
#: merged file to byte for byte.
PRODUCER = "pipeline/facts.py"
HOW = "./den stage facts --out-dir <dir>"
#: Writes into the out-dir and nowhere else.
PUBLISHES = False
#: Wikidata's public query service, unbilled.
SPENDS = False
SCRIPT = os.path.join(REPO, "pipeline", "merge_facts.py")

#: Ids per SPARQL request. 25 rather than the Swift's default 100: a 100-id batch stalled from this
#: client until the 60 s timeout while the identical query answered in ~1 s elsewhere, and the run never
#: cleared its first batch on resume. `pipeline/facts-run.sh` passed 25 for that reason, so the cached
#: bodies on disk are keyed on 25-id batches — the batch is part of the query text, and the query text is
#: the cache key.
BATCH = 25

#: Seconds between LIVE property requests. Firing them back to back sustains ~3 requests a second for
#: hours, which WDQS throttles into intermittent 429s that a restart loop then retries forever. A cached
#: answer asks nothing, so it waits for nothing.
PACE = 0.3

#: `labels-t02.json` is `--labels` here: the plot vectors' titles, which `finalize` wrote just before this
#: stage. The manifest is read for the dataset version, which names every file this stage writes.
INPUTS = (artifacts.VECTOR_LABELS.called("labels"), artifacts.DELTA_IDS, artifacts.MANIFEST)
#: The two passes, in the order the merge takes them — which is not cosmetic: the FIRST file wins a
#: collision, and swapped, every overlapping title would publish as vectorless.
OUTPUTS = (artifacts.CORPUS_FACTS, artifacts.DELTA_FACTS, artifacts.FACTS)

BOUND = {bind(entry).name: bind(entry) for entry in INPUTS}

#: Where each pass keeps its four checkpoints, relative to the out-dir.
CHECKPOINTS = {True: "", False: "facts-delta"}

#: Properties added to `wd.SPECS` after checkpoints were already on disk. A row scraped before them lacks
#: the key, and so does a row that was asked and has no value, so for these alone a row that was asked
#: records `[]`: `backfill_properties` asks every checkpointed row with no entry, and the record as it
#: ships drops the empty list.
BACKFILLED = ("awardsWon", "awardsNominated")
#: Ids per backfill request. Far more than `BATCH`: the backfill asks one property of every checkpointed
#: row, and a WDQS request's cost is the request, not its size — measured on the corpus while WDQS was
#: throttling this client, 25 ids took 22 s and 500 took 5.4 s. At 25 the 47,618-title corpus was ~15 s a
#: batch, 3,800 batches.
BACKFILL_BATCH = 500

#: The record fields that credit a person, whose traits (`wd.people`) are asked. A company credited as a
#: series' creator is asked too, and Wikidata states none of them for it.
PERSON_FIELDS = ("cast", "directors", "creators", "screenwriters", "composers", "cinematographers")
#: The traits that name an item — a gender, a country, an occupation — which ships by name.
TRAIT_ITEMS = tuple(wd.PERSON_ITEMS.values())
#: Where a person was born (`wd.birthplaces`): the place and its country, items too, named like the traits.
BIRTH_ITEMS = ("birthplace", "birthcountry")
#: The traits whose items are countries, each asked for its ISO code (`wd.country_codes`).
COUNTRY_ITEMS = ("citizenship", "birthcountry")

_ID = re.compile(r"[+-]?\d+")


def say(message):
    print(f"  {message}", file=sys.stderr)


def keys_from_labels(path):
    with open(path, encoding="utf-8") as handle:
        records = json.load(handle).get("records")
    if not records:
        raise StageError(f"facts: {path} names no records, so there is no corpus to scrape facts for.")
    return [f"{record['mediaType']}:{record['tmdbId']}" for record in records]


def keys_from_ids(path):
    """`movie:1,tv:2`, or one per line — whatever separates them."""
    with open(path, encoding="utf-8") as handle:
        return [token for token in re.split(r"[,\s]+", handle.read()) if token]


def by_type(keys):
    """`{media: [tmdbId]}` in the keys' own order. A malformed key is skipped, as it always was."""
    out = {}
    for key in keys:
        parts = [part for part in key.split(":") if part]
        if len(parts) == 2 and _ID.fullmatch(parts[1]):
            out.setdefault(parts[0], []).append(int(parts[1]))
    return out


def checkpoint(path):
    """A checkpoint, or `{}` when there is none yet. One that will not parse is a refusal: starting over
    is the whole scrape again, and it would overwrite what IS there."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            found = json.load(handle)
    except ValueError as broken:
        raise StageError(f"facts: {path} exists and does not parse ({broken}). Refusing to start the scrape "
                         f"over — restore it, or delete it deliberately.") from None
    if not isinstance(found, dict):
        raise StageError(f"facts: {path} is not the object a scrape checkpoint is.")
    return found


def save(path, value):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    caching.write_atomically(path, jsonbytes.compact(value).encode("utf-8"))


def title_strings(found):
    """The titles hop as a record carries it: the article title without its disambiguator (else the
    label), the original title (else the label), and every alias, sorted."""
    out = {}
    english = found["article"] if found["article"] is not None else found["label"]
    if english is not None:
        out["en"] = wd.stripped_article_suffix(english)
    original = found["original"] if found["original"] is not None else found["label"]
    if original is not None:
        out["orig"] = original
    if found["aliases"]:
        out["aliases"] = sorted(found["aliases"])
    return out


def identities(types, cache):
    """`(key -> resolution, kind -> items to leave out)` for every title of a pass: which ONE Wikidata item
    answers for it (`lib/wikidata.resolve`), chosen as the enrichment chooses, so a title's facts and its
    plot name the same work.

    Asked for the whole pass before the first batch, and refused rather than skipped when it fails: every
    batch after it would otherwise merge two works again."""
    found, excluded = {}, {}
    try:
        for kind, ids in types.items():
            media = "tv" if kind == "tv" else "movie"
            resolved = wikidata.resolve(ids, media, cache)
            found.update({f"{kind}:{tmdb_id}": value for tmdb_id, value in resolved.items()})
            excluded[kind] = wikidata.set_aside(resolved)
    except wikidata.DecisionError as stale:
        raise StageError(f"facts: {stale}") from None
    except (wikidata.WikidataError, http.HTTPError) as failure:
        raise StageError(f"facts: choosing each title's Wikidata item failed ({failure}). Nothing new was "
                         f"scraped; re-run.") from None
    return found, excluded


def answered():
    """How many live requests each SPARQL endpoint has answered this run, e.g. `wdqs=3 qlever=120`."""
    return " ".join(f"{name}={count}" for name, count in sorted(wd.ANSWERED.items())) or "none yet"


def scrape_batches(fields, types, cache, pace, checkpointed, resolved=None, excluded=None):
    """Fill `fields` batch by batch, calling `checkpointed` after each. Returns how many ids it skipped.

    A contested title is asked about its chosen item alone (`excluded`), and its row records the choice."""
    resolved, excluded = resolved or {}, excluded or {}
    total = sum(len(ids) for ids in types.values())
    done = skipped = 0
    for kind, ids in types.items():
        media = "tv" if kind == "tv" else "movie"
        for start in range(0, len(ids), BATCH):
            batch = ids[start:start + BATCH]
            try:
                for item in wd.SPECS:
                    if item.tv_only and media != "tv":
                        continue
                    got, live = wd.fetch_facts(batch, media, item, cache, excluded=excluded.get(kind))
                    for tmdb_id, value in got.items():
                        fields.setdefault(f"{kind}:{tmdb_id}", {})[item.key] = value
                    if live and pace:
                        time.sleep(pace)
                # Asked, so the backfill must not ask again. Only rows Wikidata answered for: a title with
                # no item is left out of the checkpoint so the next run asks for it, as it always was.
                for tmdb_id in batch:
                    row = fields.get(f"{kind}:{tmdb_id}")
                    if row is not None:
                        for key in BACKFILLED:
                            row.setdefault(key, [])
                # The languages were fetched above; the original title is chosen in one of them.
                languages = {i: (fields.get(f"{kind}:{i}") or {}).get("languages") or [] for i in batch}
                for tmdb_id, found in wd.titles(batch, media, languages, excluded=excluded.get(kind)).items():
                    strings = title_strings(found)
                    if strings:
                        fields.setdefault(f"{kind}:{tmdb_id}", {})["titles"] = strings
                # After the properties, so a contested title nothing chose still has a row that says so.
                for tmdb_id in batch:
                    chosen = wikidata.provenance(resolved.get(f"{kind}:{tmdb_id}"))
                    if chosen:
                        fields.setdefault(f"{kind}:{tmdb_id}", {}).update(chosen)
            except (wikidata.WikidataError, http.HTTPError) as failure:
                for tmdb_id in batch:
                    fields.pop(f"{kind}:{tmdb_id}", None)
                skipped += len(batch)
                checkpointed()
                say(f"facts: batch of {len(batch)} {kind} FAILED, left for a later pass — {failure}")
                continue
            done += len(batch)
            checkpointed()
            say(f"facts {done}/{total}… answered by {answered()}")
    return skipped


def refresh_collapsed_franchises(fields, cache, checkpointed, excluded=None):
    """Re-ask P179 for the rows an older scrape checkpointed as ONE Q-id. That scrape kept the least Q-id of
    several and dropped the rest, so the series behind a list target (WALL-E's was the BBC list) is not in
    the checkpoint at all, and filtering what is there would lose it. The query text is unchanged, so a
    batch asked before is answered from the cache."""
    excluded = excluded or {}
    stale = by_type(key for key, row in fields.items() if isinstance(row.get("franchise"), str))
    item = next(item for item in wd.SPECS if item.key == "franchise")
    for kind, ids in stale.items():
        media = "tv" if kind == "tv" else "movie"
        for start in range(0, len(ids), BATCH):
            batch = ids[start:start + BATCH]
            try:
                got, _ = wd.fetch_facts(batch, media, item, cache, excluded=excluded.get(kind))
            except (wikidata.WikidataError, http.HTTPError) as failure:
                raise StageError(f"facts: re-asking P179 for {len(batch)} {kind} rows failed ({failure}). The "
                                 f"rest is checkpointed; re-run.") from None
            for tmdb_id in batch:
                row = fields[f"{kind}:{tmdb_id}"]
                if tmdb_id in got:
                    row["franchise"] = got[tmdb_id]
                else:
                    row.pop("franchise", None)
            checkpointed()
    if stale:
        say(f"franchise: re-asked P179 for {sum(len(ids) for ids in stale.values())} collapsed rows")


def backfill_properties(fields, cache, checkpointed, pace=PACE, excluded=None):
    """Ask the `BACKFILLED` properties for every checkpointed row that has no entry for them — each row a
    scrape finished before they joined `wd.SPECS`. Batched by media type in id order, so a re-run over the
    same checkpoint asks the same queries and is answered from the cache. A contested title is asked about
    its chosen item alone, as every other property is (`excluded`)."""
    excluded = excluded or {}
    for item in (item for item in wd.SPECS if item.key in BACKFILLED):
        stale = by_type(key for key, row in fields.items() if item.key not in row)
        for kind, ids in stale.items():
            media = "tv" if kind == "tv" else "movie"
            ids = sorted(ids)
            for start in range(0, len(ids), BACKFILL_BATCH):
                batch = ids[start:start + BACKFILL_BATCH]
                try:
                    got, live = wd.fetch_facts(batch, media, item, cache, excluded=excluded.get(kind))
                except (wikidata.WikidataError, http.HTTPError) as failure:
                    raise StageError(f"facts: asking {item.prop} for {len(batch)} {kind} rows failed "
                                     f"({failure}). The rest is checkpointed; re-run.") from None
                for tmdb_id in batch:
                    fields[f"{kind}:{tmdb_id}"][item.key] = got.get(tmdb_id, [])
                checkpointed()
                if live and pace:
                    time.sleep(pace)
            say(f"{item.key}: asked {item.prop} for {len(ids)} checkpointed {kind} rows")


def resolve_franchises(fields, cache, items=None):
    """Keep a title's P179 targets only where they are a series (`wd.SERIES_CLASSES`), most specific first,
    and drop the field where none is. Runs before the entities are named, so a rejected list is not named
    either. Derived, like `basedOnKind`: the checkpoint keeps every target, so the rule can change without
    a re-scrape.

    A title that IS a series another title names belongs to it too. The Beck television series is the item
    its 26 films are each "part of the series" of, and no item states P179 to itself, so without this the
    series shared no franchise with its own films. `items` is each title's own item, keyed like `fields`
    (`identities`)."""
    targets = set()
    for row in fields.values():
        if isinstance(row.get("franchise"), list):
            targets.update(row["franchise"])
    members = wd.series(sorted(targets), cache)
    joined = 0
    for key, own in (items or {}).items():
        row = fields.get(key)
        if row is None or own not in members:
            continue
        listed = row["franchise"] if isinstance(row.get("franchise"), list) else []
        if own not in listed:
            row["franchise"] = listed + [own]
            joined += 1
    if joined:
        say(f"franchise: {joined} titles are a series other titles name, and join it")
    kept = dropped = 0
    for row in fields.values():
        if not isinstance(row.get("franchise"), list):
            continue
        found = wd.franchises(row["franchise"], members)
        if found:
            row["franchise"] = found
            kept += 1
        else:
            del row["franchise"]
            dropped += 1
    say(f"franchise: {len(members)} of {len(targets)} P179 targets are a series; "
        f"{kept} titles keep one, {dropped} had only lists and the like")


def resolve_awards(fields, cache):
    """File each award a title won or was nominated for under its ceremony (`wd.ceremony`): `awardsWonAt`
    is every ceremony it won something at, `awardsNominatedAt` every other one it was nominated at. An
    award no ceremony is found for is left out of both. Derived, like `franchise`: the checkpoint keeps the
    award items, so the rule can change without a re-scrape."""
    items = set()
    for row in fields.values():
        for key in BACKFILLED:
            items.update(row.get(key) or [])
    links = wd.award_links(sorted(items), cache)
    ceremonies = {q: wd.ceremony(links.get(q) or {}) for q in items}

    def at(qids):
        return {ceremonies[q] for q in qids or [] if ceremonies.get(q)}

    titles = 0
    for row in fields.values():
        won = at(row.get("awardsWon"))
        nominated = at(row.get("awardsNominated")) - won
        for key, found in (("awardsWonAt", won), ("awardsNominatedAt", nominated)):
            if found:
                row[key] = sorted(found, key=lambda q: int(q[1:]))
            else:
                row.pop(key, None)
        titles += bool(won or nominated)
    grouped = sum(1 for q in items if ceremonies[q])
    say(f"awards: {grouped} of {len(items)} award items file under a ceremony "
        f"({len(set(ceremonies.values()) - {None})} ceremonies); {titles} titles have one")


def name_entities(names, qids, path, what="entity names"):
    """Name every Q-id in `qids` that `names` has no entry for, into `names`, and save it."""
    unresolved = sorted(set(qids) - set(names))
    say(f"{what}: {len(names)} cached, {len(unresolved)} to resolve")
    if not unresolved:
        return
    for qid, info in wd.entity_details(unresolved).items():
        entry = {}
        if info["name"] is not None:
            entry["en"] = info["name"]
        if info["tmdbPersonId"] is not None:
            entry["tmdbPersonId"] = info["tmdbPersonId"]
        if info["aliases"]:
            # 0x1F-joined because the checkpoint is `{name: string}`; split back into a list below,
            # once, at the boundary — a joined string reaching atlas made the whole file unparseable.
            entry["aliases"] = "\x1f".join(sorted(info["aliases"]))
        if entry:
            names[qid] = entry
    save(path, names)


def resolve_entities(fields, path, cache):
    """Every Q-id a record names, resolved once and remembered: a resumed run that re-resolved all of them
    spent its whole life here at 16,500 titles and never reached a new batch. A single Q-id is walked too:
    `franchise` was one, and a harvest that walked only lists left all 3,019 of them unnamed.

    Then each credited person's traits (`wd.people`: gender, birth, death, citizenship, occupation) and
    birthplace (`wd.birthplaces`: the place and its country), each for every person not yet asked, and the
    names of the items those point at. `{}` records "asked, has none", under `traits` and `birth` apart, so
    a checkpoint named before either existed is asked once. Then each country those name, for its ISO code
    (`""` for none).

    Then each entity's IMDb person id (P345), for every entry not yet asked — the ones just named and the
    ones a checkpoint named before this was asked at all. `""` records "asked, has none"."""
    names = checkpoint(path)
    qids, people = set(), set()
    for row in fields.values():
        # The award items themselves are not named: what ships by name is their ceremony
        # (`awardsWonAt`/`awardsNominatedAt`), and naming every category would add thousands of entities.
        for name, value in row.items():
            if name in BACKFILLED:
                continue
            if isinstance(value, list):
                qids.update(item for item in value if isinstance(item, str) and item.startswith("Q"))
            elif isinstance(value, str) and value.startswith("Q"):
                qids.add(value)
        for name in PERSON_FIELDS:
            people.update(q for q in row.get(name) or [] if isinstance(q, str) and q.startswith("Q"))
    name_entities(names, qids, path)
    # A person with no entry has no name either, and is asked for neither IMDb id nor traits.
    unasked = sorted(qid for qid in people if qid in names and "traits" not in names[qid])
    if unasked:
        found = wd.people(unasked, cache)
        for qid in unasked:
            names[qid]["traits"] = found.get(qid, {})
        save(path, names)
        say(f"person traits: {len(unasked)} people asked, {sum(1 for q in unasked if q in found)} have some")
    unasked = sorted(qid for qid in people if qid in names and "birth" not in names[qid])
    if unasked:
        found = wd.birthplaces(unasked, cache)
        for qid in unasked:
            names[qid]["birth"] = found.get(qid, {})
        save(path, names)
        say(f"birthplaces: {len(unasked)} people asked, {sum(1 for q in unasked if q in found)} have one")
    values = {q for entry in names.values() for key in TRAIT_ITEMS
              for q in (entry.get("traits") or {}).get(key) or []}
    values |= {q for entry in names.values() for q in person_items(entry, BIRTH_ITEMS)}
    name_entities(names, values, path, what="trait values")
    countries = {q for entry in names.values() for q in person_items(entry, COUNTRY_ITEMS)}
    unasked = sorted(qid for qid in countries if qid in names and "iso" not in names[qid])
    if unasked:
        found = wd.country_codes(unasked, cache)
        for qid in unasked:
            names[qid]["iso"] = found.get(qid, "")
        save(path, names)
        say(f"country codes: {len(unasked)} countries asked, {sum(1 for q in unasked if q in found)} have one")
    unasked = sorted(qid for qid, entry in names.items() if "imdbId" not in entry)
    if unasked:
        found = wd.imdb_ids(unasked, cache)
        for qid in unasked:
            names[qid]["imdbId"] = found.get(qid, "")
        save(path, names)
        say(f"imdb ids: {len(unasked)} entities asked, {sum(1 for q in unasked if q in found)} have one")
    return names


def person_items(entry, keys):
    """The items a checkpointed person's `traits` and `birth` name under `keys`."""
    stated = {**(entry.get("traits") or {}), **(entry.get("birth") or {})}
    return [q for key in keys for q in stated.get(key) or []]


def resolve_source_authors(fields, path, cache):
    """Who wrote what each adapted title is adapted from: `sourceAuthors`, the authors (P50) of its
    `basedOn` works, sorted by Q-id number, and left out where none has one. The authors are then named with
    every other entity. Derived like `basedOnKind`: the checkpoint maps each work to its authors, `[]` for
    asked-and-none, so a work is asked once."""
    found = checkpoint(path)
    works = set()
    for row in fields.values():
        if isinstance(row.get("basedOn"), list):
            works.update(q for q in row["basedOn"] if isinstance(q, str) and q.startswith("Q"))
    unresolved = sorted(works - set(found))
    say(f"source authors: {len(found)} works cached, {len(unresolved)} to resolve")
    if unresolved:
        answered = wd.authors(unresolved, cache)
        for qid in unresolved:
            found[qid] = answered.get(qid, [])
        save(path, found)
    titles = 0
    for row in fields.values():
        authors = {q for work in row.get("basedOn") or [] if isinstance(work, str)
                   for q in found.get(work) or []}
        if authors:
            row["sourceAuthors"] = sorted(authors, key=lambda q: int(q[1:]))
            titles += 1
        else:
            row.pop("sourceAuthors", None)
    say(f"sourceAuthors: {titles} titles adapt a work Wikidata names an author of")


def resolve_sources(fields, path):
    """What each adapted title is adapted FROM — a book, a manga, a game — which the bare `basedOn` Q-id
    cannot say. A work Wikidata states no type for is remembered as `[]` so it is not asked again."""
    kinds = checkpoint(path)
    targets = set()
    for row in fields.values():
        if isinstance(row.get("basedOn"), list):
            targets.update(row["basedOn"])
    unresolved = sorted(targets - set(kinds))
    say(f"source kinds: {len(kinds)} cached, {len(unresolved)} to resolve")
    if unresolved:
        kinds.update(wd.instance_of(unresolved))
        for qid in unresolved:
            kinds.setdefault(qid, [])
        save(path, kinds)
    counts = {}
    for row in fields.values():
        if not isinstance(row.get("basedOn"), list):
            continue
        # A work Wikidata states no type for has no kind: left out, never a null beside the real ones.
        found = {wd.strongest_kind(kinds.get(qid) or []) for qid in row["basedOn"]} - {None}
        if found:
            row["basedOnKind"] = sorted(found)
            for kind in found:
                counts[kind] = counts.get(kind, 0) + 1
    say("basedOnKind: " + " ".join(f"{k}={n}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1])))


def shipped_entities(names, fields):
    """The entity map as it ships. A genre's name loses its medium ("drama television series" is a poor
    display string and defeats the TMDB match) — genres only: a person named "... film" is not rewritten.

    Only the name. The Swift replaced the whole entry, so a genre shipped without its aliases (414 of the
    858 genres in the shipped facts had some) — and a Q-id that is a genre in one record is a type, a
    subject or a composer in another (108 of them, named by 11,855 records), where losing the aliases
    loses the search strings too."""
    entities = dict(names)
    genres = set()
    for row in fields.values():
        if isinstance(row.get("genres"), list):
            genres.update(row["genres"])
    for qid in genres:
        name = (entities.get(qid) or {}).get("en")
        if name:
            stripped = wikidata.stripped_genre(name)
            if stripped:
                entities[qid] = {**entities[qid], "en": stripped}
    return entities


def entity_out(entry):
    out = {key: entry[key] for key in ("en", "tmdbPersonId", "imdbId") if entry.get(key)}
    aliases = [part for part in (entry.get("aliases") or "").split("\x1f") if part]
    if aliases:
        out["aliases"] = aliases
    # A person's traits ship beside the name, flat: `gender`, `born`, `died`, `citizenship`, `occupation`,
    # and where they were born, `birthplace` and `birthcountry`. A country ships its ISO code as `iso`.
    out.update(entry.get("traits") or {})
    out.update(entry.get("birth") or {})
    if entry.get("iso"):
        out["iso"] = entry["iso"]
    return out


def ambiguous_keys(records):
    """The titles several items claim and nothing chose between — written with no Wikidata fields at all,
    so they have no card. `store/build.py` counts the same thing for the publish gate."""
    return sorted(f"{r['mediaType']}:{r['tmdbId']}" for r in records
                  if r.get("wikidataCandidates") and not r.get("wikidataItem"))


def scrape(keys, has_vector, directory, version, out, cache, pace=PACE):
    """One pass: `keys` scraped into `out`, checkpointed in `directory`. Returns how many ids it skipped."""
    fields_path = os.path.join(directory, "facts-fields.json")
    fields = checkpoint(fields_path)
    types = by_type(keys)
    requested = [f"{kind}:{i}" for kind, ids in types.items() for i in ids]
    say(f"facts: {sum(len(ids) for ids in types.values())} titles, {len(wd.SPECS)} properties")
    resolved, excluded = identities(types, cache)
    contested = {key for key, found in resolved.items() if found.get("candidates")}
    say(f"facts: {len(contested)} titles have several Wikidata items claiming their TMDB id; "
        f"{sum(1 for key in contested if resolved[key]['item'] is None)} of them nothing singles one out of")
    # A contested row checkpointed under another choice is scraped again rather than trusted: one from
    # before the choice existed merged every claimant, and one checkpointed as ambiguous has nothing in it
    # now that a decision names its item.
    merged = sorted(key for key in contested if key in fields
                    and wikidata.provenance(resolved[key]) != {name: fields[key][name] for name in
                                                              ("wikidataItem", "wikidataCandidates")
                                                              if name in fields[key]})
    for key in merged:
        del fields[key]
    if merged:
        say(f"facts: re-scraping {len(merged)} checkpointed contested titles scraped under another choice")
    if fields:
        say(f"resuming from {len(fields)} checkpointed titles")
        types = {kind: [i for i in ids if f"{kind}:{i}" not in fields] for kind, ids in types.items()}
    skipped = scrape_batches(fields, types, cache, pace, lambda: save(fields_path, fields), resolved, excluded)
    refresh_collapsed_franchises(fields, cache, lambda: save(fields_path, fields), excluded)
    backfill_properties(fields, cache, lambda: save(fields_path, fields), pace, excluded)

    resolve_franchises(fields, cache, {key: found["item"] for key, found in resolved.items() if found.get("item")})
    resolve_awards(fields, cache)
    # Before the entities are named, so the authors are named with them.
    resolve_source_authors(fields, os.path.join(directory, "facts-source-authors.json"), cache)
    names = resolve_entities(fields, os.path.join(directory, "facts-entities.json"), cache)
    resolve_sources(fields, os.path.join(directory, "facts-source-types.json"))
    entities = shipped_entities(names, fields)
    # Built over EVERY entity, not only the Q-ids some record names as a genre — the Swift's rule, kept. So
    # a type, a subject or a studio whose name matches passes as a genre: 14 of the shipped 82 entries are
    # no record's genre ("anime television series" as instanceOf, "family" as a main subject, a studio
    # called Lunanime). Harmless while atlas looks genreMap up only by a record's `genres`; restricting it
    # to them changes the published map, which is a decision for the consumer's side.
    genre_map = wd.genre_map(entities)
    say(f"genreMap: {len(genre_map)} genres map to TMDB ids")

    # The checkpoint remembers every title this directory ever scraped; the pass writes the titles it was
    # asked for. A title the labels no longer carry stays in the checkpoint, and writing it out anyway would
    # stamp `hasVector` on a record whose vector is gone — three of them in the shipped facts.
    records = []
    for key in sorted(set(requested) & set(fields)):
        media, tmdb_id = key.split(":")
        row = {name: value for name, value in fields[key].items() if not (name in BACKFILLED and not value)}
        records.append({**row, "mediaType": media, "tmdbId": int(tmdb_id), "hasVector": has_vector})
    save(out, {"schema": 1, "datasetVersion": version, "genreMap": genre_map,
               "entities": {qid: entity_out(entry) for qid, entry in entities.items()}, "records": records})
    ambiguous = ambiguous_keys(records)
    if ambiguous:
        say(f"WARNING: {len(ambiguous)} titles ship with NO Wikidata fields: several items claim each one's TMDB "
            f"id and nothing chose between them. Decide each in {os.path.relpath(wikidata.DECISIONS, REPO)} "
            f"and re-run; the publish refuses until then: {', '.join(ambiguous)}")
    say(json.dumps({"facts": len(records), "entities": len(entities), "genreMap": len(genre_map),
                    "path": out, "skippedAfterFailure": skipped, "hasVector": int(has_vector),
                    "ambiguousItems": len(ambiguous), "answeredBy": dict(sorted(wd.ANSWERED.items()))}))
    return skipped


def argv(ctx):
    """The merge's command line: corpus pass first, because the first file wins a collision."""
    command = [sys.executable, SCRIPT]
    command += [ctx.path(artifacts.CORPUS_FACTS), ctx.path(artifacts.DELTA_FACTS)]
    command += [ctx.path(artifacts.FACTS), "--version", ctx.dataset_version]
    return command


def merge(ctx):
    out = ctx.path(artifacts.FACTS)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    result = subprocess.run(argv(ctx))
    if result.returncode != 0:
        raise StageError(f"facts: {SCRIPT} exited {result.returncode}")
    return out


def run(ctx, cache=None):
    """Scrape both passes, then merge them into the file that ships. Returns its path."""
    ctx.require(artifacts.MANIFEST)
    try:
        ctx = dataclasses.replace(ctx, dataset_version=finalize.manifest_version(ctx))
    except StageError as refusal:
        raise StageError(f"facts: {refusal}") from None
    labels = ctx.require(BOUND[artifacts.VECTOR_LABELS.name].artifact)
    delta = ctx.require(artifacts.DELTA_IDS)
    cache = wikidata.cache_for() if cache is None else cache
    skipped = 0
    for has_vector, keys, artifact in ((True, keys_from_labels(labels), artifacts.CORPUS_FACTS),
                                       (False, keys_from_ids(delta), artifacts.DELTA_FACTS)):
        directory = os.path.join(ctx.out_dir, CHECKPOINTS[has_vector])
        skipped += scrape(keys, has_vector, directory, ctx.dataset_version, ctx.path(artifact), cache)
    if skipped:
        raise StageError(f"facts: {skipped} ids were skipped after their batches failed, so the scrape is "
                         f"not finished and the merge would publish without them. Everything else is "
                         f"checkpointed; re-run to sweep them up.")
    return merge(ctx)
