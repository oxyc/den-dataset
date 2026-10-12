#!/usr/bin/env python3
"""Director (P57) and genre (P136) from Wikidata — the two clauses of the embedding document that still
came from TMDB.

Everything else in the CC0 document shape is already clean: the plot is Wikipedia, the themes are our own
tags, and `Created by` rides along on the mapping hop. These two are what is left, and they come from here
so the vectors can be built with no TMDB Content in them at all.

**Two requests per batch, not one query with two OPTIONALs.** Both properties are multi-valued, and one
query with several OPTIONALs returns their CROSS PRODUCT — which times out at WDQS on a 100-id batch.

**The label language is `en,mul`, not `en`.** Wikidata has moved proper names to the `mul` (multilingual)
code: Christopher Nolan (Q25191) has no English `rdfs:label` at all. Asking for `en` alone makes the label
service return the bare Q-id, which `parse_labelled` drops as an identifier — so the director vanishes
with no error anywhere. Measured: that silently cost Inception, The Dark Knight and The Prestige theirs.

The query TEXT is part of the cache key, so it is reproduced byte for byte from the pass that scraped the
shipped corpus. Reformatting it is a re-scrape of ~770 requests.
"""
import collections
import concurrent.futures
import json
import math
import os
import re
import sys
import urllib.parse

from . import cache as caching
from . import http

HOST = "query.wikidata.org"
PATH = "/sparql"

#: A second endpoint for the per-batch queries: QLever's index of the weekly Wikidata dump. When WDQS
#: throttles (429 with `Retry-After: 120`, 20–90 s queries) a corpus pass takes a day; QLever answered the
#: same batch query in 0.5 s with the same rows. It is a dump, so an edit made since that week's dump is
#: not in it. It has no `SERVICE wikibase:label`; `qlever_text` resolves labels in the query instead.
#:
#: Its own limit is 30 s, answered as a 429 saying `Operation timed out`. Measured on a 150-id batch (and on
#: 75, 38, 19 and 10): the mapping takes 14 s, the languages, kinds, origins, claimants and doc-facts
#: queries under a second — but `source_query` (the class walk), `target_query` with language labels and
#: `wikipedias_query` (a COUNT over every sitelink) time out at every size. Those three are asked of WDQS
#: only (`qlever=False` below), since QLever would add two minutes of retries before the same failure.
QLEVER_HOST = "qlever.dev"
QLEVER_PATH = "/api/wikidata"
#: WDQS predeclares these; QLever resolves only prefixes the query declares. Sent to QLever only: the
#: cache key is the query text as WDQS is asked it, so an answer from either endpoint is kept under it.
QLEVER_PREFIXES = ("PREFIX wd: <http://www.wikidata.org/entity/>\n"
                   "PREFIX wdt: <http://www.wikidata.org/prop/direct/>\n"
                   "PREFIX p: <http://www.wikidata.org/prop/>\n"
                   "PREFIX psv: <http://www.wikidata.org/prop/statement/value/>\n"
                   "PREFIX wikibase: <http://wikiba.se/ontology#>\n"
                   "PREFIX schema: <http://schema.org/>\n"
                   "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
                   "PREFIX skos: <http://www.w3.org/2004/02/skos/core#>\n")
#: `qlever` asks QLever first and WDQS only when it fails; anything else asks WDQS first.
PREFER_VAR = "DEN_SPARQL_PREFER"
#: Which endpoint answered each live request, for the stage's log.
ANSWERED = collections.Counter()

_LABEL_SERVICE = re.compile(r'SERVICE wikibase:label \{ bd:serviceParam wikibase:language "([^"]+)"\. \}')
_SELECTED_LABEL = re.compile(r"\?(\w+)Label\b")
_OPTIONAL_LINE = re.compile(r"^\s*OPTIONAL \{ [^{}]* \}\s*$")


def _label_block(var, languages):
    """What `SERVICE wikibase:label` does for `?{var}Label`, as patterns QLever can run: the first of
    `languages` the item has a label in (`en,mul` — `en` first), bound to the same variable. An item with no
    label in any leaves it unbound where the service would answer the bare Q-id, and every parser drops both."""
    codes = [code.strip() for code in languages.split(",")]
    parts = [f'OPTIONAL {{ ?{var} rdfs:label ?{var}Label_{code} . FILTER(LANG(?{var}Label_{code}) = "{code}") }}'
             for code in codes]
    return " ".join(parts + [f"BIND(COALESCE({', '.join(f'?{var}Label_{c}' for c in codes)}) AS ?{var}Label)"])


def qlever_text(query):
    """The query QLever is asked for `query`, or None when it cannot be asked there.

    A query without the label service is sent as is. With it, the service line is replaced by the labels'
    patterns (`_label_block`) for each `?xLabel` the query selects. When `?x` is only bound inside a
    one-line `OPTIONAL { … }` (the mapping's `?creator`) the patterns go INSIDE it: after it, they would
    join on a possibly-unbound `?x`, which SPARQL reads as "any label at all". Anything this does not
    recognise stays on WDQS.
    """
    service = _LABEL_SERVICE.search(query)
    if service is None:
        return None if "SERVICE wikibase:label" in query else query
    head = query.split(" WHERE", 1)[0]
    variables = sorted(set(_SELECTED_LABEL.findall(head)))
    # `[AUTO_LANGUAGE]` is the service's own, and a code that is not plain letters cannot name a variable.
    if not variables or not all(code.isalpha() for code in service.group(1).split(",")):
        return None
    lines = query.split("\n")
    blocks = []
    for var in variables:
        mention = re.compile(rf"\?{var}\b")
        at = [i for i, line in enumerate(lines) if mention.search(line) and "SERVICE wikibase:label" not in line]
        block = _label_block(var, service.group(1))
        if at and all(lines[i].lstrip().startswith("OPTIONAL {") for i in at):
            if not _OPTIONAL_LINE.match(lines[at[-1]]):
                return None
            lines[at[-1]] = lines[at[-1]].rstrip()[:-1] + block + " }"
        else:
            blocks.append(block)
    text = "\n".join(lines)
    return _LABEL_SERVICE.sub(lambda _: " ".join(blocks), text, count=1)


def _ask(name, query, attempts=http.ATTEMPTS):
    host, path, text = (QLEVER_HOST, QLEVER_PATH, QLEVER_PREFIXES + query) if name == "qlever" else \
        (HOST, PATH, query)
    payload = http.request(host, path, {"format": "json"}, method="POST", body=text.encode("utf-8"),
                           headers={"Content-Type": "application/sparql-query",
                                    "Accept": "application/sparql-results+json"}, attempts=attempts)
    ANSWERED[name] += 1
    return payload


