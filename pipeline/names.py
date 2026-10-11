#!/usr/bin/env python3
"""The names table: every film and series Wikidata gives a TMDB id, with its name and year.

    python3 -m pipeline.names --out out/names.json

The store holds the titles that cleared the corpus. A household's watchlist and history reach further — a
title without a Wikipedia plot, one released since the last build — and an assistant reading them would
meet those titles as bare ids. TMDB's own names may not reach a model (its terms), so this table is built
from Wikidata alone, which is CC0: every item stating a TMDB movie id (P4947) or TMDB TV series id (P4983),
its name, and the year it came out. The store writer keeps the ones it has no row for, as the optional
`wd_name_*` sections (den-spec `wire/store-v3.md`, "Wikidata names"); nothing searches or ranks them.

**Which name.** The English label, else the multilingual (`mul`) one — Wikidata moved many proper names to
`mul` — else the title (P1476), else the label in the language of the work's original language (P364 →
P424). An item with none of those is left out: it has no name to publish. The writer strips a Wikipedia
disambiguator from it the way it strips one from a card (`store/cards.py`). **The original title** is the
title (P1476) in the original language, or the only title the item states, kept when it differs from the
name. **The year** is the earliest publication date (P577) for a film; for a series, the earliest start
time (P580), else its earliest publication date.

**One item per id.** About one id in 560 is stated by two items. The committed decision for it wins
(`data/wikidata-item-decisions.json`), then the rules the facts stage uses (`lib/wikidata.choose`): the item
stating no other TMDB id, then the one with an English article. An id nothing singles an item out for is
left out rather than named after two works.

**Where it is asked.** QLever's Wikidata endpoint, which indexes the weekly dump and answers a whole-catalogue
scan in seconds; WDQS caps a query at 60 s and would have to be paged by thousands of VALUES batches. The
scans are paged (`PAGE` rows, ordered), sent with the pipeline's User-Agent and retried with backoff by
`lib/http.py`. A failed page fails the table: a partial names table would quietly name fewer titles.

The daily job refreshes it when a new generation is built and otherwise carries the live generation's copy
(`refresh`), so a day that rebuilds the same generation rebuilds the same store.
"""
import argparse
import datetime
import json
import os
import sys

if not __package__:
    sys.path[0] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from lib import cache as caching  # noqa: E402
from lib import http  # noqa: E402
from lib import wikidata  # noqa: E402
from lib.wikidata_facts import QLEVER_HOST, QLEVER_PATH, QLEVER_PREFIXES  # noqa: E402

#: Rows per scan page. A page of 100,000 bindings is ~15 MB of JSON and answers in a few seconds.
PAGE = 100_000
#: A whole-catalogue scan takes longer than a 100-id batch; `lib/http.py`'s 60 s is for those.
TIMEOUT = 180
SCHEMA = 1
MEDIA = ("movie", "tv")
#: The release-date properties per media, most authoritative first. A series' first air date is its start
#: time (P580); many state only a publication date.
DATES = {"movie": ("P577",), "tv": ("P580", "P577")}
XSD_DATETIME = "http://www.w3.org/2001/XMLSchema#dateTime"


class NamesError(RuntimeError):
    """The table could not be built: an endpoint failed or answered something that is not a result."""


def ask(query, timeout=TIMEOUT):
    """The bindings of one QLever query."""
    try:
        payload = http.request(QLEVER_HOST, QLEVER_PATH, {"format": "json"}, method="POST",
                               body=(QLEVER_PREFIXES + query).encode("utf-8"),
                               headers={"Content-Type": "application/sparql-query",
                                        "Accept": "application/sparql-results+json"}, timeout=timeout)
    except http.HTTPError as failure:
        raise NamesError(f"names: QLever failed: {failure}") from failure
    try:
        bindings = json.loads(payload.decode("utf-8"))["results"]["bindings"]
        if not isinstance(bindings, list):
            raise TypeError(bindings)
    except (ValueError, KeyError, TypeError):
        raise NamesError(f"names: QLever answered something that is not a SPARQL result: {payload[:200]!r}") \
            from None
    return bindings


def paged(select, where, order, asker=ask):
    """Every binding of `SELECT select WHERE { where }`, `PAGE` rows at a time in `order`."""
    out, offset, page = [], 0, PAGE
    while True:
        rows = asker(f"SELECT {select} WHERE {{\n{where}\n}}\nORDER BY {order}\nLIMIT {page}\nOFFSET {offset}")
        out.extend(rows)
        if len(rows) < page:
            return out
        offset += page


