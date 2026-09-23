#!/usr/bin/env python3
"""TMDB's `/discover`, which the worklist enumerates a universe from. Nothing else in the pipeline asks TMDB
anything: the per-title detail call the enrichment made is gone (oxyc/den-dataset#53), and with it the
detail cache.

Small on purpose. The app's client is a different thing with different needs.

Two rules are load-bearing and neither is obvious from the endpoint:

  * **`results` is required.** Defaulting it to an empty list turns every unexpected shape — an auth error
    body, a schema change, a maintenance page — into a valid empty page. The worklist's paging loop then
    stops after page one and the delta pass reports "0 new titles" instead of failing, silently, daily.
  * **`/discover` is never cached.** Its whole job is to surface titles that are new or have newly crossed
    the vote floor, so serving it from disk hides exactly what it was asked for.
"""
import json
import os

from . import http

HOST = "api.themoviedb.org"
BASE = "/3"

#: `/discover` pages 20 results each and serves at most 500 pages, so one query sees 10,000 titles at most.
MAX_PAGES = 500


class TMDBError(RuntimeError):
    pass


def api_key(env=None):
    """The key, or a refusal. Read at call time and sent only as a query parameter TMDB requires there."""
    env = os.environ if env is None else env
    key = env.get("TMDB_API_KEY")
    if not key:
        raise TMDBError("set TMDB_API_KEY (TMDB's own API requires it)")
    return key


def discover_params(media, vote_count_gte=None, release_date_gte=None, sort_by="popularity.desc",
                    include_adult=False, origin_countries=None):
    """The `/discover/{movie,tv}` parameters, without `api_key` or `page`.

    The date field is named for the media: TMDB calls it `primary_release_date` for a film and
    `first_air_date` for a series, and sending the wrong one is not an error — it is an unfiltered query
    that looks like a filtered one. `origin_countries` are OR-ed: TMDB joins `|` as "any of".
    """
    params = {"sort_by": sort_by, "include_adult": "true" if include_adult else "false"}
    if vote_count_gte is not None:
        params["vote_count.gte"] = str(vote_count_gte)
    if release_date_gte:
        date_key = "first_air_date" if media == "tv" else "primary_release_date"
        params[f"{date_key}.gte"] = release_date_gte
    if origin_countries:
        params["with_origin_country"] = "|".join(sorted(origin_countries))
    return params


class TMDB:
    """A thin client over `lib/http`."""

    def __init__(self, key=None):
        self._key = key or api_key()

    def get(self, path, params=None):
        """One TMDB call."""
        payload = http.request(HOST, BASE + path, dict(params or {}, api_key=self._key))
        return json.loads(payload.decode("utf-8"))

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