def sparql(query, fallback=False, env=None):
    """The body of one SPARQL request. With `fallback`, QLever stands behind WDQS (or in front of it, when
    `PREFER_VAR` says so). WDQS is then asked once: its retries wait out a `Retry-After` of up to two
    minutes each, which is the throttle QLever is there to avoid. It hands over only on a 429, a 5xx or a
    timeout — any other status is its answer about the query. QLever hands over on any failure, since a
    query it cannot run is still one WDQS can.

    When QLever is throttling too (a 429 that is not its own timeout), WDQS is asked again with its retries:
    waiting out its `Retry-After` is slow, but it is the only endpoint left, and giving up there threw away a
    daily run at docfacts after 73 minutes (2026-10-12, run 38265391264). A QLever timeout or 5xx still
    raises, so `_asked_batch` asks the batch as halves rather than waiting minutes on a query too large."""
    elsewhere = qlever_text(query) if fallback else None
    if elsewhere is None:
        return _ask("wdqs", query)
    env = os.environ if env is None else env
    if env.get(PREFER_VAR) == "qlever":
        try:
            return _ask("qlever", elsewhere)
        except http.HTTPError:
            return _ask("wdqs", query)
    try:
        return _ask("wdqs", query, attempts=1)
    except http.HTTPError as failure:
        if failure.status != 0 and not http.is_transient(failure.status):
            raise
    try:
        return _ask("qlever", elsewhere)
    except http.HTTPError as failure:
        if failure.status != 429 or _splittable(failure):
            raise
    return _ask("wdqs", query)

#: Batches of one lookup in flight at once. WDQS allows five concurrent queries per client and answers 429
#: beyond that; three is the doc-facts scrape's figure (`pipeline/docfacts.py`), kept well under the limit.
#: A daily run spent over three minutes asking these lookups one batch at a time.
LOOKUP_CONCURRENCY = 3


def in_parallel(ask, batches):
    """`[ask(batch) for batch in batches]`, `LOOKUP_CONCURRENCY` at a time. The answers come back in batch
    order, so a caller merging them in that order builds what the one-at-a-time loop built; the first
    failure is raised as the loop would have raised it."""
    if len(batches) <= 1:
        return [ask(batch) for batch in batches]
    with concurrent.futures.ThreadPoolExecutor(max_workers=LOOKUP_CONCURRENCY) as pool:
        return list(pool.map(ask, batches))

#: The TMDB-id property per media. A film and a series are different statements, and asking the wrong one
#: returns nothing rather than erroring.
ID_PROPERTY = {"movie": "P4947", "tv": "P4983"}

DIRECTOR, GENRE = "P57", "P136"
#: What a title IS — `film`, `anime television series`. Asked beside P136 by `kind_query`, never by the
#: doc-facts query, whose text is a cache key.
INSTANCE_OF = "P31"

#: A label the SERVICE could not resolve comes back as the bare Q-id.
QID = re.compile(r"^Q\d+$")

#: Longest first, so "television series" is taken whole before "series" can bite into it. `television
#: program`, `television` and `anime and manga` were missing once, which left `reality television`,
#: `crime fiction` and their kin unmatched against a TMDB vocabulary that does contain them: 38 genre
#: Q-ids across 1,014 references, unmapped for want of a suffix.
MEDIA_SUFFIXES = ("television program", "television series", "anime and manga", "tv series",
                  "television", "series", "movie", "anime", "film")


class WikidataError(RuntimeError):
    """A 200 that is not a SPARQL result — WDQS maintenance HTML, a proxy error page. Distinguished from
    "no bindings" because the two mean opposite things to a resumable scrape."""


def exclusion(ids, excluded):
    """The line that keeps every per-title query on ONE item, or "" when no id in the batch needs it.

    Every query here finds its title as `?film wdt:P4947 ?tmdb`, and two items can state the same TMDB id:
    series 2559 is claimed by "Boon" (1986) and by "Bonn – Alte Freunde, neue Feinde" (2023). Keyed by
    `?tmdb`, their answers merged into one row — the card title from one, the IMDb id from the other. The
    MINUS drops the items `resolve` did not choose, pair by pair: an item set aside for one id may be the
    only claimant of another in the same batch.

    A batch with no such id gets NO line, so its text — the cache key — is the one already on disk.
    """
    pairs = [(tmdb_id, qid) for tmdb_id in sorted(set(int(i) for i in ids))
             for qid in (excluded or {}).get(tmdb_id, ())]
    if not pairs:
        return ""
    rows = " ".join(f'("{tmdb_id}" wd:{qid})' for tmdb_id, qid in pairs)
    return f"  MINUS {{ VALUES (?tmdb ?film) {{ {rows} }} }}\n"


def query_text(ids, media, prop, excluded=None):
    """The SPARQL this pipeline has always sent. Whitespace included — it is hashed into the cache key."""
    values = " ".join(f'"{tmdb_id}"' for tmdb_id in ids)
    return (f"SELECT ?tmdb ?vLabel WHERE {{\n"
            f"  VALUES ?tmdb {{ {values} }}\n"
            f"  ?film wdt:{ID_PROPERTY[media]} ?tmdb .\n"
            f"{exclusion(ids, excluded)}"
            f"  ?film wdt:{prop} ?v .\n"
            f'  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,mul". }}\n'
            f"}}\n"
            f"ORDER BY ?tmdb ?vLabel")


def parse_labelled(payload):
    """`tmdbId -> [label]` for a one-property query.

    Values accumulate per id rather than first-wins, since a film binds once per value.
    """
    try:
        root = json.loads(payload.decode("utf-8"))
        bindings = root["results"]["bindings"]
    except (ValueError, KeyError, TypeError):
        raise WikidataError(f"not a SPARQL result: {payload[:200]!r}") from None
    out = {}
    for binding in bindings:
        raw = (binding.get("tmdb") or {}).get("value")
        label = (binding.get("vLabel") or {}).get("value")
        if not raw or not label or not raw.isdigit():
            continue
        # A Q-id is an identifier, not a name, and embedding "Q1379241" as a director teaches nothing.
        if QID.match(label):
            continue
        values = out.setdefault(int(raw), [])
        if label not in values:
            values.append(label)
    return out


