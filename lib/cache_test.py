#!/usr/bin/env python3
"""The cache key, pinned — because it is a contract with 2.1 GB of bodies already on disk.

A key that hashes differently does not fail. It misses every entry the Swift passes wrote, re-fetches the
whole corpus, and looks like a slow first run; and `pipeline/backfill_plot_provenance.py`, which
reconstructs the same key to replay grounding decisions out of those bodies, reports every row
unrecoverable instead of erroring.

The digest below is not invented. It names a file that exists under `.cache/` in the main checkout,
written by the Swift pass that built the shipped corpus — so this test is the derivation held against real
evidence rather than against itself.
"""
import json
import os
import stat
import tempfile
import time
import unittest
from unittest import mock

from . import cache as caching
from . import wikipedia

#: `en.wikipedia.org/w/api.php` + `articleProse`'s exact query for "Star Wars (film)". Present as
#: `.cache/wiki/da/da9f30….json`.
WIKI_QUERY = {"action": "parse", "prop": "wikitext|revid", "format": "json",
              "formatversion": "2", "redirects": "1", "page": "Star Wars (film)"}
WIKI_DIGEST = "da9f30f7d87e9687f88315085805043bfd5cf19cfc6873463b178e925e234972"
WIKI_HOST_PATH = "en.wikipedia.org/w/api.php"


class Key(unittest.TestCase):
    def test_a_wikipedia_request_hashes_to_the_file_the_swift_pass_wrote(self):
        cache = caching.ResponseCache("wiki", "/nowhere", 1)
        self.assertEqual(cache.key("en.wikipedia.org/w/api.php", WIKI_QUERY), WIKI_DIGEST)

    def test_the_host_is_part_of_a_wikipedia_key(self):
        """Leaving it out was a silent correctness bug rather than a missed hit: every Wikipedia serves
        `/w/api.php`, so a request for the Italian "Iago (film)" had the same path and query as the
        English one and was handed the ENGLISH body. Nothing downstream could detect it — the text it
        yields is perfectly well-formed."""
        cache = caching.ResponseCache("wiki", "/nowhere", 1)
        english = cache.key("en.wikipedia.org/w/api.php", WIKI_QUERY)
        italian = cache.key("it.wikipedia.org/w/api.php", WIKI_QUERY)
        self.assertNotEqual(english, italian)

    def test_the_namespace_separates_two_sources_with_one_path(self):
        wiki = caching.ResponseCache("wiki", "/nowhere", 1)
        tmdb = caching.ResponseCache("tmdb", "/nowhere", 1)
        self.assertNotEqual(wiki.key("/movie/11", {}), tmdb.key("/movie/11", {}))

    def test_the_order_a_caller_built_the_query_in_does_not_matter(self):
        cache = caching.ResponseCache("wiki", "/nowhere", 1)
        forward = cache.key("host/path", {"a": "1", "b": "2"})
        backward = cache.key("host/path", {"b": "2", "a": "1"})
        self.assertEqual(forward, backward)

    def test_a_credential_never_reaches_a_key(self):
        """A cache directory is exactly the kind of place a secret gets copied into and forgotten, so the
        filename must not be able to carry one — whatever the call site remembers to do."""
        cache = caching.ResponseCache("tmdb", "/nowhere", 1)
        without = cache.key("/movie/11", {})
        for name in caching.CREDENTIAL_KEYS:
            self.assertEqual(cache.key("/movie/11", {name: "s3cret"}), without, name)

    def test_the_fan_out_is_the_first_two_characters(self):
        """Tens of thousands of entries in one directory makes every lookup a linear scan on some
        filesystems, and the directory itself unusable from a shell."""
        cache = caching.ResponseCache("wiki", "/root", 1)
        self.assertEqual(cache.path_for(WIKI_DIGEST),
                         os.path.join("/root", "wiki", "da", f"{WIKI_DIGEST}.json"))


