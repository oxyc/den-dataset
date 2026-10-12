import datetime
import gzip
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import certification_coverage as coverage


class Inputs(unittest.TestCase):
    def test_loads_spine_wikidata_years_and_ephemeral_popularity_rank(self):
        with tempfile.TemporaryDirectory() as work:
            labels = os.path.join(work, "labels.json")
            facts = os.path.join(work, "facts.json")
            export = os.path.join(work, "movie.json.gz")
            with open(labels, "w") as fh:
                json.dump({"records": [{"mediaType": "movie", "tmdbId": 2},
                                        {"mediaType": "movie", "tmdbId": 1}]}, fh)
            with open(facts, "w") as fh:
                json.dump({"records": [{"mediaType": "movie", "tmdbId": 1,
                                        "released": {"date": "+2024-01-02"}}]}, fh)
            with gzip.open(export, "wt") as fh:
                fh.write('{"id": 1, "popularity": 8}\n{"id": 2, "popularity": 20}\n')
            rows = coverage.load_titles(labels, facts, {"movie": export, "tv": None})
        self.assertEqual([(r["id"], r["year"], r["percentile"]) for r in rows],
                         [(2, None, 0), (1, 2024, .5)])


class Sources(unittest.TestCase):
    def test_tmdb_movie_and_tv_shapes_are_normalized_only_in_memory(self):
        movie = {"results": [{"iso_3166_1": "US", "release_dates": [
            {"certification": "PG"}, {"certification": ""}]}]}
        tv = {"results": [{"iso_3166_1": "GB", "rating": "12"}]}
        self.assertEqual(coverage.tmdb_values("movie", movie), {"US": {"PG"}})
        self.assertEqual(coverage.tmdb_values("tv", tv), {"GB": {"12"}})

    def test_tmdb_error_body_is_not_silently_counted_as_no_coverage(self):
        with self.assertRaises(coverage.tmdb.TMDBError):
            coverage.tmdb_values("movie", {"status_message": "bad key"})

    def test_wikidata_query_keeps_media_property_country_properties_and_exclusions(self):
        query = coverage.wikidata_query([2, 1], "movie", {2: ["Q9"]})
        self.assertIn('VALUES ?tmdb { "1" "2" }', query)
        self.assertIn("wdt:P4947", query)
        self.assertIn("wdt:P1657|wdt:P2363", query)
        self.assertIn('("2" wd:Q9)', query)

    def test_band_mapping_preserves_pg13_and_r_boundary(self):
        self.assertEqual(coverage.band("US", "PG-13"), "teen")
        self.assertEqual(coverage.band("US", "R"), "mature")
        self.assertEqual(coverage.band("GB", "12A"), "teen")
        self.assertIsNone(coverage.band("US", "Not Rated"))


class Report(unittest.TestCase):
    titles = [
        {"media": "movie", "id": 1, "year": 2025, "popularity": 9, "percentile": 0},
        {"media": "tv", "id": 2, "year": 2010, "popularity": 1, "percentile": .75},
        {"media": "movie", "id": 3, "year": None, "popularity": None},
    ]

    def test_sources_are_separate_overlap_and_strata_are_aggregate(self):
        wd = {("movie", 1): {"US": {"PG"}}, ("tv", 2): {"GB": {"mystery"}}}
        runtime = {("movie", 1): {"US": {"PG-13"}}, ("movie", 3): {"DE": {"FSK 6"}}}
        report = coverage.aggregate(self.titles, wd, runtime, "GB", datetime.date(2026, 1, 1))
        self.assertEqual(report["overlap"], {"both": 1, "wikidataOnly": 1,
                                             "tmdbRuntimeOnly": 1, "neither": 0})
        self.assertEqual(report["sources"]["wikidata"]["all"]["anyCertification"], 2)
        self.assertEqual(report["sources"]["wikidata"]["all"]["viewerRegion"], 1)
        self.assertEqual(report["sources"]["tmdbRuntime"]["all"]["band:family"], 1)
        self.assertEqual(report["sources"]["wikidata"]["all"]["onlyUnknownOrUnmapped"], 1)
        self.assertEqual(report["sources"]["wikidata"]["recency:last5"]["titles"], 1)

    def test_serialized_report_contains_no_title_identifier_or_raw_rating(self):
        report = coverage.aggregate(self.titles, {("movie", 1): {"US": {"secret-rating"}}}, {},
                                    as_of=datetime.date(2026, 1, 1))
        body = json.dumps(report)
        for forbidden in ("tmdbId", "movie:1", "secret-rating"):
            self.assertNotIn(forbidden, body)

    def test_main_writes_only_the_aggregate_report(self):
        with tempfile.TemporaryDirectory() as work, \
             mock.patch.object(coverage, "load_titles", return_value=self.titles), \
             mock.patch.object(coverage, "fetch_wikidata", return_value={}), \
             mock.patch.object(coverage, "fetch_tmdb", return_value={}):
            out = os.path.join(work, "aggregate.json")
            self.assertEqual(coverage.main(["--labels", "l", "--facts", "f", "--out", out]), 0)
            with open(out) as fh:
                report = json.load(fh)
        self.assertEqual(report["scope"]["titles"], 3)
        self.assertNotIn("titles", report["sources"]["wikidata"])


if __name__ == "__main__":
    unittest.main()
