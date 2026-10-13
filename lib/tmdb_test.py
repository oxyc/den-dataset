#!/usr/bin/env python3
"""The TMDB client — `/discover`, and the shape that used to pass for an answer.

A body with no `results` list is a 200 that reads as a page with no titles in it. The worklist's paging loop
stops after page one and a delta reports "0 new titles" instead of failing — silently, and daily.
"""
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from . import cache as caching, tmdb as tmdb_api


class FakeHTTP:
    """Answers whatever it was handed, and records the requests. `lib/http` is the seam."""

    def __init__(self, body):
        self.body = body
        self.requests = []

    def __call__(self, host, path, params=None, **kwargs):
        self.requests.append((host, path, dict(params or {})))
        return json.dumps(self.body).encode("utf-8")


class Query(unittest.TestCase):
    def test_a_series_window_uses_the_field_a_series_has(self):
        """TMDB names the date `primary_release_date` for a film and `first_air_date` for a series.
        Sending the wrong one is not an error — it is an unfiltered query that looks like a filtered
        one."""
        movie = tmdb_api.discover_params("movie", release_date_gte="2026-01-01")
        series = tmdb_api.discover_params("tv", release_date_gte="2026-01-01")
        self.assertEqual(movie["primary_release_date.gte"], "2026-01-01")
        self.assertEqual(series["first_air_date.gte"], "2026-01-01")
        self.assertNotIn("first_air_date.gte", movie)

    def test_a_bounded_window_names_both_ends_in_the_medias_own_fields(self):
        movie = tmdb_api.discover_params("movie", release_date_gte="1999-01-01",
                                        release_date_lte="1999-12-31")
        series = tmdb_api.discover_params("tv", release_date_gte="1999-01-01",
                                         release_date_lte="1999-12-31")
        self.assertEqual((movie["primary_release_date.gte"], movie["primary_release_date.lte"]),
                         ("1999-01-01", "1999-12-31"))
        self.assertEqual((series["first_air_date.gte"], series["first_air_date.lte"]),
                         ("1999-01-01", "1999-12-31"))

    def test_a_query_with_no_filters_carries_only_what_it_must(self):
        self.assertEqual(tmdb_api.discover_params("movie"),
                         {"sort_by": "popularity.desc", "include_adult": "false"})

    def test_origin_countries_are_any_of(self):
        """`|` is TMDB's OR; `,` would ask for a title from every one of them at once."""
        self.assertEqual(tmdb_api.discover_params("movie", origin_countries={"SE", "FR"})["with_origin_country"],
                         "FR|SE")


class Paging(unittest.TestCase):
    def setUp(self):
        self.client = tmdb_api.TMDB(key="test")

    def drive(self, body):
        fake = FakeHTTP(body)
        with mock.patch.object(tmdb_api.http, "request", fake):
            return self.client.discover("movie", {}), fake

    def test_a_real_page_reads_as_a_page(self):
        (rows, page, total), _fake = self.drive({"page": 2, "total_pages": 5, "results": [{"id": 7}]})
        self.assertEqual([row["id"] for row in rows], [7])
        self.assertEqual((page, total), (2, 5))

    def test_an_error_body_is_refused_rather_than_read_as_an_empty_page(self):
        with self.assertRaises(tmdb_api.TMDBError) as refused:
            self.drive({"success": False, "status_message": "Invalid API key"})
        self.assertIn("results", str(refused.exception))

    def test_a_single_page_response_that_omits_the_counts_still_reads(self):
        """A one-page answer legitimately omits `page`/`total_pages`; only `results` is required."""
        (rows, page, total), _fake = self.drive({"results": []})
        self.assertEqual((rows, page, total), ([], 1, 1))

    def test_the_key_rides_in_the_query_and_nothing_is_cached_without_a_cache(self):
        for _ in range(2):
            _answer, fake = self.drive({"results": []})
            self.assertEqual(fake.requests[0][2]["api_key"], "test")


class Cached(unittest.TestCase):
    """A page is served from disk only while it is younger than the age the caller allows, an error body is
    never written, and the key never reaches the cache."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.cache = caching.ResponseCache(tmdb_api.DISCOVER_NAMESPACE, self.dir, 7 * caching.DAY_SECONDS)
        self.clock = [1_000_000.0]
        self.client = tmdb_api.TMDB(key="secret-key", cache=self.cache, now=lambda: self.clock[0])

    def ask(self, body, max_age):
        fake = FakeHTTP(body)
        with mock.patch.object(tmdb_api.http, "request", fake):
            rows, _page, _total = self.client.discover("movie", {"sort_by": "vote_count.desc"}, max_age=max_age)
        return [row["id"] for row in rows], len(fake.requests)

    def written(self):
        path = os.path.join(self.dir, tmdb_api.DISCOVER_NAMESPACE)
        return [os.path.join(root, name) for root, _dirs, names in os.walk(path) for name in names]

    def test_a_young_page_is_reused_and_an_old_one_asked_again(self):
        self.clock[0] = time.time()
        self.assertEqual(self.ask({"results": [{"id": 1}]}, 3600), ([1], 1))
        self.assertEqual(self.ask({"results": [{"id": 2}]}, 3600), ([1], 0), "a fresh page was asked again")
        self.clock[0] += 7200
        self.assertEqual(self.ask({"results": [{"id": 3}]}, 3600), ([3], 1), "a stale page was reused")

    def test_no_age_means_ask(self):
        self.clock[0] = time.time()
        self.ask({"results": [{"id": 1}]}, 3600)
        self.assertEqual(self.ask({"results": [{"id": 2}]}, 0), ([2], 1))

    def test_an_error_body_is_not_cached_and_the_key_is_never_written(self):
        with self.assertRaises(tmdb_api.TMDBError):
            self.ask({"status_message": "Invalid API key"}, 3600)
        self.assertEqual(self.written(), [])
        self.clock[0] = time.time()
        self.ask({"results": [{"id": 1}]}, 3600)
        for path in self.written():
            with open(path, encoding="utf-8") as fh:
                self.assertNotIn("secret-key", path + fh.read())

    def test_no_key_is_a_refusal(self):
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(tmdb_api.TMDBError):
            tmdb_api.TMDB()


if __name__ == "__main__":
    unittest.main()