def stripped_genre(label):
    """`"science fiction film"` → `"science fiction"`.

    Wikidata appends the medium to genre labels where TMDB does not, and comparing the raw strings makes a
    vocabulary that largely DOES line up look like it shares nothing: mean overlap with TMDB measured 0.01
    before this strip and 0.40 after. A value that is ONLY the medium says nothing — every film is a film
    — so it becomes empty and the caller drops it.
    """
    text = label.lower().strip()
    changed = True
    while changed:
        changed = False
        for word in MEDIA_SUFFIXES:
            if text.endswith(" " + word):
                text = text[: -(len(word) + 1)].strip()
                changed = True
        # `fiction` only when something survives it AND the whole phrase is not itself a genre — otherwise
        # "science fiction" becomes "science", which is not a TMDB genre and not a thing. hasSuffix rather
        # than equality: "hard science fiction" and "military science fiction" are real referenced genres,
        # and an equality guard strips them to "hard science" and "military science". The stripped label
        # travels into the shipped entity map and the composed document, so a mangled one travels.
        if (not changed and text.endswith(" fiction") and not text.endswith("science fiction")
                and len(text) > len(" fiction") + 1):
            text = text[: -len(" fiction")].strip()
            changed = True
    return "" if text in MEDIA_SUFFIXES else text


def fetch_property(ids, media, prop, cache=None, excluded=None):
    """One property over one batch of ids, from disk where the same batch was asked before.

    A caller's resume already skips ids present in its output file, so the cache earns its keep on a
    different axis: the two properties are two requests over the same id batch, and a re-run scoped to a
    different id list still repeats whole batches whose membership happens to coincide.
    """
    # WDQS only: the names come back in the order the document is composed in, and QLever sorts them
    # case-insensitively where WDQS sorts by code point (measured: `LGBTQ-related film` first on one, last on
    # the other), so an answer from it would be a different embedding input.
    return _asked_batch(ids, lambda batch: query_text(batch, media, prop, excluded), "sparql-docfacts",
                        parse_labelled, cache, qlever=False)


def doc_facts(ids, media, cache=None, excluded=None):
    """`tmdbId -> {"directors": [...], "genres": [...]}` for one batch.

    An id Wikidata states neither for is ABSENT from the result rather than present and empty: unknown is
    not "none", and the caller is the one that decides how to record that.
    """
    unique = sorted(set(int(value) for value in ids))
    if not unique:
        return {}
    directors = fetch_property(unique, media, DIRECTOR, cache, excluded)
    raw_genres = fetch_property(unique, media, GENRE, cache, excluded)
    out = {}
    for tmdb_id in unique:
        genres = sorted({stripped_genre(name) for name in raw_genres.get(tmdb_id, [])} - {""})
        names = directors.get(tmdb_id, [])
        if not names and not genres:
            continue
        out[tmdb_id] = {"directors": names, "genres": genres}
    return out


#: The enrichment's mapping query, line for line as the Swift pass sent it — comments included, because
#: they are part of the text and the text is the cache key. The comments are SPARQL comments; they say why
#: the query has the shape it has, and they reach WDQS with it.
_MAPPING = (
    "SELECT ?tmdb ?article ?sourceArticle ?imdb ?runtime ?creatorLabel ?anyArticle ?anySite WHERE {",
    "  VALUES ?tmdb { {values} }",
    "  ?film wdt:{property} ?tmdb .",
    "  OPTIONAL { ?film wdt:P345 ?imdb . }",
    "  OPTIONAL { ?film wdt:P2047 ?runtime . }",
    "  OPTIONAL { ?film wdt:P170 ?creator . }",
    "  OPTIONAL { ?article schema:about ?film ; schema:isPartOf <https://en.wikipedia.org/> . }",
    "  # SOURCE-WORK FALLBACK. An adaptation's own article is often production-and-episodes with no plot:",
    "  # \"Attack on Titan (TV series)\" is Series overview / Season 1-4 / Cast, while the STORY lives on the",
    "  # article for the work it adapts. P144 (based on) names that work, and it tells the same story, so",
    "  # its plot describes this title: 13 Reasons Why reads its plot off the novel, The Pacific off the",
    "  # memoir, Shooter off Point of Impact.",
    "  #",
    "  # P179 (part of the series) was tried here too and REMOVED. A franchise sibling is not the same",
    "  # story, so it produced confidently wrong plots — Angel grounded on Buffy, Torchwood on Doctor Who,",
    "  # Xena on Hercules, Bates Motel on Psycho. Measured over the titles this fallback newly grounded:",
    "  # 322 came from a P144 source work, against 66 reachable only through P179. Dropping those 66 costs",
    "  # 0.16% of the grounded corpus and removes every such attribution.",
    "  OPTIONAL { ?film wdt:P144 ?basedOn .",
    "             ?sourceArticle schema:about ?basedOn ; schema:isPartOf <https://en.wikipedia.org/> . }",
    "  # OTHER-LANGUAGE ARTICLES. Two thirds of the films with no plot have no English article at all, so",
    "  # no heading rule can reach them — but half of those have one in another language, and bge-m3",
    "  # embeds that prose directly. Restricted to the wikis we have plot-heading lists for; an",
    "  # unrestricted sitelink query returns a row per language and multiplies the whole result set.",
    "  OPTIONAL { ?anyArticle schema:about ?film ; schema:isPartOf ?anySite .",
    "             VALUES ?anySite { {wikis} } }",
    "  SERVICE wikibase:label { bd:serviceParam wikibase:language \"en,mul\". }",
    "}",
    "ORDER BY ?tmdb ?article",
)

#: `+123` and `0123` are integers to Swift's `Int(_:)`, and a SPARQL literal is read the way the pass read it.
_INTEGER = re.compile(r"[+-]?[0-9]+")
_BAD_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


def mapping_query(ids, media, languages, excluded=None):
    """The SPARQL that maps one batch of TMDB ids to their articles, and the facts that ride along.

    `languages` are the other Wikipedias a plot may be read from. They are the query's business too: an
    unrestricted sitelink returns a row per language and multiplies the result set.
    """
    values = " ".join(f'"{tmdb_id}"' for tmdb_id in sorted(set(int(i) for i in ids)))
    wikis = " ".join(f"<https://{code}.wikipedia.org/>" for code in sorted(languages))
    lines = list(_MAPPING)
    minus = exclusion(ids, excluded)
    if minus:
        lines.insert(3, minus.rstrip("\n"))
    return "\n".join(lines).replace("{values}", values).replace(
        "{property}", ID_PROPERTY[media]).replace("{wikis}", wikis)


def article_title(url):
    """`https://en.wikipedia.org/wiki/The_Matrix` → `The Matrix`, percent-decoded.

    A malformed escape leaves the whole name undecoded rather than half-decoded, as Foundation's
    `removingPercentEncoding` does — the stored name is looked up again later, and a partly-decoded one
    names a page that does not exist.
    """
    if "/wiki/" not in url:
        return None
    raw = url.split("/wiki/", 1)[1]
    decoded = raw
    if not _BAD_ESCAPE.search(raw):
        try:
            decoded = urllib.parse.unquote(raw, errors="strict")
        except UnicodeDecodeError:
            decoded = raw
    return decoded.replace("_", " ") or None