def cell(binding, name):
    value = (binding.get(name) or {}).get("value")
    return value if isinstance(value, str) else None


def qid(binding, name="item"):
    value = cell(binding, name)
    return wikidata._item(value) if value else None


def year_of(binding, name="date"):
    """The year of an `xsd:dateTime` cell, or None for anything else (an unknown value, a bare string)."""
    found = binding.get(name) or {}
    if found.get("datatype") != XSD_DATETIME or not isinstance(found.get("value"), str):
        return None
    text = found["value"]
    try:
        year = int(text.lstrip("+-").split("-")[0])
    except (ValueError, IndexError):
        return None
    return -year if text.startswith("-") else year


def scan(media, asker=ask):
    """Everything Wikidata says that the table reads, for one media: `(claims, labels, titles, codes,
    native, years)`, each keyed by Q-id."""
    prop = wikidata.ID_PROPERTY[media]
    claims = {}
    for row in paged("?item ?id", f"  ?item wdt:{prop} ?id .", "?item ?id", asker=asker):
        item, raw = qid(row), cell(row, "id")
        if item and raw and raw.isdigit():
            claims.setdefault(item, set()).add(int(raw))
    labels = {}
    for row in paged("DISTINCT ?item ?label",
                     f"  ?item wdt:{prop} [] ; rdfs:label ?label .\n"
                     f'  FILTER(LANG(?label) = "en" || LANG(?label) = "mul")', "?item ?label", asker=asker):
        item = qid(row)
        if item:
            labels.setdefault(item, {})[(row.get("label") or {}).get("xml:lang")] = cell(row, "label")
    titles = {}
    for row in paged("DISTINCT ?item ?title", f"  ?item wdt:{prop} [] ; wdt:P1476 ?title .", "?item ?title",
                     asker=asker):
        item, text = qid(row), cell(row, "title")
        if item and text:
            titles.setdefault(item, []).append(((row.get("title") or {}).get("xml:lang") or "", text))
    codes = {}
    for row in paged("DISTINCT ?item ?code", f"  ?item wdt:{prop} [] ; wdt:P364 ?language .\n"
                     "  ?language wdt:P424 ?code .", "?item ?code", asker=asker):
        item, code = qid(row), cell(row, "code")
        if item and code:
            codes.setdefault(item, set()).add(code)
    native = {}
    for row in paged("DISTINCT ?item ?label", f"  ?item wdt:{prop} [] ; wdt:P364 ?language ; rdfs:label ?label .\n"
                     "  ?language wdt:P424 ?code .\n  FILTER(LANG(?label) = ?code)", "?item ?label", asker=asker):
        item, text = qid(row), cell(row, "label")
        if item and text:
            native.setdefault(item, []).append(text)
    years = {}
    for prop_date in DATES[media]:
        found = {}
        for row in paged("DISTINCT ?item ?d", f"  ?item wdt:{prop} [] ; wdt:{prop_date} ?d .", "?item ?d",
                         asker=asker):
            item, year = qid(row), year_of(row, "d")
            if item and year is not None:
                found[item] = min(year, found.get(item, year))
        for item, year in found.items():
            years.setdefault(item, year)
    return claims, labels, titles, codes, native, years


def evidence(qids, media, asker=ask):
    """`lib/wikidata.parse_evidence`'s shape for the claimants of contested ids, asked of QLever."""
    out = {}
    ordered = sorted(set(qids), key=lambda q: (len(q), q))
    for start in range(0, len(ordered), wikidata.EVIDENCE_BATCH):
        query = wikidata.evidence_query(ordered[start:start + wikidata.EVIDENCE_BATCH], media)
        payload = json.dumps({"results": {"bindings": asker(query)}}).encode("utf-8")
        out.update(wikidata.parse_evidence(payload))
    return out


def chosen_items(claims, media, decisions, asker=ask):
    """`tmdbId -> Q-id`, one item per id (the module docstring's rule), and how many ids were left out."""
    by_id = {}
    for item, ids in claims.items():
        for tmdb_id in ids:
            by_id.setdefault(tmdb_id, []).append(item)
    contested = {tmdb_id: sorted(items, key=lambda q: (len(q), q))
                 for tmdb_id, items in by_id.items() if len(items) > 1}
    undecided = {tmdb_id: items for tmdb_id, items in contested.items()
                 if decisions.get((media, tmdb_id)) not in items}
    proof = evidence([q for items in undecided.values() for q in items], media, asker) if undecided else {}
    out, ambiguous = {}, 0
    for tmdb_id, items in by_id.items():
        if tmdb_id not in contested:
            out[tmdb_id] = items[0]
        elif tmdb_id not in undecided:
            out[tmdb_id] = decisions[(media, tmdb_id)]
        else:
            item, _rule = wikidata.choose(items, proof)
            if item is None:
                ambiguous += 1
            else:
                out[tmdb_id] = item
    return out, ambiguous


