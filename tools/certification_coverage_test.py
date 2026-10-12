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

    def test_loads_verified_public_maturity_and_rejects_a_non_conservative_age(self):
        body = {"records": [{"mediaType": "movie", "tmdbId": 1, "minimumAge": 16,
                             "observations": [
                                 {"authority": "CNC", "age": 12, "matchedBy": "exact"},
                                 {"authority": "KAVI", "authorityId": "secret-id", "raw": "secret-raw",
                                  "age": 16, "matchedBy": "exact"}]}],
                "derivation": {"audit": {"CNC": {"ambiguousRows": 2},
                                           "crossAuthorityConflicts": [{"key": "movie:1"}]}}}
        with tempfile.TemporaryDirectory() as work:
            path = os.path.join(work, "maturity.json")
            with open(path, "w") as fh:
                json.dump(body, fh)
            values, audit = coverage.load_public_maturity(path)
            body["records"][0]["minimumAge"] = 12
            with open(path, "w") as fh:
                json.dump(body, fh)
            with self.assertRaisesRegex(ValueError, "highest observation"):
                coverage.load_public_maturity(path)
        self.assertEqual(values[("movie", 1)]["minimumAge"], 16)
        self.assertEqual(audit, {"crossAuthorityConflicts": 1, "ambiguousRows": 2})
        self.assertNotIn("secret", repr(values))


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

    def test_age_mapping_preserves_exact_boundaries_and_does_not_guess_pg(self):
        self.assertEqual(coverage.minimum_age("US", "PG-13"), 13)
        self.assertEqual(coverage.minimum_age("US", "R"), 17)
        self.assertEqual(coverage.minimum_age("GB", "12A"), 12)
        self.assertIsNone(coverage.minimum_age("US", "PG"))
        self.assertIsNone(coverage.minimum_age("US", "Not Rated"))