def language_code(site):
    """`https://de.wikipedia.org/` → `de`. English is carried separately, as the own article."""
    host = urllib.parse.urlparse(site).hostname or ""
    if not host.endswith(".wikipedia.org"):
        return None
    code = host.replace(".wikipedia.org", "")
    return None if code in ("", "en") else code


def _cell(binding, name):
    cell = binding.get(name)
    if cell is None:
        return None
    if not isinstance(cell, dict) or not isinstance(cell.get("value"), str):
        raise WikidataError(f"a `{name}` cell with no string value: {cell!r}"[:200])
    return cell["value"]


def _minutes(value):
    """Wikidata stores runtime as a decimal ("96" / "96.0"), rounded half away from zero."""
    try:
        minutes = float(value)
    except ValueError:
        return None
    if math.isnan(minutes) or math.isinf(minutes):
        return None
    return int(math.floor(abs(minutes) + 0.5)) * (1 if minutes >= 0 else -1)


def parse_mapping(payload):
    """`tmdbId -> mapping` for one SPARQL body. RAISES on a body that is not a SPARQL result.

    An absent id in a successful result is an answer — the title has no article — and the enrichment
    records it and moves on. A WDQS maintenance page decoded as "no bindings" would make every title in the
    batch plotless and checkpointed, never to be re-grounded, so the two must not look alike.
    """
    try:
        bindings = json.loads(payload.decode("utf-8"))["results"]["bindings"]
        if not isinstance(bindings, list):
            raise TypeError(bindings)
    except (ValueError, KeyError, TypeError):
        raise WikidataError(f"not a SPARQL result: {payload[:200]!r}") from None
    out = {}
    for binding in bindings:
        raw = _cell(binding, "tmdb")
        if raw is None or not _INTEGER.fullmatch(raw):
            continue
        tmdb_id = int(raw)
        entry = out.setdefault(tmdb_id, {"article": None, "sourceArticle": None, "imdb": None,
                                         "runtimeMinutes": None, "creators": [], "articlesByLang": {}})
        # A title binds once per creator, per runtime and per language, so these ACCUMULATE across rows —
        # first-wins would silently drop the second Duffer brother. The single-valued ones are first-wins.
        article = _cell(binding, "article")
        if entry["article"] is None and article is not None:
            entry["article"] = article_title(article)
        # The work this ADAPTS (P144) — its plot is this story. A franchise sibling's is not.
        source = _cell(binding, "sourceArticle")
        if entry["sourceArticle"] is None and source is not None:
            entry["sourceArticle"] = article_title(source)
        if entry["imdb"] is None:
            entry["imdb"] = _cell(binding, "imdb")
        runtime = _cell(binding, "runtime")
        minutes = _minutes(runtime) if runtime is not None else None
        # A series may carry several runtimes (a 50- and a 70-minute cut). The SMALLEST answers "have I
        # got time for this".
        if minutes is not None and (entry["runtimeMinutes"] is None or minutes < entry["runtimeMinutes"]):
            entry["runtimeMinutes"] = minutes
        site, any_article = _cell(binding, "anySite"), _cell(binding, "anyArticle")
        if site is not None and any_article is not None:
            code, title = language_code(site), article_title(any_article)
            if code and title:
                entry["articlesByLang"][code] = title
        creator = _cell(binding, "creatorLabel")
        if creator and creator not in entry["creators"]:
            entry["creators"].append(creator)
            entry["creators"].sort()
    return out


def mapping(ids, media, languages, cache=None, excluded=None):
    """`tmdbId -> mapping` for one batch of one media type, from disk where the same batch was asked before.

    ONE media per call. TMDB's movie and series id spaces overlap — movie 95 is Armageddon, series 95 is
    Buffy — so a lookup shared across a batch holding both once grounded series 91545 (Young Wallander) on
    "Sunday Drive (film)": a confident, completely wrong plot with nothing in the output to mark it.

    This is the most valuable entry in the cache. WDQS is the pipeline's flakiest dependency — it throttled
    to one request a minute during the work that added the cache — and a mapping is stable, so a cached one
    lets a re-run proceed through a WDQS outage entirely.
    """
    if not ids:
        return {}
    return _asked_batch(ids, lambda batch: mapping_query(batch, media, languages, excluded), "sparql",
                        parse_mapping, cache)


def wikipedias_query(ids, media, excluded=None):
    """The SPARQL that counts, for one batch of TMDB ids, how many Wikipedias have an article on each.

    Separate from the mapping query because it is asked BEFORE admission, of the titles TMDB's count left
    below its floor, and it asks for a number rather than the mapping's rows: an unrestricted sitelink
    pattern returns a row per language, which is exactly what `COUNT` folds away on the server. Only
    Wikipedia sitelinks count — `wikibase:sitelinks` also counts Commons, Wikiquote and Wikisource.

    The site is recognised by its URL, not by `?site wikibase:wikiGroup "wikipedia"`: the join takes WDQS
    ~27 s per 150 titles, the string test ~2 s, for the same answer.
    """
    values = " ".join(f'"{tmdb_id}"' for tmdb_id in sorted(set(int(i) for i in ids)))
    return (f"SELECT ?tmdb (COUNT(DISTINCT ?article) AS ?wikis) WHERE {{\n"
            f"  VALUES ?tmdb {{ {values} }}\n"
            f"  ?film wdt:{ID_PROPERTY[media]} ?tmdb .\n"
            f"{exclusion(ids, excluded)}"
            f"  OPTIONAL {{ ?article schema:about ?film ; schema:isPartOf ?site .\n"
            f'             FILTER(STRENDS(STR(?site), ".wikipedia.org/")) }}\n'
            f"}}\n"
            f"GROUP BY ?tmdb\n"
            f"ORDER BY ?tmdb")


def parse_wikipedias(payload):
    """`tmdbId -> count` for one SPARQL body. RAISES on a body that is not a SPARQL result, or on a count
    that is not an integer — read as zero, it would refuse a title for a malformed answer.

    An id with no Wikidata item is absent; the caller reads that as no Wikipedia at all.
    """
    try:
        bindings = json.loads(payload.decode("utf-8"))["results"]["bindings"]
        if not isinstance(bindings, list):
            raise TypeError(bindings)
    except (ValueError, KeyError, TypeError):
        raise WikidataError(f"not a SPARQL result: {payload[:200]!r}") from None
    out = {}
    for binding in bindings:
        raw, count = _cell(binding, "tmdb"), _cell(binding, "wikis")
        if raw is None or not _INTEGER.fullmatch(raw):
            continue
        if count is None or not _INTEGER.fullmatch(count):
            raise WikidataError(f"a Wikipedia count that is not an integer: {count!r} for {raw}")
        out[int(raw)] = int(count)
    return out


