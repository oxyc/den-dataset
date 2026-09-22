#!/usr/bin/env python3
"""IMDb's vote counts, from the public daily `title.ratings.tsv.gz` dump — for the admission gate only.

**The licence decides where these numbers may go.** IMDb grants a personal, non-commercial,
NON-TRANSFERABLE licence to its datasets (https://www.imdb.com/conditions), so a count read here may
decide whether a title is enriched, and may never be written into anything that ships: the store, the
release, the corpus, a batch file. It lives in two places and only two — this process's memory, and the
dump itself under the local cache root (`<cache root>/imdb/`, `.cache/imdb/` by default), which is never
in an out-dir and never uploaded. `scripts/v2/build_store.py` declares `imdb` a vendor source, so a store
column sourced from here is refused like a TMDB one.

**Fetched with a conditional GET.** The dump is ~8 MB and changes daily; the `ETag` and `Last-Modified`
of the copy on disk go back to the server, and a 304 costs one round trip. It is a mirror refreshed on
use, not a response cache with a TTL, so `DEN_CACHE=0` does not switch it off: there is no other copy to
fall back to.

**Kept only above a minimum.** The whole dump is ~1.6M rows; the gate asks one question of it — does this
title clear a floor — so rows below the lowest floor are dropped as they are read, and a title that is
absent here is below every floor. That keeps a multi-hour drain from holding a quarter-gigabyte dict.
"""
import gzip
import io
import json
import os
import time

from . import cache as caching
from . import http

HOST = "datasets.imdbws.com"
PATH = "/title.ratings.tsv.gz"
FILENAME = "title.ratings.tsv.gz"
#: The dump's own header. A different one is a schema change, and reading it by position would turn a
#: moved column into every title's vote count.
HEADER = ("tconst", "averageRating", "numVotes")


class Unavailable(RuntimeError):
    """No usable dump: the download failed and there is no earlier copy on disk to read instead."""


def directory(env=None):
    return os.path.join(caching.root(env), "imdb")


def _meta_path(where):
    return os.path.join(where, FILENAME + ".meta.json")


def _read_meta(where):
    try:
        with open(_meta_path(where), encoding="utf-8") as handle:
            meta = json.load(handle)
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}


def refresh(env=None, request=None):
    """The dump's path on disk, refreshed if the server has a newer one, and a note saying which.

    `(path, note)`. The note is `"downloaded"`, `"unchanged"`, or — when the server could not be reached
    and an earlier copy exists — `"stale: …"` naming the error and the copy's age. A vote count only climbs,
    so an old dump under-admits and never over-admits; the caller says so rather than refusing.
    Raises `Unavailable` when there is nothing on disk to read.
    """
    request = request or http.request
    where = directory(env)
    path = os.path.join(where, FILENAME)
    have = os.path.exists(path)
    meta = _read_meta(where) if have else {}
    headers = {"Accept": "*/*"}
    if have and meta.get("etag"):
        headers["If-None-Match"] = meta["etag"]
    if have and meta.get("last-modified"):
        headers["If-Modified-Since"] = meta["last-modified"]
    received = {}
    try:
        body = request(HOST, PATH, headers=headers, received=received)
    except http.HTTPError as error:
        if not have:
            raise Unavailable(f"IMDb ratings dump could not be downloaded ({error}) and there is no earlier "
                              f"copy at {path}") from error
        age = (time.time() - os.path.getmtime(path)) / caching.DAY_SECONDS
        return path, f"stale: {error}; using the copy from {age:.1f} day(s) ago"
    if received.get("status") == 304:
        os.utime(path)
        return path, "unchanged"
    os.makedirs(where, exist_ok=True)
    caching.write_atomically(path, body)
    kept = {name: received.get(name) for name in ("etag", "last-modified") if received.get(name)}
    caching.write_atomically(_meta_path(where), json.dumps(kept, sort_keys=True).encode("utf-8"))
    return path, "downloaded"


def parse(path, minimum=0):
    """`tconst -> numVotes` for every row with at least `minimum` votes. Raises `Unavailable` on a file
    that is not the dump — a truncated gzip, an error page saved under its name, a changed header."""
    votes = {}
    try:
        with gzip.open(path, "rb") as raw:
            lines = io.TextIOWrapper(raw, encoding="utf-8")
            header = tuple(next(lines, "").rstrip("\n").split("\t"))
            if header != HEADER:
                raise Unavailable(f"{path} starts {header!r}, not the ratings dump's {HEADER!r}")
            for line in lines:
                tconst, _rating, count = line.rstrip("\n").split("\t")
                count = int(count)
                if count >= minimum:
                    votes[tconst] = count
    except (OSError, EOFError, ValueError) as error:
        raise Unavailable(f"{path} is not a readable ratings dump ({error})") from error
    if not votes and minimum == 0:
        raise Unavailable(f"{path} holds a header and no rows")
    return votes


class Ratings:
    """The counts one run judges by, and how fresh they are."""

    def __init__(self, votes, note):
        self.votes = votes
        self.note = note

    def get(self, imdb_id):
        """The title's vote count, or 0 when it is absent — absent is below every floor that was asked."""
        return self.votes.get(imdb_id, 0) if imdb_id else 0


_loaded = {}


def ratings(minimum=0, env=None, request=None):
    """The dump, refreshed once per process and kept above `minimum`. Raises `Unavailable`.

    Once per process because a drain runs one enrich batch after another in the same interpreter, and
    re-reading 1.6M rows per batch costs more than the batch's TMDB calls.
    """
    where = directory(env)
    if (where, minimum) not in _loaded:
        path, note = refresh(env, request)
        _loaded[(where, minimum)] = Ratings(parse(path, minimum), note)
    return _loaded[(where, minimum)]
