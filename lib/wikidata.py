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
import json
import math
import re
import urllib.parse

from . import cache as caching
from . import http

HOST = "query.wikidata.org"
PATH = "/sparql"

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


def query_text(ids, media, prop):
    """The SPARQL this pipeline has always sent. Whitespace included — it is hashed into the cache key."""
    values = " ".join(f'"{tmdb_id}"' for tmdb_id in ids)
    return (f"SELECT ?tmdb ?vLabel WHERE {{\n"
            f"  VALUES ?tmdb {{ {values} }}\n"
            f"  ?film wdt:{ID_PROPERTY[media]} ?tmdb .\n"
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


def fetch_property(ids, media, prop, cache=None):
    """One property over one batch of ids, from disk where the same batch was asked before.

    A caller's resume already skips ids present in its output file, so the cache earns its keep on a
    different axis: the two properties are two requests over the same id batch, and a re-run scoped to a
    different id list still repeats whole batches whose membership happens to coincide.
    """
    query = query_text(ids, media, prop)
    key = None
    if cache is not None:
        key = cache.key("sparql-docfacts", {"q": query})
        hit = cache.read(key)
        if hit is not None:
            try:
                return parse_labelled(hit)
            except WikidataError:
                pass
    payload = http.request(HOST, PATH, {"format": "json"}, method="POST", body=query.encode("utf-8"),
                           headers={"Content-Type": "application/sparql-query",
                                    "Accept": "application/sparql-results+json"})
    parsed = parse_labelled(payload)
    # After a successful parse only — a WDQS maintenance page must not outlive the outage.
    if key is not None:
        cache.write(key, payload)
    return parsed


def doc_facts(ids, media, cache=None):
    """`tmdbId -> {"directors": [...], "genres": [...]}` for one batch.

    An id Wikidata states neither for is ABSENT from the result rather than present and empty: unknown is
    not "none", and the caller is the one that decides how to record that.
    """
    unique = sorted(set(int(value) for value in ids))
    if not unique:
        return {}
    directors = fetch_property(unique, media, DIRECTOR, cache)
    raw_genres = fetch_property(unique, media, GENRE, cache)
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


def mapping_query(ids, media, languages):
    """The SPARQL that maps one batch of TMDB ids to their articles, and the facts that ride along.

    `languages` are the other Wikipedias a plot may be read from. They are the query's business too: an
    unrestricted sitelink returns a row per language and multiplies the result set.
    """
    values = " ".join(f'"{tmdb_id}"' for tmdb_id in sorted(set(int(i) for i in ids)))
    wikis = " ".join(f"<https://{code}.wikipedia.org/>" for code in sorted(languages))
    return "\n".join(_MAPPING).replace("{values}", values).replace(
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


def mapping(ids, media, languages, cache=None):
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
    query = mapping_query(ids, media, languages)
    key = None
    if cache is not None:
        key = cache.key("sparql", {"q": query})
        hit = cache.read(key)
        if hit is not None:
            try:
                return parse_mapping(hit)
            except WikidataError:
                pass
    payload = http.request(HOST, PATH, {"format": "json"}, method="POST", body=query.encode("utf-8"),
                           headers={"Content-Type": "application/sparql-query",
                                    "Accept": "application/sparql-results+json"})
    parsed = parse_mapping(payload)
    if key is not None:
        cache.write(key, payload)
    return parsed


def imdb_query(ids, media):
    """The SPARQL that maps one batch of TMDB ids to their IMDb ids (P345), and nothing else.

    Separate from the mapping query because it is asked BEFORE admission, of the titles TMDB's count left
    below its floor: they need an IMDb id to be judged on IMDb's count, and none of the mapping's
    sitelinks until they are admitted.
    """
    values = " ".join(f'"{tmdb_id}"' for tmdb_id in sorted(set(int(i) for i in ids)))
    return (f"SELECT ?tmdb ?imdb WHERE {{\n"
            f"  VALUES ?tmdb {{ {values} }}\n"
            f"  ?film wdt:{ID_PROPERTY[media]} ?tmdb .\n"
            f"  ?film wdt:P345 ?imdb .\n"
            f"}}\n"
            f"ORDER BY ?tmdb ?imdb")


def parse_imdb(payload):
    """`tmdbId -> tt…` for one SPARQL body. RAISES on a body that is not a SPARQL result.

    P345 is multi-valued and not checked against IMDb's id space, so only a `tt` id counts, and the first in
    the query's order wins — the same one every run. An id with none is absent: the caller judges it on
    TMDB's count alone.
    """
    try:
        bindings = json.loads(payload.decode("utf-8"))["results"]["bindings"]
        if not isinstance(bindings, list):
            raise TypeError(bindings)
    except (ValueError, KeyError, TypeError):
        raise WikidataError(f"not a SPARQL result: {payload[:200]!r}") from None
    out = {}
    for binding in bindings:
        raw, imdb = _cell(binding, "tmdb"), _cell(binding, "imdb")
        if raw is None or not _INTEGER.fullmatch(raw) or not imdb or not imdb.startswith("tt"):
            continue
        out.setdefault(int(raw), imdb)
    return out


def imdb_ids(ids, media, cache=None):
    """`tmdbId -> tt…` for one batch of one media type, from disk where the same batch was asked before."""
    if not ids:
        return {}
    query = imdb_query(ids, media)
    key = None
    if cache is not None:
        key = cache.key("sparql-imdb", {"q": query})
        hit = cache.read(key)
        if hit is not None:
            try:
                return parse_imdb(hit)
            except WikidataError:
                pass
    payload = http.request(HOST, PATH, {"format": "json"}, method="POST", body=query.encode("utf-8"),
                           headers={"Content-Type": "application/sparql-query",
                                    "Accept": "application/sparql-results+json"})
    parsed = parse_imdb(payload)
    if key is not None:
        cache.write(key, payload)
    return parsed


def kind_query(ids, media):
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
            f"  {{ ?film wdt:{GENRE} ?v . }} UNION {{ ?film wdt:{INSTANCE_OF} ?v . }}\n"
            f'  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,mul". }}\n'
            f"}}\n"
            f"ORDER BY ?tmdb ?vLabel")


def kinds(ids, media, cache=None):
    """`tmdbId -> [label]` — what one batch of titles are, by genre and by type, from disk where the same
    batch was asked before. An id Wikidata states neither for is absent."""
    if not ids:
        return {}
    query = kind_query(ids, media)
    key = None
    if cache is not None:
        key = cache.key("sparql-kind", {"q": query})
        hit = cache.read(key)
        if hit is not None:
            try:
                return parse_labelled(hit)
            except WikidataError:
                pass
    payload = http.request(HOST, PATH, {"format": "json"}, method="POST", body=query.encode("utf-8"),
                           headers={"Content-Type": "application/sparql-query",
                                    "Accept": "application/sparql-results+json"})
    parsed = parse_labelled(payload)
    if key is not None:
        cache.write(key, payload)
    return parsed


def language_query(ids, media):
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


def languages(ids, media, cache=None):
    """`tmdbId -> [ISO 639-1 code]` for one batch of one media type, from disk where the same batch was
    asked before. What a title is IN, Wikidata's and CC0, rather than TMDB's `original_language`."""
    if not ids:
        return {}
    query = language_query(ids, media)
    key = None
    if cache is not None:
        key = cache.key("sparql-language", {"q": query})
        hit = cache.read(key)
        if hit is not None:
            try:
                return parse_languages(hit)
            except WikidataError:
                pass
    payload = http.request(HOST, PATH, {"format": "json"}, method="POST", body=query.encode("utf-8"),
                           headers={"Content-Type": "application/sparql-query",
                                    "Accept": "application/sparql-results+json"})
    parsed = parse_languages(payload)
    if key is not None:
        cache.write(key, payload)
    return parsed


#: What makes a P144 work a SCREEN work: an instance of film, television program or web series, or of any
#: subclass of them (`television series`, `anime film`, `miniseries`, `silent short film`). A class walk
#: rather than `lib/wikidata_facts.SOURCE_KIND_BY_TYPE`'s label list, which names `film` and `television
#: series` but folds `television program`, `miniseries`, `anime film` and `web series` into `other`.
SCREEN_CLASSES = ("Q11424", "Q15416", "Q526877")


def source_query(ids, media):
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


def sources(ids, media, cache=None):
    """`tmdbId -> {article: is a screen work}` for one batch of one media type, from disk where the same
    batch was asked before. What lets the source-work fallback tell a novel from a remake's original."""
    if not ids:
        return {}
    query = source_query(ids, media)
    key = None
    if cache is not None:
        key = cache.key("sparql-source", {"q": query})
        hit = cache.read(key)
        if hit is not None:
            try:
                return parse_sources(hit)
            except WikidataError:
                pass
    payload = http.request(HOST, PATH, {"format": "json"}, method="POST", body=query.encode("utf-8"),
                           headers={"Content-Type": "application/sparql-query",
                                    "Accept": "application/sparql-results+json"})
    parsed = parse_sources(payload)
    if key is not None:
        cache.write(key, payload)
    return parsed


def target_query(ids, media):
    """The SPARQL that names one batch of TMDB ids: the item's label and its publication dates (P577),
    plus its start time (P580) for a series, whose first air date is what a series' year means."""
    values = " ".join(f'"{tmdb_id}"' for tmdb_id in sorted(set(int(i) for i in ids)))
    start = "  OPTIONAL { ?film wdt:P580 ?start . }\n" if media == "tv" else ""
    return (f"SELECT ?tmdb ?filmLabel ?released ?start WHERE {{\n"
            f"  VALUES ?tmdb {{ {values} }}\n"
            f"  ?film wdt:{ID_PROPERTY[media]} ?tmdb .\n"
            f"  OPTIONAL {{ ?film wdt:P577 ?released . }}\n"
            f"{start}"
            f'  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,mul". }}\n'
            f"}}\n"
            f"ORDER BY ?tmdb ?filmLabel")


_DATE_YEAR = re.compile(r"^\+?([0-9]{1,4})-")


def parse_targets(payload):
    """`tmdbId -> {"title", "year"}` for one SPARQL body. RAISES on a body that is not a SPARQL result.

    The title is the first label in the query's order; a bare Q-id is the label service finding none, and
    is None rather than a name. The year is the EARLIEST — a festival premiere before a wide release, the
    original before a restoration — and a series' start time wins over any publication date. An id with
    neither is present with both None: Wikidata answered, and said nothing.
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
        entry = seen.setdefault(int(raw), {"title": None, "released": set(), "start": set()})
        label = _cell(binding, "filmLabel")
        if entry["title"] is None and label and not QID.match(label):
            entry["title"] = label
        for name in ("released", "start"):
            found = _DATE_YEAR.match(_cell(binding, name) or "")
            if found:
                entry[name].add(int(found.group(1)))
    return {tmdb_id: {"title": entry["title"],
                      "year": min(entry["start"] or entry["released"], default=None)}
            for tmdb_id, entry in seen.items()}


def targets(ids, media, cache=None):
    """`tmdbId -> {"title", "year"}` for one batch of one media type, from disk where the same batch was
    asked before. What names a title to anything that asks whether an article is about it — Wikidata's,
    CC0, rather than TMDB's."""
    if not ids:
        return {}
    query = target_query(ids, media)
    key = None
    if cache is not None:
        key = cache.key("sparql-target", {"q": query})
        hit = cache.read(key)
        if hit is not None:
            try:
                return parse_targets(hit)
            except WikidataError:
                pass
    payload = http.request(HOST, PATH, {"format": "json"}, method="POST", body=query.encode("utf-8"),
                           headers={"Content-Type": "application/sparql-query",
                                    "Accept": "application/sparql-results+json"})
    parsed = parse_targets(payload)
    if key is not None:
        cache.write(key, payload)
    return parsed


def cache_for(env=None):
    """Wikidata shares the `wiki` namespace with Wikipedia — one cache to age out, one to clear."""
    return caching.wiki(env)