def wikipedias(ids, media, day, cache=None, excluded=None):
    """`tmdbId -> how many Wikipedias have an article` for one batch of one media type, from disk where the
    same batch was asked before on the same `day`.

    The day is part of the cache key because the count moves and the gate judges it again daily: a
    below-floor title comes back in a batch much like yesterday's, and a 180-day cache would answer it
    with the same count until the entry expired.
    """
    if not ids:
        return {}
    return _asked_batch(ids, lambda batch: wikipedias_query(batch, media, excluded), "sparql-wikipedias",
                        parse_wikipedias, cache, {"day": day}, qlever=False)


def kind_query(ids, media, excluded=None):
    """The SPARQL that names what one batch of TMDB ids ARE: the labels of their P136 genres and their P31
    types, in one result.

    A UNION rather than two OPTIONALs, and one request rather than two. Both properties are multi-valued,
    so OPTIONALs return their cross product — the reason `doc_facts` asks one property per request — but a
    UNION is a disjunction: each value is its own row, and the two sets simply concatenate.

    LABELS, not Q-ids. Wikidata spreads anime across dozens of items (`anime film`, `anime television
    series`, `fantasy anime and manga`, and a new one whenever an editor needs it), so a caller holding a
    pinned list of Q-ids silently stops matching the ones minted after it was written.
    """
    values = " ".join(f'"{tmdb_id}"' for tmdb_id in sorted(set(int(i) for i in ids)))
    return (f"SELECT ?tmdb ?vLabel WHERE {{\n"
            f"  VALUES ?tmdb {{ {values} }}\n"
            f"  ?film wdt:{ID_PROPERTY[media]} ?tmdb .\n"
            f"{exclusion(ids, excluded)}"
            f"  {{ ?film wdt:{GENRE} ?v . }} UNION {{ ?film wdt:{INSTANCE_OF} ?v . }}\n"
            f'  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,mul". }}\n'
            f"}}\n"
            f"ORDER BY ?tmdb ?vLabel")


def kinds(ids, media, cache=None, excluded=None):
    """`tmdbId -> [label]` — what one batch of titles are, by genre and by type, from disk where the same
    batch was asked before. An id Wikidata states neither for is absent."""
    if not ids:
        return {}
    return _asked_batch(ids, lambda batch: kind_query(batch, media, excluded), "sparql-kind", parse_labelled,
                        cache)


def language_query(ids, media, excluded=None):
    """The SPARQL that names one batch of TMDB ids' original languages: P364, resolved to ISO 639-1 through
    P218, which is the code a Wikipedia sitelink is keyed by.

    Its own request rather than another OPTIONAL on the mapping query: the mapping's TEXT is its cache key
    and ~770 of its bodies are already on disk, so adding a line to it re-asks WDQS for all of them. P364
    is multi-valued too, and an OPTIONAL would multiply the mapping's sitelink rows by it.
    """
    values = " ".join(f'"{tmdb_id}"' for tmdb_id in sorted(set(int(i) for i in ids)))
    return (f"SELECT ?tmdb ?code WHERE {{\n"
            f"  VALUES ?tmdb {{ {values} }}\n"
            f"  ?film wdt:{ID_PROPERTY[media]} ?tmdb .\n"
            f"{exclusion(ids, excluded)}"
            f"  ?film wdt:P364 ?v .\n"
            f"  ?v wdt:P218 ?code .\n"
            f"}}\n"
            f"ORDER BY ?tmdb ?code")


def parse_languages(payload):
    """`tmdbId -> [code]`, lower-cased and sorted. RAISES on a body that is not a SPARQL result.

    Every code a title states, not one: a co-production has several, and the caller tries each of them
    before the wikis the title says nothing about. A language with no P218 has no row — an ISO 639-1 code
    is what a sitelink is keyed by, and there is no article to prefer without one.
    """
    try:
        bindings = json.loads(payload.decode("utf-8"))["results"]["bindings"]
        if not isinstance(bindings, list):
            raise TypeError(bindings)
    except (ValueError, KeyError, TypeError):
        raise WikidataError(f"not a SPARQL result: {payload[:200]!r}") from None
    out = {}
    for binding in bindings:
        raw, code = _cell(binding, "tmdb"), _cell(binding, "code")
        if raw is None or not _INTEGER.fullmatch(raw) or not code:
            continue
        found = out.setdefault(int(raw), [])
        if code.lower() not in found:
            found.append(code.lower())
    return {tmdb_id: sorted(codes) for tmdb_id, codes in out.items()}


def languages(ids, media, cache=None, excluded=None):
    """`tmdbId -> [ISO 639-1 code]` for one batch of one media type, from disk where the same batch was
    asked before. What a title is IN, Wikidata's and CC0, rather than TMDB's `original_language`."""
    if not ids:
        return {}
    return _asked_batch(ids, lambda batch: language_query(batch, media, excluded), "sparql-language",
                        parse_languages, cache)


def origin_query(ids, media, excluded=None):
    """The SPARQL that names one batch of TMDB ids' countries of origin: P495, resolved to ISO 3166-1
    alpha-2 through P297 — the code the admission tiers are written in (`pipeline/floors.py`)."""
    values = " ".join(f'"{tmdb_id}"' for tmdb_id in sorted(set(int(i) for i in ids)))
    return (f"SELECT ?tmdb ?code WHERE {{\n"
            f"  VALUES ?tmdb {{ {values} }}\n"
            f"  ?film wdt:{ID_PROPERTY[media]} ?tmdb .\n"
            f"{exclusion(ids, excluded)}"
            f"  ?film wdt:P495 ?v .\n"
            f"  ?v wdt:P297 ?code .\n"
            f"}}\n"
            f"ORDER BY ?tmdb ?code")


def origins(ids, media, cache=None, excluded=None):
    """`tmdbId -> [ISO 3166-1 alpha-2 code]`, upper-cased and sorted, for one batch of one media type, from
    disk where the same batch was asked before. An id Wikidata states no country for is absent."""
    if not ids:
        return {}
    found = _asked_batch(ids, lambda batch: origin_query(batch, media, excluded), "sparql-origin",
                         parse_languages, cache)
    return {tmdb_id: sorted(code.upper() for code in codes) for tmdb_id, codes in found.items()}


#: What makes a P144 work a SCREEN work: an instance of film, television program or web series, or of any
#: subclass of them (`television series`, `anime film`, `miniseries`, `silent short film`). A class walk
#: rather than `lib/wikidata_facts.SOURCE_KIND_BY_TYPE`'s label list, which names `film` and `television
#: series` but folds `television program`, `miniseries`, `anime film` and `web series` into `other`.
SCREEN_CLASSES = ("Q11424", "Q15416", "Q526877")


