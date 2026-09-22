#!/usr/bin/env python3
"""The TMDB client — the two shapes that used to pass for an answer.

Both are 200s. Neither raises anywhere without a check written for it:

  * a body with no `results` list, which reads as a page with no titles in it. The worklist's paging loop
    stops after page one and a delta reports "0 new titles" instead of failing — silently, and daily;
  * a partial detail record under load, which decodes cleanly because every field is optional and then
    sits in the cache for its whole TTL as a title with no keywords, no director and no cast.
"""
import json
import tempfile
import unittest

from . import cache as caching
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

    def test_origins_are_alternatives(self):
        """Within one TMDB parameter a pipe is OR and a comma is AND; a comma here would ask for titles
        originating in every named country at once, which is nothing."""
        params = tmdb_api.discover_params("movie", origin_country=["FR", "IT"])
        self.assertEqual(params["with_origin_country"], "FR|IT")

    def test_a_query_with_no_filters_carries_only_what_it_must(self):
        self.assertEqual(tmdb_api.discover_params("movie"),
                         {"sort_by": "popularity.desc", "include_adult": "false"})


class Paging(unittest.TestCase):
    def setUp(self):
        self.client = tmdb_api.TMDB(key="test", cache=None)

    def drive(self, body):
        fake = FakeHTTP(body)
        original = tmdb_api.http.request
        tmdb_api.http.request = fake
        try:
            return self.client.discover("movie", {}), fake
        finally:
            tmdb_api.http.request = original

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


class Body(unittest.TestCase):
    def test_a_partial_record_is_not_worth_keeping(self):
        """`{}` and TMDB's own `{"success":false,…}` both decode cleanly, because every detail field is
        optional. Anything cached is served for the whole TTL, so one bad minute becomes weeks."""
        self.assertFalse(tmdb_api.is_title_record({}, False))
        self.assertFalse(tmdb_api.is_title_record({"success": False, "status_code": 34}, False))
        self.assertFalse(tmdb_api.is_title_record({"id": 11, "title": "   "}, False))
        self.assertTrue(tmdb_api.is_title_record({"id": 11, "title": "Star Wars"}, False))

    def test_a_response_missing_the_resources_it_was_asked_for_is_not_worth_keeping(self):
        """A title with no keywords, no director and no cast is indistinguishable downstream from a title
        that genuinely has none."""
        asked = {"id": 11, "title": "Star Wars"}
        self.assertFalse(tmdb_api.is_title_record(asked, True))
        self.assertTrue(tmdb_api.is_title_record(dict(asked, keywords={}, credits={}), True))

    def test_a_series_carries_its_name_where_a_film_carries_its_title(self):
        self.assertTrue(tmdb_api.is_title_record({"id": 1399, "name": "Game of Thrones"}, False))


class Sidecar(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.cache = caching.ResponseCache("tmdb", self.directory.name, 3600)
        self.client = tmdb_api.TMDB(key="test", cache=self.cache)

    def tearDown(self):
        self.directory.cleanup()

    def test_it_reads_the_record_enrichment_already_paid_for(self):
        """The cache key covers the query string, so a bare `/movie/11` and
        `/movie/11?append_to_response=…` hash differently: asking for the bare one meant a 100%-cached
        corpus still cost one live call per title. Measured, 47,541 of 47,542 titles are already on disk
        under the enrichment key and none under the bare one."""
        self.cache.write(self.cache.key("/movie/11", {"append_to_response": tmdb_api.APPENDED}),
                         json.dumps({"id": 11, "title": "Star Wars", "poster_path": "/p.jpg",
                                     "release_date": "1977-05-25"}).encode())
        original = tmdb_api.http.request

        def refuse(*args, **kwargs):
            raise AssertionError("a cached title must not cost a request")

        tmdb_api.http.request = refuse
        try:
            row = self.client.poster_meta("movie", 11)
        finally:
            tmdb_api.http.request = original
        self.assertEqual(row, {"tmdbId": 11, "mediaType": "movie", "title": "Star Wars",
                               "posterPath": "/p.jpg", "year": 1977})

    def test_a_title_with_no_poster_is_still_a_row(self):
        """A missing poster is a fact about the title; a missing ROW is a card the app cannot render."""
        self.cache.write(self.cache.key("/tv/1399", {"append_to_response": tmdb_api.APPENDED}),
                         json.dumps({"id": 1399, "name": "Game of Thrones",
                                     "first_air_date": "2011-04-17"}).encode())
        row = self.client.poster_meta("tv", 1399)
        self.assertIsNone(row["posterPath"])
        self.assertEqual((row["title"], row["year"]), ("Game of Thrones", 2011))

    def test_a_record_with_no_date_has_no_year_rather_than_a_wrong_one(self):
        self.assertIsNone(tmdb_api.year_of({"id": 1}))
        self.assertIsNone(tmdb_api.year_of({"id": 1, "release_date": ""}))
        self.assertEqual(tmdb_api.year_of({"id": 1, "first_air_date": "2011-04-17"}), 2011)


if __name__ == "__main__":
    unittest.main()
