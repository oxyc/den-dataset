#!/usr/bin/env python3
"""The TMDB client — `/discover`, and the shape that used to pass for an answer.

A body with no `results` list is a 200 that reads as a page with no titles in it. The worklist's paging loop
stops after page one and a delta reports "0 new titles" instead of failing — silently, and daily.
"""
import json
import os
import unittest
from unittest import mock

from . import tmdb as tmdb_api


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

    def test_the_key_rides_in_the_query_and_nothing_is_cached(self):
        """`/discover` answers a question that changes daily; served from disk it would hide the new titles
        it exists to find."""
        for _ in range(2):
            _answer, fake = self.drive({"results": []})
            self.assertEqual(fake.requests[0][2]["api_key"], "test")

    def test_no_key_is_a_refusal(self):
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(tmdb_api.TMDBError):
            tmdb_api.TMDB()


if __name__ == "__main__":
    unittest.main()