def clean(text):
    return " ".join(text.split()) if isinstance(text, str) else ""


def name_of(item, labels, titles, codes, native):
    """`(name, original title or None)` for one item, by the module docstring's rule."""
    own = labels.get(item) or {}
    stated = sorted(titles.get(item) or ())
    languages = codes.get(item) or set()
    in_original = [text for lang, text in stated if lang in languages]
    original = clean(in_original[0]) if in_original else (clean(stated[0][1]) if len(stated) == 1 else "")
    for candidate in (own.get("en"), own.get("mul"), stated[0][1] if stated else None,
                      sorted(native.get(item) or [""])[0]):
        name = clean(candidate)
        if name:
            return name, (original if original and original != name else None)
    return None, None


def index_description():
    """What QLever says its index is — which Wikidata dump, and whether it is updated live since."""
    try:
        stats = json.loads(http.request(QLEVER_HOST, QLEVER_PATH, {"cmd": "stats"}).decode("utf-8"))
    except (http.HTTPError, ValueError) as failure:
        raise NamesError(f"names: QLever's stats did not answer: {failure}") from failure
    name = stats.get("name-index") if isinstance(stats, dict) else None
    if not isinstance(name, str) or not name:
        raise NamesError(f"names: QLever's stats name no index: {str(stats)[:200]}")
    return name


def build(asker=ask, decisions=None, describe=index_description, today=None):
    """The table: `{"schema", "source", "rows": [[media, tmdbId, name, year, original], ...], "counts"}`,
    rows sorted by media then id. `source` says where and when it was read, for the manifest's provenance."""
    decisions = wikidata.load_decisions() if decisions is None else decisions
    source = {"name": "wikidata", "license": "CC0-1.0", "endpoint": f"https://{QLEVER_HOST}{QLEVER_PATH}",
              "index": describe(),
              "fetched": (today or datetime.datetime.now(datetime.timezone.utc).date()).isoformat()}
    rows, counts = [], {}
    for media in MEDIA:
        claims, labels, titles, codes, native, years = scan(media, asker)
        items, ambiguous = chosen_items(claims, media, decisions, asker)
        named = 0
        for tmdb_id in sorted(items):
            item = items[tmdb_id]
            name, original = name_of(item, labels, titles, codes, native)
            if name is None:
                continue
            rows.append([media, tmdb_id, name, years.get(item), original])
            named += 1
        counts[media] = {"ids": len(items) + ambiguous, "ambiguous": ambiguous, "named": named,
                         "withYear": sum(1 for row in rows if row[0] == media and row[3] is not None),
                         "withOriginal": sum(1 for row in rows if row[0] == media and row[4] is not None)}
    return {"schema": SCHEMA, "source": source, "counts": counts, "rows": rows}


def write(table, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    body = json.dumps(table, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    caching.write_atomically(path, body.encode("utf-8"))


def refresh(path, new_generation, build_table=build):
    """The daily step: rebuild the table at `path` when the day builds a new generation, else keep the copy
    the live generation's bundle laid there. Returns `(what happened, why)`; a failed fetch keeps the carried
    copy, and the store stage refuses a day that would drop a table the live store carried.

    Only a new generation gets a new table: `publish-dataset.sh` refuses a store that changed under an
    unchanged `datasetVersion`, so a table refreshed on a day that rebuilt the live generation would refuse
    that day's publish."""
    if not new_generation:
        return "kept", "the live generation is rebuilt as it was, so its names table is too"
    try:
        table = build_table()
    except (NamesError, wikidata.WikidataError) as failure:
        carried = "the live generation's copy is kept" if os.path.exists(path) else "the store has no names table"
        return "failed", f"{failure}; {carried}"
    write(table, path)
    return "refreshed", json.dumps(table["counts"], sort_keys=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, help="where to write names.json")
    args = parser.parse_args(argv)
    try:
        table = build()
    except (NamesError, wikidata.WikidataError) as failure:
        print(f"error: {failure}", file=sys.stderr)
        return 1
    write(table, args.out)
    print(json.dumps({"out": args.out, "rows": len(table["rows"]), "counts": table["counts"]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
