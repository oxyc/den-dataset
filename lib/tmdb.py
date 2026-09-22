#!/usr/bin/env python3
"""TMDB's `/discover`, which the worklist enumerates a universe from — and the detail record the enrichment
reads each title through (`title_record`), from the ~60k bodies the Swift enrichment already cached.

Small on purpose. The app's client is a different thing with different needs.

Two rules are load-bearing and neither is obvious from the endpoint:

  * **`results` is required.** Defaulting it to an empty list turns every unexpected shape — an auth error
    body, a schema change, a maintenance page — into a valid empty page. The worklist's paging loop then
    stops after page one and the delta pass reports "0 new titles" instead of failing, silently, daily.
  * **`/discover` is never cached.** Its whole job is to surface titles that are new or have newly crossed
    the vote floor, so serving it from disk hides exactly what it was asked for. `lib/cache.py`'s
    `tmdb_is_cacheable` is where that lives, because the same rule decides what a detail call may keep.
"""
import json
import os

from . import cache as caching
from . import http

HOST = "api.themoviedb.org"
BASE = "/3"

#: `/discover` pages 20 results each and serves at most 500 pages, so one query sees 10,000 titles at most.
MAX_PAGES = 500


class TMDBError(RuntimeError):
    pass


def api_key(env=None):
    """The key, or a refusal. Read at call time and sent only as a query parameter TMDB requires there;
    `lib/cache.py` strips it before anything is hashed, so it never reaches a filename."""
    env = os.environ if env is None else env
    key = env.get("TMDB_API_KEY")
    if not key:
        raise TMDBError("set TMDB_API_KEY (TMDB's own API requires it)")
    return key


def discover_params(media, vote_count_gte=None, release_date_gte=None, sort_by="popularity.desc",
                    include_adult=False):
    """The `/discover/{movie,tv}` parameters, without `api_key` or `page`.

    The date field is named for the media: TMDB calls it `primary_release_date` for a film and
    `first_air_date` for a series, and sending the wrong one is not an error — it is an unfiltered query
    that looks like a filtered one.
    """
    params = {"sort_by": sort_by, "include_adult": "true" if include_adult else "false"}
    if vote_count_gte is not None:
        params["vote_count.gte"] = str(vote_count_gte)
    if release_date_gte:
        date_key = "first_air_date" if media == "tv" else "primary_release_date"
        params[f"{date_key}.gte"] = release_date_gte
    return params


def is_title_record(body, expecting_appended):
    """True when a body looks like a real title record and is therefore safe to keep.

    Deliberately structural rather than a decode. Every field of the detail record is optional, so `{}`
    and TMDB's own `{"success":false,"status_code":34}` both parse cleanly; what a partial 200 under load
    lacks is a numeric `id`, a name, or — when the request asked for sub-resources — those resources. A
    response missing them yields a title with no keywords, no director and no cast, which is
    indistinguishable downstream from a title that genuinely has none. Anything cached is served for the
    whole TTL, so one bad minute becomes weeks of wrong answers.
    """
    if not isinstance(body, dict) or not isinstance(body.get("id"), int):
        return False
    if not (body.get("title") or body.get("name") or "").strip():
        return False
    # PRESENCE, not contents: a title with an empty keyword list is a fact about the title, while a
    # response that carries no `keywords` key at all is a detail call whose sub-resources did not arrive.
    if expecting_appended and (body.get("keywords") is None or body.get("credits") is None):
        return False
    return True


#: Detail, keywords and credits in ONE call. The value is part of the detail record's cache key, so it is
#: spelled the way the Swift enrichment spelled it: ~60k records are on disk under exactly this. The record
#: no longer carries a keyword or a credit (see `title_record`), but asking for less would miss every one
#: of those bodies and fetch the corpus again.
APPEND = "keywords,credits"


def _field(body, name, kind):
    """`body[name]`, None when absent or null, and a refusal when it is there as the wrong type.

    Strict because the Swift decoder was: a record whose `genres` is not a list of `{id, name}` failed to
    decode and was dropped as a dead id, not enriched with half its fields. `bool` is not an `int` here,
    though Python says it is.
    """
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise ValueError(f"TMDB `{name}` is {type(value).__name__}, not {kind.__name__}")
    return value


