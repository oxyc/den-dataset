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
import re

from . import cache as caching
from . import http

HOST = "query.wikidata.org"
PATH = "/sparql"

#: The TMDB-id property per media. A film and a series are different statements, and asking the wrong one
#: returns nothing rather than erroring.
ID_PROPERTY = {"movie": "P4947", "tv": "P4983"}

DIRECTOR, GENRE = "P57", "P136"

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


def cache_for(env=None):
    """Wikidata shares the `wiki` namespace with Wikipedia — one cache to age out, one to clear."""
    return caching.wiki(env)