class Report(unittest.TestCase):
    titles = [
        {"media": "movie", "id": 1, "year": 2025, "popularity": 9, "percentile": 0},
        {"media": "tv", "id": 2, "year": 2010, "popularity": 1, "percentile": .75},
        {"media": "movie", "id": 3, "year": None, "popularity": None},
    ]

    def test_sources_are_separate_overlap_and_strata_are_aggregate(self):
        wd = {("movie", 1): {"US": {"G"}}, ("tv", 2): {"GB": {"mystery"}}}
        runtime = {("movie", 1): {"US": {"PG-13"}}, ("movie", 3): {"DE": {"FSK 6"}}}
        public = {("movie", 1): {"minimumAge": 12, "authorities": {
            "CNC": {"ages": {12}, "methods": {"exact-title+year+director"}}}}}
        report = coverage.aggregate(self.titles, wd, runtime, public,
                                    {"crossAuthorityConflicts": 1, "ambiguousRows": 2},
                                    "GB", datetime.date(2026, 1, 1))
        self.assertEqual(report["overlap"], {"both": 1, "wikidataOnly": 1,
                                             "tmdbRuntimeOnly": 1, "neither": 0})
        self.assertEqual(report["sources"]["wikidata"]["all"]["anyCertification"], 2)
        self.assertEqual(report["sources"]["wikidata"]["all"]["viewerRegion"], 1)
        self.assertEqual(report["sources"]["tmdbRuntime"]["all"]["age:12"], 0)
        self.assertEqual(report["sources"]["wikidata"]["all"]["onlyUnknownOrUnmapped"], 1)
        self.assertEqual(report["sources"]["wikidata"]["recency:last5"]["titles"], 1)
        self.assertEqual(report["sources"]["publicBaseline"]["all"]["anyCertification"], 1)
        self.assertEqual(report["publicAuthorities"]["CNC"]["all"]["age:12"], 1)
        self.assertEqual(report["publicAudit"], {
            "crossAuthorityConflicts": 1, "ambiguousRows": 2,
            "matchMethods": {"exact-title+year+director": 1}})
        self.assertEqual(report["overlapThreeSources"]["allThree"], 1)

    def test_region_wins_and_only_absence_uses_us_fallback(self):
        values = {
            ("movie", 1): {"GB": {"12"}, "US": {"R"}, "DE": {"FSK 0"}},
            ("tv", 2): {"GB": {"unmapped"}, "US": {"TV-Y"}},
            ("movie", 3): {"US": {"PG-13"}},
        }
        report = coverage.aggregate(self.titles, values, {}, region="GB", as_of=datetime.date(2026, 1, 1))
        counts = report["sources"]["wikidata"]["all"]
        self.assertEqual(counts["viewerRegion"], 2)
        self.assertEqual(counts["usFallback"], 1)
        self.assertEqual(counts["age:0"], 0)  # the US and DE values for regional rows do not leak in
        self.assertEqual(counts["age:17"], 0)
        self.assertEqual(counts["onlyUnknownOrUnmapped"], 1)
        self.assertEqual(counts["age:12"], 1)
        self.assertEqual(counts["age:13"], 1)
        self.assertEqual(counts["eligibleAt:12"], 1)
        self.assertEqual(counts["eligibleAt:13"], 2)

    def test_maximum_effective_age_conservatively_decides_kid_eligibility(self):
        values = {("movie", 1): {"US": {"G", "PG-13", "R"}}}
        report = coverage.aggregate(self.titles, values, {}, region="US", as_of=datetime.date(2026, 1, 1))
        counts = report["sources"]["wikidata"]["all"]
        self.assertEqual(counts["age:0"], 1)
        self.assertEqual(counts["age:13"], 1)
        self.assertEqual(counts["age:17"], 1)
        self.assertEqual(counts["eligibleAt:16"], 0)
        self.assertEqual(counts["eligibleAt:17"], 1)

    def test_serialized_report_contains_no_title_identifier_or_raw_rating(self):
        public = {("movie", 1): {"minimumAge": 12, "authorities": {
            "secret-authority": {"ages": {12}, "methods": {"secret-method"}}}}}
        report = coverage.aggregate(self.titles, {("movie", 1): {"US": {"secret-rating"}}}, {}, public,
                                    as_of=datetime.date(2026, 1, 1))
        body = json.dumps(report)
        for forbidden in ("tmdbId", "movie:1", "secret-rating"):
            self.assertNotIn(forbidden, body)

    def test_public_maturity_outside_the_measured_spine_is_refused(self):
        public = {("movie", 99): {"minimumAge": 12, "authorities": {
            "CNC": {"ages": {12}, "methods": {"exact"}}}}}
        with self.assertRaisesRegex(ValueError, "outside the measured corpus"):
            coverage.aggregate(self.titles, {}, {}, public)

    def test_main_writes_only_the_aggregate_report(self):
        with tempfile.TemporaryDirectory() as work, \
             mock.patch.object(coverage, "load_titles", return_value=self.titles), \
             mock.patch.object(coverage, "load_public_maturity", return_value=({}, {})), \
             mock.patch.object(coverage, "fetch_wikidata", return_value={}), \
             mock.patch.object(coverage, "fetch_tmdb", return_value={}):
            out = os.path.join(work, "aggregate.json")
            args = ["--labels", "l", "--facts", "f", "--maturity", "m", "--out", out,
                    "--generated-at", "2026-01-02T03:04:05+00:00"]
            self.assertEqual(coverage.main(args), 0)
            with open(out, "rb") as fh:
                first = fh.read()
            self.assertEqual(coverage.main(args), 0)
            with open(out, "rb") as fh:
                self.assertEqual(fh.read(), first)
            with open(out) as fh:
                report = json.load(fh)
        self.assertEqual(report["scope"]["titles"], 3)
        self.assertEqual(report["generatedAt"], "2026-01-02T03:04:05+00:00")
        self.assertNotIn("titles", report["sources"]["wikidata"])

    def test_generated_at_requires_an_offset(self):
        with self.assertRaisesRegex(SystemExit, "UTC offset"):
            coverage.main(["--labels", "l", "--facts", "f", "--maturity", "m",
                           "--generated-at", "2026-01-02T03:04:05"])

    def test_smoke_limit_restricts_the_public_artifact_to_the_measured_prefix(self):
        public = {("movie", 1): {"minimumAge": 12, "authorities": {
            "CNC": {"ages": {12}, "methods": {"exact"}}}},
                  ("tv", 2): {"minimumAge": 16, "authorities": {
            "KAVI": {"ages": {16}, "methods": {"exact"}}}}}
        with tempfile.TemporaryDirectory() as work, \
             mock.patch.object(coverage, "load_titles", return_value=self.titles), \
             mock.patch.object(coverage, "load_public_maturity", return_value=(public, {})), \
             mock.patch.object(coverage, "fetch_wikidata", return_value={}), \
             mock.patch.object(coverage, "fetch_tmdb", return_value={}):
            out = os.path.join(work, "aggregate.json")
            coverage.main(["--labels", "l", "--facts", "f", "--maturity", "m", "--limit", "1",
                           "--out", out, "--generated-at", "2026-01-02T03:04:05+00:00"])
            with open(out) as fh:
                report = json.load(fh)
        self.assertEqual(report["scope"]["titles"], 1)
        self.assertEqual(report["sources"]["publicBaseline"]["all"]["anyCertification"], 1)


if __name__ == "__main__":
    unittest.main()