def _named(items, name, also=()):
    """A list of objects each carrying a string `name` (and ints/strings named in `also`)."""
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get(name), str):
            raise ValueError(f"TMDB list item without a string `{name}`: {item!r}"[:200])
        for extra, kind in also:
            if not isinstance(item.get(extra), kind) or isinstance(item.get(extra), bool):
                raise ValueError(f"TMDB list item without `{extra}`: {item!r}"[:200])
    return items


def title_record(body, tmdb_id, media):
    """The detail body as the enriched record's TMDB half. Raises ValueError on a body that is not one.

    THE OVERVIEW STOPS HERE, LENGTH AND ALL. TMDB's terms (§1.C) speak directly to using their content
    with a machine-learning application, and this record feeds a classifier and an embedder. The text was
    dropped at this boundary first and its character count kept, for one reader: a stub check that refused
    a title whose overview ran under 20 characters. That check is gone (oxyc/den-dataset#53) — `overview`
    downstream holds a Wikipedia plot or nothing, so the length said nothing about what a title would be
    grounded on, and over the whole repass it refused 3 titles out of 59,209 — so nothing crosses now.

    ONLY WHAT A READER NEEDS crosses (oxyc/den-dataset#53). `originCountry` picks the admission tier
    (`pipeline/floors.py`) and `voteCount` is the gate's count for an export row; `genreIDs` is where
    `./den genres-moods` takes a new title's `animated` flag from (genre 16). `originalLanguage` is still
    written. `title`, `year`, `genres`, `keywords`, `keywordIDs`, `director` and `topCast` were written for
    readers that are gone: the Swift batch decoder that required `title`, the classify dump that now names
    a title by Wikidata's label and year (`pipeline/articles.targets`), and the premise ruler's candidate
    miner (`scripts/v2/premise_mine_candidates.py`), which reads `keywordIDs` off the `out-t02` batches
    already on disk and is not run on new ones.
    """
    if not isinstance(body, dict):
        raise ValueError("TMDB detail body is not an object")
    countries = _field(body, "origin_country", list)
    if countries is None:
        produced = _named(_field(body, "production_countries", list) or [], "iso_3166_1")
        countries = [country["iso_3166_1"] for country in produced]
    genres = _named(_field(body, "genres", list) or [], "name", (("id", int),))
    # `overview` is read and DISCARDED: reading it still type-checks the field, so a body whose overview is
    # not a string is refused here rather than decoded into half a record.
    _field(body, "overview", str)
    return {
        "tmdbId": tmdb_id, "mediaType": media,
        "genreIDs": [genre["id"] for genre in genres],
        "originCountry": countries, "originalLanguage": _field(body, "original_language", str),
        "voteCount": _field(body, "vote_count", int) or 0,
    }


class TMDB:
    """A thin client over `lib/http`, with the detail cache `lib/cache` describes."""

    def __init__(self, key=None, cache=None):
        self._key = key or api_key()
        self.cache = caching.tmdb() if cache is None else cache

    def get(self, path, params=None):
        """One TMDB call, served from disk where the policy allows it."""
        params = dict(params or {})
        key = None
        if self.cache is not None and caching.tmdb_is_cacheable(path):
            key = self.cache.key(path, params)
            hit = self.cache.read(key)
            if hit is not None:
                return json.loads(hit.decode("utf-8"))
        payload = http.request(HOST, BASE + path, dict(params, api_key=self._key))
        body = json.loads(payload.decode("utf-8"))
        if key is not None and is_title_record(body, "append_to_response" in params):
            self.cache.write(key, payload)
        return body

    def discover(self, media, params, page=1):
        """One page of `/discover/{media}` as `(rows, page, total_pages)`.

        `results` missing is a refusal rather than an empty page — see the module docstring.
        """
        body = self.get(f"/discover/{media}", dict(params, page=str(page)))
        rows = body.get("results")
        if not isinstance(rows, list):
            raise TMDBError(
                f"/discover/{media} page {page} carries no `results` list. That is an error body, not an "
                f"empty page — treating it as one stops the paging loop and reports a universe of "
                f"whatever was collected so far: {json.dumps(body)[:200]}")
        return rows, int(body.get("page") or 1), int(body.get("total_pages") or 1)
