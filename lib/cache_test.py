#!/usr/bin/env python3
"""The cache key, pinned — because it is a contract with 2.1 GB of bodies already on disk.

A key that hashes differently does not fail. It misses every entry the Swift passes wrote, re-fetches the
whole corpus, and looks like a slow first run; and `scripts/backfill-plot-provenance.py`, which
reconstructs the same key to replay grounding decisions out of those bodies, reports every row
unrecoverable instead of erroring.

The two digests below are not invented. Each one names a file that exists under `.cache/` in the main
checkout, written by the Swift pass that built the shipped corpus — so this test is the derivation held
against real evidence rather than against itself.
"""
import os
import tempfile
import time
import unittest

from . import cache as caching

#: `en.wikipedia.org/w/api.php` + `articleProse`'s exact query for "Star Wars (film)". Present as
#: `.cache/wiki/da/da9f30….json`.
WIKI_QUERY = {"action": "parse", "prop": "wikitext|revid", "format": "json",
              "formatversion": "2", "redirects": "1", "page": "Star Wars (film)"}
WIKI_DIGEST = "da9f30f7d87e9687f88315085805043bfd5cf19cfc6873463b178e925e234972"

#: `/movie/11` with the sub-resources `enrich` appends. Present as `.cache/tmdb/33/333e08….json` — the
#: entry the enrichment wrote, which the Python port of it has to find under the same name.
TMDB_DIGEST = "333e08246e09fd6059b081f999aa20f43bcf156ca165f6d189519efd982e1800"


class Key(unittest.TestCase):
    def test_a_wikipedia_request_hashes_to_the_file_the_swift_pass_wrote(self):
        cache = caching.ResponseCache("wiki", "/nowhere", 1)
        self.assertEqual(cache.key("en.wikipedia.org/w/api.php", WIKI_QUERY), WIKI_DIGEST)

    def test_a_tmdb_detail_request_hashes_to_the_file_the_swift_pass_wrote(self):
        cache = caching.ResponseCache("tmdb", "/nowhere", 1)
        self.assertEqual(cache.key("/movie/11", {"append_to_response": "keywords,credits"}), TMDB_DIGEST)

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


class Configuration(unittest.TestCase):
    def test_a_source_can_be_switched_off_on_its_own_or_with_everything(self):
        self.assertIsNone(caching.configured("wiki", 180, {"DEN_CACHE": "0"}))
        self.assertIsNone(caching.configured("wiki", 180, {"WIKI_CACHE": "off"}))
        self.assertIsNotNone(caching.configured("wiki", 180, {"TMDB_CACHE": "0"}))

    def test_a_zero_ttl_is_off_rather_than_a_write_only_cache(self):
        """Every read would miss and every fetch would still be written — which is never what someone
        reaching for "0" wants."""
        self.assertIsNone(caching.configured("wiki", 180, {"WIKI_CACHE_TTL_DAYS": "0"}))

    def test_the_ttl_is_the_one_both_sources_share(self):
        """180 days, deliberately the same number for both so they age out together. For TMDB it is a
        COMPLIANCE boundary — their terms allow caching for a limited period, not indefinitely."""
        self.assertEqual(caching.TTL_DAYS, 180)
        self.assertEqual(caching.tmdb({}).ttl_seconds, 180 * caching.DAY_SECONDS)
        self.assertEqual(caching.wiki({}).ttl_seconds, 180 * caching.DAY_SECONDS)

    def test_discover_is_not_cacheable_and_a_title_detail_is(self):
        """`/discover` exists to surface what is new or has newly crossed the vote floor, so serving it
        from disk hides exactly what it is asked for."""
        self.assertTrue(caching.tmdb_is_cacheable("/movie/278"))
        self.assertTrue(caching.tmdb_is_cacheable("/tv/1396"))
        self.assertFalse(caching.tmdb_is_cacheable("/discover/movie"))
        self.assertFalse(caching.tmdb_is_cacheable("/movie/popular"))
        self.assertFalse(caching.tmdb_is_cacheable("/movie/278/credits"))


if __name__ == "__main__":
    unittest.main()