class WikipediaKey(unittest.TestCase):
    """The key `fetch_parse` actually files a body under — built from `PARSE_QUERY`, not from a copy of it.

    `WIKI_QUERY` above pins the derivation; this pins the query the fetch sends. A parameter dropped from
    `PARSE_QUERY` moves every one of ~80k keys, and a test holding its own literal of the query would not
    notice."""

    def test_the_query_the_fetch_sends_hashes_to_the_file_the_swift_pass_wrote(self):
        cache = caching.ResponseCache("wiki", "/nowhere", 1)
        query = dict(wikipedia.PARSE_QUERY, page="Star Wars (film)")
        self.assertEqual(cache.key(WIKI_HOST_PATH, query), WIKI_DIGEST)

    def test_a_fetched_article_lands_in_the_swift_passs_file(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = caching.ResponseCache("wiki", directory, 3600)
            body = json.dumps({"parse": {"title": "Star Wars (film)", "revid": 1, "wikitext": "x"}}).encode()
            with mock.patch.object(wikipedia.http, "request", lambda *args, **kwargs: body):
                wikipedia.fetch_parse("Star Wars (film)", "en", cache)
            self.assertTrue(os.path.isfile(cache.path_for(WIKI_DIGEST)))

    @unittest.skipUnless(os.path.isfile(caching.ResponseCache("wiki", caching.root(), 1).path_for(WIKI_DIGEST)),
                         "no Swift-written cache here (CI has none); DEN_CACHE_DIR points at one")
    def test_the_digest_names_a_file_on_disk(self):
        """The evidence the digest above stands on, checked wherever the cache is present."""
        cache = caching.ResponseCache("wiki", caching.root(), 1)
        with open(cache.path_for(WIKI_DIGEST), "rb") as handle:
            self.assertEqual(json.loads(handle.read())["parse"]["title"], "Star Wars (film)")


class Store(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.cache = caching.ResponseCache("wiki", self.directory.name, 3600)

    def tearDown(self):
        self.directory.cleanup()

    def test_what_was_written_reads_back(self):
        key = self.cache.key("host/path", {"a": "1"})
        self.cache.write(key, b'{"parse":{}}')
        self.assertEqual(self.cache.read(key), b'{"parse":{}}')

    def test_an_entry_past_its_ttl_is_a_miss_rather_than_a_stale_answer(self):
        cache = caching.ResponseCache("wiki", self.directory.name, 1)
        key = cache.key("host/path", {})
        cache.write(key, b"{}")
        os.utime(cache.path_for(key), (time.time() - 10, time.time() - 10))
        self.assertIsNone(cache.read(key))

    def test_an_absent_or_empty_entry_degrades_to_a_live_fetch(self):
        """Never raises. A broken entry must cost a request, not a twelve-hour run."""
        self.assertIsNone(self.cache.read("0" * 64))
        key = self.cache.key("host/empty", {})
        self.cache.write(key, b"")
        self.assertIsNone(self.cache.read(key))

    def test_a_write_that_fails_part_way_leaves_the_previous_entry(self):
        """Written in place, the file is truncated before the body arrives, and a reader in between — or
        after a failure — gets an empty entry: a live fetch at best, a decode of garbage at worst."""
        key = self.cache.key("host/path", {})
        self.cache.write(key, b'{"parse":{"old":1}}')
        with self.assertRaises(TypeError):
            self.cache.write(key, object())
        self.assertEqual(self.cache.read(key), b'{"parse":{"old":1}}')
        leftovers = [name for name in os.listdir(os.path.dirname(self.cache.path_for(key)))
                     if name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_an_entry_is_readable_by_others_as_the_swift_passes_left_it(self):
        """0644, as the 2.1 GB the Swift passes wrote is. `mkstemp`'s 0600 would make every entry this
        port writes one a second user or a backup silently cannot read."""
        key = self.cache.key("host/path", {})
        self.cache.write(key, b"{}")
        self.assertEqual(stat.S_IMODE(os.stat(self.cache.path_for(key)).st_mode), 0o644)


class Atomic(unittest.TestCase):
    """`write_atomically`, which the resumable stages write their own files through as well."""

    def test_an_interrupted_write_leaves_the_previous_file_whole(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "doc-facts.json")
            caching.write_atomically(path, b"previous")
            with self.assertRaises(TypeError):
                caching.write_atomically(path, object())
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), b"previous")
            self.assertEqual(os.listdir(directory), ["doc-facts.json"])


class Configuration(unittest.TestCase):
    def test_the_default_root_is_this_checkouts_cache_wherever_den_is_run_from(self):
        """Relative to the working directory, `den` run from anywhere else finds an empty cache and
        re-fetches the whole corpus — which reads as a slow first run, not a mistake."""
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.assertEqual(caching.root({}), os.path.join(repo, ".cache"))
        self.assertEqual(caching.root({"DEN_CACHE_DIR": "/srv/cache"}), "/srv/cache")

    def test_a_source_can_be_switched_off_on_its_own_or_with_everything(self):
        self.assertIsNone(caching.configured("wiki", 180, {"DEN_CACHE": "0"}))
        self.assertIsNone(caching.configured("wiki", 180, {"WIKI_CACHE": "off"}))
        self.assertIsNotNone(caching.configured("wiki", 180, {"OTHER_CACHE": "0"}))

    def test_a_zero_ttl_is_off_rather_than_a_write_only_cache(self):
        """Every read would miss and every fetch would still be written — which is never what someone
        reaching for "0" wants."""
        self.assertIsNone(caching.configured("wiki", 180, {"WIKI_CACHE_TTL_DAYS": "0"}))

    def test_the_ttl_is_180_days(self):
        self.assertEqual(caching.TTL_DAYS, 180)
        self.assertEqual(caching.wiki({}).ttl_seconds, 180 * caching.DAY_SECONDS)


if __name__ == "__main__":
    unittest.main()