def source_query(ids, media, excluded=None):
    """The SPARQL that names, for one batch of TMDB ids, the English article of every work each is based on
    (P144), and whether that work is itself a film or a series.

    Its own request, for the reason `language_query` is: the mapping's text is its cache key. The class
    test is an EXISTS walked from the work up, so WDQS starts from the handful of P144 targets rather than
    from every film there is.
    """
    values = " ".join(f'"{tmdb_id}"' for tmdb_id in sorted(set(int(i) for i in ids)))
    classes = ", ".join(f"wd:{qid}" for qid in SCREEN_CLASSES)
    return (f"SELECT DISTINCT ?tmdb ?sourceArticle ?screen WHERE {{\n"
            f"  VALUES ?tmdb {{ {values} }}\n"
            f"  ?film wdt:{ID_PROPERTY[media]} ?tmdb .\n"
            f"{exclusion(ids, excluded)}"
            f"  ?film wdt:P144 ?basedOn .\n"
            f"  ?sourceArticle schema:about ?basedOn ; schema:isPartOf <https://en.wikipedia.org/> .\n"
            f"  BIND(EXISTS {{ ?basedOn wdt:P31/wdt:P279* ?class . FILTER(?class IN ({classes})) }} AS ?screen)\n"
            f"}}\n"
            f"ORDER BY ?tmdb ?sourceArticle")


def parse_sources(payload):
    """`tmdbId -> {article: is a screen work}`. RAISES on a body that is not a SPARQL result.

    An article two P144 works share is a screen work when either is — the question is whether reading it
    describes another production. An id with no P144 work that has an English article is absent.
    """
    try:
        bindings = json.loads(payload.decode("utf-8"))["results"]["bindings"]
        if not isinstance(bindings, list):
            raise TypeError(bindings)
    except (ValueError, KeyError, TypeError):
        raise WikidataError(f"not a SPARQL result: {payload[:200]!r}") from None
    out = {}
    for binding in bindings:
        raw, url = _cell(binding, "tmdb"), _cell(binding, "sourceArticle")
        if raw is None or not _INTEGER.fullmatch(raw) or url is None:
            continue
        article = article_title(url)
        if article is None:
            continue
        works = out.setdefault(int(raw), {})
        works[article] = works.get(article, False) or _cell(binding, "screen") == "true"
    return out


def sources(ids, media, cache=None, excluded=None):
    """`tmdbId -> {article: is a screen work}` for one batch of one media type, from disk where the same
    batch was asked before. What lets the source-work fallback tell a novel from a remake's original."""
    if not ids:
        return {}
    return _asked_batch(ids, lambda batch: source_query(batch, media, excluded), "sparql-source",
                        parse_sources, cache, qlever=False)


def target_query(ids, media, excluded=None, languages=()):
    """The SPARQL that names one batch of TMDB ids: the item's label and its publication dates (P577),
    plus its start time (P580) for a series, whose first air date is what a series' year means.

    Also the item's other names: its title (P1476, which editors fill in the work's own language and
    sometimes in translations) and its labels in `languages`, the languages of the articles the batch was
    grounded on. A German article about a film Wikidata labels "Room 205" in English is headed
    "Kollegiet"; these are what say that both names are the same work."""
    values = " ".join(f'"{tmdb_id}"' for tmdb_id in sorted(set(int(i) for i in ids)))
    start = "  OPTIONAL { ?film wdt:P580 ?start . }\n" if media == "tv" else ""
    wanted = sorted(set(languages) - {"en"})
    local = ("  OPTIONAL { ?film rdfs:label ?local . BIND(LANG(?local) AS ?localLang)\n"
             f"             FILTER(?localLang IN ({', '.join(json.dumps(code) for code in wanted)})) }}\n"
             if wanted else "")
    return (f"SELECT ?tmdb ?filmLabel ?released ?start ?original ?local ?localLang WHERE {{\n"
            f"  VALUES ?tmdb {{ {values} }}\n"
            f"  ?film wdt:{ID_PROPERTY[media]} ?tmdb .\n"
            f"{exclusion(ids, excluded)}"
            f"  OPTIONAL {{ ?film wdt:P577 ?released . }}\n"
            f"{start}"
            f"  OPTIONAL {{ ?film wdt:P1476 ?original . }}\n"
            f"{local}"
            f'  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,mul". }}\n'
            f"}}\n"
            f"ORDER BY ?tmdb ?filmLabel")


_DATE_YEAR = re.compile(r"^\+?([0-9]{1,4})-")


def parse_targets(payload):
    """`tmdbId -> {"title", "year", "originals", "labels"}` for one SPARQL body. RAISES on a body that is
    not a SPARQL result.

    The title is the first label in the query's order; a bare Q-id is the label service finding none, and
    is None rather than a name. The year is the EARLIEST — a festival premiere before a wide release, the
    original before a restoration — and a series' start time wins over any publication date. An id with
    neither is present with both None: Wikidata answered, and said nothing. `originals` is every P1476
    title, sorted; `labels` is `{language: label}` for the languages the query asked for.
    """
    try:
        bindings = json.loads(payload.decode("utf-8"))["results"]["bindings"]
        if not isinstance(bindings, list):
            raise TypeError(bindings)
    except (ValueError, KeyError, TypeError):
        raise WikidataError(f"not a SPARQL result: {payload[:200]!r}") from None
    seen = {}
    for binding in bindings:
        raw = _cell(binding, "tmdb")
        if raw is None or not _INTEGER.fullmatch(raw):
            continue
        entry = seen.setdefault(int(raw), {"title": None, "released": set(), "start": set(),
                                           "originals": set(), "labels": {}})
        label = _cell(binding, "filmLabel")
        if entry["title"] is None and label and not QID.match(label):
            entry["title"] = label
        for name in ("released", "start"):
            found = _DATE_YEAR.match(_cell(binding, name) or "")
            if found:
                entry[name].add(int(found.group(1)))
        if original := _cell(binding, "original"):
            entry["originals"].add(original)
        local, language = _cell(binding, "local"), _cell(binding, "localLang")
        if local and language:
            entry["labels"][language] = local
    return {tmdb_id: {"title": entry["title"],
                      "year": min(entry["start"] or entry["released"], default=None),
                      "originals": sorted(entry["originals"]), "labels": entry["labels"]}
            for tmdb_id, entry in seen.items()}


def targets(ids, media, cache=None, excluded=None, languages=()):
    """`tmdbId -> {"title", "year", "originals", "labels"}` for one batch of one media type, from disk where
    the same batch was asked before. What names a title to anything that asks whether an article is about
    it — Wikidata's, CC0, rather than TMDB's."""
    if not ids:
        return {}
    return _asked_batch(ids, lambda batch: target_query(batch, media, excluded, languages), "sparql-target",
                        parse_targets, cache, qlever=False)


