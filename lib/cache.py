#!/usr/bin/env python3
"""The on-disk cache of upstream responses — and the key derivation, which is a CONTRACT.

The pipeline re-reads the whole corpus often. A full re-enrich is ~80k Wikipedia requests and a few
thousand Wikidata queries, and the run that motivated the cache spent most of its ~10h re-fetching material that
had not changed. WHAT is safe to keep differs per source, so each caller decides that; this module only
knows how to store a body under a key, honestly and atomically.

## The key is fixed, and this is the only definition of it

    SHA256("<namespace>\\x01<path>?<query, sorted by key>")

filed at `<root>/<namespace>/<first two hex characters>/<full hex>.json`. It was derived by
`Sources/DenDataset/ResponseCache.swift`, and it is reproduced here rather than improved for two reasons
that are not style:

  * there are ~2.1 GB of bodies under `.cache/wiki` written by the Swift passes. A key that hashes
    differently does not miss loudly — it re-fetches the whole corpus and looks like a slow first run.
  * `scripts/backfill-plot-provenance.py` reconstructs the same key to replay grounding decisions out of
    those bodies, and recovered 47,529 of 47,529 rows with it. A changed derivation makes that tool report
    every row unrecoverable rather than erroring.

Changing it is a migration, not a port detail.

The `path` a Wikipedia caller passes INCLUDES THE HOST. Every Wikipedia serves `/w/api.php`, so leaving
the host out was a silent correctness bug rather than a missed hit: a request for the Italian
"Iago (film)" had the same path and query as the English one and was handed the ENGLISH body, and the
multilingual fallback then saw an article with no plot section and gave up.

## The credential never touches the disk

Keys are built with the credential parameters REMOVED, so no filename and nothing written can carry one.
A cache directory is exactly the kind of place a secret gets copied into and then forgotten, which is why
this is enforced here rather than left to each call site to remember.
"""
import hashlib
import os
import tempfile
import time

#: Query parameters that must never reach a key, and therefore never a filename.
CREDENTIAL_KEYS = frozenset(("api_key", "token", "access_token", "key", "apikey"))

#: Values that switch a cache off. `DEN_CACHE=0` disables everything; `<NAMESPACE>_CACHE=0` one source.
OFF = frozenset(("0", "off", "no", "false"))

DAY_SECONDS = 24 * 60 * 60

#: The checkout this module lives in. The default cache is `.cache` under it, not under the working
#: directory: `den` run from anywhere else would find an empty cache there and re-fetch the whole corpus,
#: which looks like a slow first run rather than a mistake.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: What the Swift passes' `Data.write(options: .atomic)` left under the default umask. `mkstemp` creates
#: 0600, and a cache entry only its writer can read is one a second user or a backup silently skips.
MODE = 0o644


def write_atomically(path, body):
    """`body` at `path` via a temp file in the same directory and a rename.

    A reader — another worker, or the next run's resume — sees the old file or the new one, never one that
    was truncated and not yet refilled. An interrupted in-place write leaves a 0-byte file, and a resumable
    stage then either refuses it or starts over; neither is what the interruption asked for. Raises: the
    caller decides whether a failed write is fatal.
    """
    parent = os.path.dirname(os.path.abspath(path))
    handle, temp = tempfile.mkstemp(dir=parent, prefix=f".{os.path.basename(path)}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as fh:
            os.fchmod(fh.fileno(), MODE)
            fh.write(body)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


class ResponseCache:
    """One namespace's entries. `namespace` separates one source's bodies from another's, so `/movie/1`
    on TMDB cannot collide with a Wikipedia page of the same path and one source can be cleared alone."""

    def __init__(self, namespace, directory, ttl_seconds):
        self.namespace = namespace
        self.directory = directory
        self.ttl_seconds = ttl_seconds

    def key(self, path, query=None):
        """`path` + the query, minus credentials, hashed with the namespace.

        Sorted, so an equivalent request maps to one entry regardless of the order a caller built it in.
        """
        safe = "&".join(f"{name}={value}" for name, value in sorted((query or {}).items())
                        if name.lower() not in CREDENTIAL_KEYS)
        return hashlib.sha256(f"{self.namespace}\x01{path}?{safe}".encode()).hexdigest()

    def path_for(self, key):
        """Two-character fan-out: tens of thousands of entries in one directory makes every lookup a
        linear scan on some filesystems, and the directory itself unusable from a shell."""
        return os.path.join(self.directory, self.namespace, key[:2], f"{key}.json")

    def read(self, key):
        """The cached body, or None when absent, unreadable or past its TTL.

        Never raises: a broken entry must degrade to a live fetch, never fail a twelve-hour run.
        """
        path = self.path_for(key)
        try:
            if time.time() - os.path.getmtime(path) >= self.ttl_seconds:
                return None
            with open(path, "rb") as handle:
                body = handle.read()
        except OSError:
            return None
        return body or None

    def write(self, key, body):
        """Write via `write_atomically`.

        The pipeline fans out across threads, so two workers can write one key at once; without this a
        reader can see a half-written body and decode garbage — which is indistinguishable from an
        upstream that answered nonsense.
        """
        path = self.path_for(key)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            write_atomically(path, body)
        except OSError:
            # A cache that cannot be written is a slow run, not a failed one.
            return


def root(env=None):
    """Root directory for every namespace: `DEN_CACHE_DIR`, else `.cache` in this checkout."""
    env = os.environ if env is None else env
    return env.get("DEN_CACHE_DIR") or os.path.join(REPO, ".cache")


def configured(namespace, default_ttl_days, env=None):
    """A namespaced cache, or None when caching is switched off for it.

    `DEN_CACHE=0` disables everything; `<NAMESPACE>_CACHE=0` disables one source;
    `<NAMESPACE>_CACHE_TTL_DAYS` overrides its lifetime. A zero or negative TTL means every read misses —
    a write-only cache, which is never what someone reaching for "0" wants, so it reads as "off".
    """
    env = os.environ if env is None else env
    prefix = namespace.upper()
    if str(env.get("DEN_CACHE", "")).lower() in OFF or str(env.get(f"{prefix}_CACHE", "")).lower() in OFF:
        return None
    try:
        days = float(env.get(f"{prefix}_CACHE_TTL_DAYS") or default_ttl_days)
    except ValueError:
        days = default_ttl_days
    if days <= 0:
        return None
    return ResponseCache(namespace, root(env), days * DAY_SECONDS)


#: 180 days. A short expiry looks prudent for Wikipedia and is not: measured over ten weeks, 38% of plots
#: differed in some way but only ~4% moved the embedding far enough to matter, so expiry cannot tell those
#: apart and pays full price for the answer. Freshness is a decision taken two other ways: `WIKI_CACHE=0`
#: re-reads everything, and each grounded title stores the revision it was read from so a refresh can ask
#: for current revids in bulk.
#:
#: TMDB has no namespace any more: nothing in the pipeline asks it for a title (oxyc/den-dataset#53), and
#: `/discover`, the one call left, is never cached. The `.cache/tmdb` bodies earlier passes wrote are read
#: by nothing and age out.
TTL_DAYS = 180

WIKI_NAMESPACE = "wiki"


def wiki(env=None):
    return configured(WIKI_NAMESPACE, TTL_DAYS, env)
