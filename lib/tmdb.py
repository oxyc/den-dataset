#!/usr/bin/env python3
"""The two TMDB endpoints this pipeline reads: `/discover` and a title's detail record.

Small on purpose. The app's client is a different thing with different needs; this one exists so the
worklist can enumerate a universe, and nothing else belongs here until the enrichment moves across.

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

#: `/discover` pages 20 results each and serves at most 500 pages — the ceiling the year partition exists
#: to page past.
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


def discover_params(media, vote_count_gte=None, release_date_gte=None, release_date_lte=None,
                    origin_country=(), sort_by="popularity.desc", include_adult=False):
    """The `/discover/{movie,tv}` parameters, without `api_key` or `page`.

    The date field is named for the media: TMDB calls it `primary_release_date` for a film and
    `first_air_date` for a series, and sending the wrong one is not an error — it is an unfiltered query
    that looks like a filtered one.
    """
    params = {"sort_by": sort_by, "include_adult": "true" if include_adult else "false"}
    if vote_count_gte is not None:
        params["vote_count.gte"] = str(vote_count_gte)
    if origin_country:
        # Within one TMDB parameter a pipe is OR and a comma is AND; origins are alternatives.
        params["with_origin_country"] = "|".join(origin_country)
    date_key = "first_air_date" if media == "tv" else "primary_release_date"
    if release_date_gte:
        params[f"{date_key}.gte"] = release_date_gte
    if release_date_lte:
        params[f"{date_key}.lte"] = release_date_lte
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