#: Ids per claimant lookup and Q-ids per evidence lookup. Their own sizes, not a stage's: `resolve` is asked
#: for a stage's whole id list up front, and membership is part of the cache key.
CLAIM_BATCH = 200
EVIDENCE_BATCH = 100


def claimant_query(ids, media):
    """Every item that states each TMDB id — usually one, sometimes two."""
    values = " ".join(f'"{tmdb_id}"' for tmdb_id in sorted(set(int(i) for i in ids)))
    return (f"SELECT ?tmdb ?film WHERE {{\n"
            f"  VALUES ?tmdb {{ {values} }}\n"
            f"  ?film wdt:{ID_PROPERTY[media]} ?tmdb .\n"
            f"}}\n"
            f"ORDER BY ?tmdb ?film")


def _item(uri):
    qid = uri.rstrip("/").rsplit("/", 1)[-1]
    return qid if QID.match(qid) else None


def _bindings(payload):
    try:
        bindings = json.loads(payload.decode("utf-8"))["results"]["bindings"]
        if not isinstance(bindings, list):
            raise TypeError(bindings)
    except (ValueError, KeyError, TypeError):
        raise WikidataError(f"not a SPARQL result: {payload[:200]!r}") from None
    return bindings


def parse_claimants(payload):
    """`tmdbId -> [Q-id]`, sorted, so the order WDQS returns rows in decides nothing."""
    out = {}
    for binding in _bindings(payload):
        raw, film = _cell(binding, "tmdb"), _cell(binding, "film")
        qid = _item(film) if film else None
        if raw is None or not _INTEGER.fullmatch(raw) or qid is None:
            continue
        out.setdefault(int(raw), set()).add(qid)
    return {tmdb_id: sorted(qids, key=lambda q: (len(q), q)) for tmdb_id, qids in out.items()}


def evidence_query(qids, media):
    """What tells two claimants of one TMDB id apart: every TMDB id of this media each item states, and its
    English Wikipedia article. A UNION, so the multi-valued properties concatenate rather than multiply."""
    values = " ".join(f"wd:{qid}" for qid in sorted(set(qids)))
    return (f"SELECT ?film ?claim ?article WHERE {{\n"
            f"  VALUES ?film {{ {values} }}\n"
            f"  {{ ?film wdt:{ID_PROPERTY[media]} ?claim . }}\n"
            f"  UNION {{ ?article schema:about ?film ; schema:isPartOf <https://en.wikipedia.org/> . }}\n"
            f"}}\n"
            f"ORDER BY ?film")


def parse_evidence(payload):
    """`Q-id -> {"claims": [tmdbId], "articles": [title]}`, every list sorted."""
    out = {}
    for binding in _bindings(payload):
        film = _cell(binding, "film")
        qid = _item(film) if film else None
        if qid is None:
            continue
        entry = out.setdefault(qid, {"claims": set(), "articles": set()})
        claim, article = _cell(binding, "claim"), _cell(binding, "article")
        if claim and _INTEGER.fullmatch(claim):
            entry["claims"].add(int(claim))
        if article and article_title(article):
            entry["articles"].add(article_title(article))
    return {qid: {name: sorted(values) for name, values in entry.items()} for qid, entry in out.items()}


def _asked(query, namespace, parse, cache, extra=None, qlever=True):
    """One query: from disk where its WDQS text was kept, else live through `sparql` (WDQS, then QLever
    unless `qlever` is off). The body is kept under the WDQS text whichever endpoint answered, and only
    once it parses."""
    key = cache.key(namespace, {"q": query, **(extra or {})}) if cache is not None else None
    if key is not None:
        hit = cache.read(key)
        if hit is not None:
            try:
                return parse(hit)
            except WikidataError:
                pass
    payload = sparql(query, fallback=qlever)
    parsed = parse(payload)
    if key is not None:
        cache.write(key, payload)
    return parsed


#: A batch is split no further than this many ids. Past it a timeout is the answer about those ids.
SPLIT_FLOOR = 10


def _splittable(failure):
    """A timeout (status 0), a request timeout or a 5xx — WDQS answers a query that ran out of time with a
    500 — or QLever's own timeout, which it sends as a 429 saying `Operation timed out`. Not any other 429:
    that is a throttle, and more requests would only deepen it. Not any other 4xx: the server's answer about
    the query, which a smaller batch does not change."""
    return (failure.status == 0 or failure.status == 408 or 500 <= failure.status <= 599
            or (failure.status == 429 and b"timed out" in (failure.body or b"")))


#: Most TMDB ids one per-title query is given. `language_query` on the 200 most-linked films runs past WDQS's
#: 60 s every time and answers in 0.4-0.8 s at 175 and below (a cliff, not a slope: the plan flips); the daily
#: job's catalogue pass hands `enrich` 500 of them at once (`pipeline/fetch.BATCH`). 150 is `enrich.LIMIT`,
#: the size every cached body was asked at, so a batch of that size or less keeps its query text and key.
TMDB_CHUNK = 150


def _asked_batch(ids, build, namespace, parse, cache, extra=None, qlever=True, chunk=TMDB_CHUNK):
    """`_asked` for a batch of ids whose answer is a dict keyed by id. More than `chunk` ids are asked in
    sorted chunks of that size and merged. A batch that fails for want of time is asked again as two
    halves, merged — the answers are keyed per id, so they do not overlap — down to `SPLIT_FLOOR` ids. The
    whole batch's query text stays the cache key when it answers; a halved batch is kept as its halves,
    each under its own text. The first half to fail ends the attempt.

    `qlever` is off for the queries QLever was measured to time out on at every batch size (its server limit
    is 30 s): asking it only adds two minutes of retries before the failure."""
    if chunk and len(set(ids)) > chunk:
        ordered, out = sorted(set(ids)), {}
        for start in range(0, len(ordered), chunk):
            out.update(_asked_batch(ordered[start:start + chunk], build, namespace, parse, cache, extra, qlever,
                                    chunk))
        return out
    try:
        return _asked(build(ids), namespace, parse, cache, extra, qlever)
    except http.HTTPError as failure:
        if len(set(ids)) <= SPLIT_FLOOR or not _splittable(failure):
            raise
        ordered = sorted(set(ids))
        middle = len(ordered) // 2
        print(f"wikidata: {len(ordered)} ids failed ({failure}); asking {middle} and {len(ordered) - middle}",
              file=sys.stderr)
    out = _asked_batch(ordered[:middle], build, namespace, parse, cache, extra, qlever, chunk)
    out.update(_asked_batch(ordered[middle:], build, namespace, parse, cache, extra, qlever, chunk))
    return out


