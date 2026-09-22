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
from unittest import mock

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


class Detail(unittest.TestCase):
    """`TMDB.get` on a detail path against a real cache directory — the rule above, used."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.client = tmdb_api.TMDB(key="test",
                                    cache=caching.ResponseCache("tmdb", self.directory.name, 3600))

    def fetch_twice(self, body):
        fake = FakeHTTP(body)
        with mock.patch.object(tmdb_api.http, "request", fake):
            for _ in range(2):
                self.client.get("/movie/11", {"append_to_response": "keywords,credits"})
        return len(fake.requests)

    def test_a_partial_record_is_asked_for_again_rather_than_served_for_the_ttl(self):
        self.assertEqual(self.fetch_twice({"id": 11, "title": "Star Wars"}), 2)

    def test_a_whole_record_is_served_from_disk_the_second_time(self):
        self.assertEqual(self.fetch_twice({"id": 11, "title": "Star Wars", "keywords": {}, "credits": {}}), 1)


SHAWSHANK = {"id": 278, "title": "The Shawshank Redemption", "release_date": "1994-09-23",
             "overview": "  A banker is sentenced to life in Shawshank.  ", "vote_count": 29000,
             "original_language": "en", "origin_country": ["US"],
             "genres": [{"id": 18, "name": "Drama"}, {"id": 80, "name": "Crime"}],
             "keywords": {"keywords": [{"id": 378, "name": "prison"}]},
             "credits": {"cast": [{"name": "Morgan Freeman", "order": 1}, {"name": "Tim Robbins", "order": 0},
                                  {"name": "Uncredited"}, {"name": "Bob Gunton", "order": 2},
                                  {"name": "William Sadler", "order": 3}, {"name": "Clancy Brown", "order": 4}],
                         "crew": [{"name": "Roger Deakins", "job": "Director of Photography"},
                                  {"name": "Frank Darabont", "job": "Director"}]}}


class Record(unittest.TestCase):
    def test_the_overview_does_not_cross_at_all_not_even_its_length(self):
        """TMDB's terms (§1.C) speak to their content in a machine-learning application; this record feeds a
        classifier and an embedder. Its length crossed for one reader, the stub check, which is gone."""
        record = tmdb_api.title_record(SHAWSHANK, 278, "movie")
        self.assertNotIn("Shawshank.", json.dumps(record))
        self.assertEqual([name for name in record if "overview" in name.lower()], [])

    def test_an_overview_of_the_wrong_type_is_still_a_refusal(self):
        """Read and discarded, not skipped: a body whose fields have the wrong types is a partial 200, and
        the Swift decoder dropped the whole record rather than enriching half of one."""
        with self.assertRaises(ValueError):
            tmdb_api.title_record(dict(SHAWSHANK, overview=["a"]), 278, "movie")

    def test_the_facts_it_does_carry_and_nothing_else(self):
        """Each field is here for a reader: the admission tier, the export row's count, the `animated` flag
        `./den genres-moods` takes from genre 16. The title, year, genre names, keywords, director and cast
        were written for readers that are gone, and nothing named by a person crosses at all."""
        record = tmdb_api.title_record(SHAWSHANK, 278, "movie")
        self.assertEqual(record, {"tmdbId": 278, "mediaType": "movie", "genreIDs": [18, 80],
                                  "originCountry": ["US"], "originalLanguage": "en", "voteCount": 29000})
        for text in ("Shawshank", "Darabont", "Robbins", "prison", "Drama", "1994"):
            self.assertNotIn(text, json.dumps(record))

    def test_tmdb_creators_never_cross(self):
        """`createdBy` is composed into the embedding document; it comes from Wikidata (P170) alone."""
        series = {"id": 1438, "name": "The Wire", "first_air_date": "2002-06-02",
                  "created_by": [{"name": "David Simon"}], "credits": {"crew": [{"name": "X", "job": "Creator"}]}}
        film = {"id": 1, "title": "F", "credits": {"crew": [{"name": "Y", "job": "Creator"}]}}
        for body, media in ((series, "tv"), (film, "movie")):
            record = tmdb_api.title_record(body, body["id"], media)
            self.assertNotIn("createdBy", record)
            self.assertNotIn("David Simon", json.dumps(record))

    def test_production_countries_answer_only_when_origin_country_is_absent(self):
        body = {"id": 1, "title": "F", "production_countries": [{"iso_3166_1": "FR"}]}
        self.assertEqual(tmdb_api.title_record(body, 1, "movie")["originCountry"], ["FR"])
        self.assertEqual(tmdb_api.title_record(dict(body, origin_country=[]), 1, "movie")["originCountry"], [])

    def test_a_body_of_the_wrong_shape_is_refused_rather_than_half_read(self):
        """A record whose `genres` is not a list of `{id, name}` did not decode, and was dropped as a dead id
        rather than enriched with half its fields."""
        for body in ([], {"id": 1, "genres": [{"name": "Drama"}]}, {"id": 1, "vote_count": "12"},
                     {"id": 1, "vote_count": True}):
            with self.assertRaises(ValueError):
                tmdb_api.title_record(body, 1, "movie")


if __name__ == "__main__":
    unittest.main()