def claimants(ids, media, cache=None):
    """`tmdbId -> [Q-id]` for every id at least one item states, from disk where a batch was asked before."""
    ordered = sorted(set(int(i) for i in ids))
    out = {}
    for answer in in_parallel(
            lambda batch: _asked_batch(batch, lambda part: claimant_query(part, media), "sparql-claimant",
                                       parse_claimants, cache, chunk=None),
            [ordered[start:start + CLAIM_BATCH] for start in range(0, len(ordered), CLAIM_BATCH)]):
        out.update(answer)
    return out


def item_evidence(qids, media, cache=None):
    """`Q-id -> evidence` (`parse_evidence`) for the claimants of a contested id."""
    ordered = sorted(set(qids))
    out = {}
    for answer in in_parallel(
            lambda batch: _asked_batch(batch, lambda part: evidence_query(part, media), "sparql-evidence",
                                       parse_evidence, cache, chunk=None),
            [ordered[start:start + EVIDENCE_BATCH] for start in range(0, len(ordered), EVIDENCE_BATCH)]):
        out.update(answer)
    return out


#: The order the evidence is weighed in. Each rule NARROWS the claimants to those it holds for, where it
#: holds for any; the first that leaves one decides. `sole-claim`: the item states no other TMDB id — an item
#: carrying two is usually one work with a second work's id pasted on. `article`: the item has an English
#: Wikipedia article — the other is most often a stub, a season or a part of the work (`FLCL, season 1`,
#: `Olympia Part One`, an unlabelled duplicate).
#:
#: Two rules came first once, `imdb` and `year`: the item whose P345 or date matched the IMDb id and year
#: TMDB's detail record names. They were TMDB's facts deciding what a published row says, so they are gone
#: (oxyc/den-dataset#53). Over the 81 contested titles in the enriched corpus no committed decision covered,
#: the two rules here pick the same item for 44; for the other 37 (29 left ambiguous, 8 choosing the other
#: item) the choice the old rules made is committed in `DECISIONS`.
RULES = ("sole-claim", "article")


def choose(candidates, evidence):
    """`(Q-id, rule)`: the one claimant the evidence singles out, or `(None, "ambiguous")`.

    `evidence` is `item_evidence`'s. The candidates are sorted first, so neither the order WDQS returned them
    in nor the order the rows arrived decides anything. Nothing that singles one out is an answer too: None,
    and the caller drops what it would otherwise have had to merge from two works.
    """
    pool = sorted(set(candidates), key=lambda q: (len(q), q))
    tests = {"sole-claim": lambda q: len(evidence.get(q, {}).get("claims", ())) == 1,
             "article": lambda q: bool(evidence.get(q, {}).get("articles"))}
    if len(pool) == 1:
        return pool[0], None
    for rule in RULES:
        narrowed = [q for q in pool if tests[rule](q)]
        if narrowed:
            pool = narrowed
        if len(pool) == 1:
            return pool[0], rule
    return None, "ambiguous"


#: A person's choice for the contested titles no rule decides, read before the rules.
DECISIONS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data",
                         "wikidata-item-decisions.json")


class DecisionError(RuntimeError):
    """A committed decision that no longer fits Wikidata: its id is not contested any more, or its item no
    longer claims the id. Refused rather than applied or ignored — either would keep a judgement nobody
    re-made alive, the way a stale alias decision would."""


def load_decisions(path=DECISIONS):
    """`(media, tmdbId) -> Q-id` from the committed decisions file; `{}` when there is none."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        rows = json.load(handle).get("decisions") or []
    return {(row["mediaType"], int(row["tmdbId"])): row["item"] for row in rows}


def resolve(ids, media, cache=None, decisions=None):
    """`tmdbId -> {"item", "candidates", "rule"}` for every id an item states: which ONE item every
    per-title query answers from.

    An uncontested id is `{"item": Q}`. A contested one also carries its sorted `candidates` and the `rule`
    that chose: `decision` when `data/wikidata-item-decisions.json` names the item, else the first of
    `RULES` that singles one out, else `ambiguous` with `item` None. `decisions` is `load_decisions()`'s
    map, read from the committed file when not given. Raises `DecisionError` for a decision about an id
    among `ids` that it no longer fits.
    """
    decisions = load_decisions() if decisions is None else decisions
    claimed = {tmdb_id: sorted(set(qids), key=lambda q: (len(q), q))
               for tmdb_id, qids in claimants(ids, media, cache).items() if qids}
    contested = {tmdb_id: qids for tmdb_id, qids in claimed.items() if len(qids) > 1}
    for tmdb_id in sorted(set(int(i) for i in ids)):
        decided = decisions.get((media, tmdb_id))
        if decided is None:
            continue
        if tmdb_id not in contested:
            raise DecisionError(f"{DECISIONS} decides {media}:{tmdb_id}, which only "
                                f"{claimed.get(tmdb_id) or 'no item'} now claims. Remove the stale entry.")
        if decided not in contested[tmdb_id]:
            raise DecisionError(f"{DECISIONS} chooses {decided} for {media}:{tmdb_id}, which is no longer one "
                                f"of its claimants {contested[tmdb_id]}. Decide it again.")
    undecided = {tmdb_id: qids for tmdb_id, qids in contested.items() if (media, tmdb_id) not in decisions}
    evidence = item_evidence([q for qids in undecided.values() for q in qids], media, cache) if undecided else {}
    out = {}
    for tmdb_id, qids in claimed.items():
        if tmdb_id not in contested:
            out[tmdb_id] = {"item": qids[0]}
        elif tmdb_id not in undecided:
            out[tmdb_id] = {"item": decisions[(media, tmdb_id)], "candidates": qids, "rule": "decision"}
        else:
            chosen, rule = choose(qids, evidence)
            out[tmdb_id] = {"item": chosen, "candidates": qids, "rule": rule}
    return out


def set_aside(resolution):
    """`tmdbId -> [Q-id]`: the claimants every query must leave out (`exclusion`) — all of them where
    nothing chose one, so an ambiguous title answers nothing rather than two works at once."""
    return {tmdb_id: [q for q in found["candidates"] if q != found["item"]]
            for tmdb_id, found in resolution.items() if found.get("candidates")}


def provenance(found):
    """What a row records about a contested title: the item chosen (absent when none was) and every item
    that claimed its TMDB id. An uncontested title records nothing, so its row is unchanged."""
    if not found or not found.get("candidates"):
        return {}
    out = {"wikidataCandidates": list(found["candidates"])}
    if found.get("item"):
        out["wikidataItem"] = found["item"]
    return out


def cache_for(env=None):
    """Wikidata shares the `wiki` namespace with Wikipedia — one cache to age out, one to clear."""
    return caching.wiki(env)
