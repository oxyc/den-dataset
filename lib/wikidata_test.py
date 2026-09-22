#!/usr/bin/env python3
"""The Wikidata hop — the query it sends, and the two ways a name silently vanished.

Both cost real coverage and neither raised anything:

  * asking the label service for `en` alone returns the bare Q-id for anyone whose name lives under the
    `mul` code, which `parse_labelled` drops as an identifier. Christopher Nolan has no English
    `rdfs:label` at all, so Inception, The Dark Knight and The Prestige lost their director;
  * comparing Wikidata's genre labels to TMDB's without stripping the medium makes a vocabulary that
    largely does line up look like it shares nothing: mean overlap measured 0.01 before the strip
    and 0.40 after.

The query TEXT is pinned because it is hashed into the cache key: reformatting it re-asks Wikidata for
~770 batches already on disk.
"""
import json
import unittest

from . import wikidata


def bindings(*pairs):
    return json.dumps({"results": {"bindings": [
        {"tmdb": {"value": str(tmdb)}, "vLabel": {"value": label}} for tmdb, label in pairs]}}).encode()


class Query(unittest.TestCase):
    def test_the_label_service_is_asked_for_en_and_mul(self):
        """`mul` is where Wikidata keeps proper names now. Asking for `en` alone is not an error — it is a
        director that quietly is not there."""
        self.assertIn('wikibase:language "en,mul"', wikidata.query_text([1], "movie", "P57"))

    def test_a_film_and_a_series_are_matched_on_different_properties(self):
        self.assertIn("wdt:P4947", wikidata.query_text([1], "movie", "P57"))
        self.assertIn("wdt:P4983", wikidata.query_text([1], "tv", "P57"))

    def test_the_ids_are_quoted_values_in_the_order_given(self):
        self.assertIn('VALUES ?tmdb { "11" "12" }', wikidata.query_text([11, 12], "movie", "P136"))

    def test_the_text_is_stable_for_one_batch(self):
        """It is hashed into the cache key, so two spellings of one question are two scrapes."""
        self.assertEqual(wikidata.query_text([11, 12], "movie", "P57"),
                         wikidata.query_text([11, 12], "movie", "P57"))


class Parse(unittest.TestCase):
    def test_values_accumulate_per_id(self):
        """A film binds once per value, so two directors arrive as two rows."""
        parsed = wikidata.parse_labelled(bindings((11, "Lucas"), (11, "Kershner"), (12, "Stanton")))
        self.assertEqual(parsed, {11: ["Lucas", "Kershner"], 12: ["Stanton"]})

    def test_a_repeated_value_is_kept_once(self):
        self.assertEqual(wikidata.parse_labelled(bindings((11, "Lucas"), (11, "Lucas"))), {11: ["Lucas"]})

    def test_an_unresolved_label_is_dropped_rather_than_embedded(self):
        """A label the service could not resolve comes back as the bare Q-id. That is an identifier, not a
        name, and embedding "Q1379241" as a director teaches the model nothing."""
        self.assertEqual(wikidata.parse_labelled(bindings((11, "Q1379241"), (11, "Lucas"))),
                         {11: ["Lucas"]})

    def test_a_body_that_is_not_a_sparql_result_is_refused(self):
        """WDQS maintenance HTML and a proxy error page both arrive as 200s. "No bindings" and "not a
        result" mean opposite things to a resumable scrape."""
        with self.assertRaises(wikidata.WikidataError):
            wikidata.parse_labelled(b"<html>Service temporarily unavailable</html>")


class StrippedGenre(unittest.TestCase):
    def test_the_medium_is_stripped_off_a_genre(self):
        self.assertEqual(wikidata.stripped_genre("science fiction film"), "science fiction")
        self.assertEqual(wikidata.stripped_genre("drama television series"), "drama")
        self.assertEqual(wikidata.stripped_genre("Thriller Film"), "thriller")

    def test_the_late_additions_are_still_there(self):
        """`television program`, `television` and `anime and manga` were missing once, which left 38 genre
        Q-ids across 1,014 references unmatched against a TMDB vocabulary that does contain them."""
        self.assertEqual(wikidata.stripped_genre("reality television"), "reality")
        self.assertEqual(wikidata.stripped_genre("drama television program"), "drama")
        self.assertEqual(wikidata.stripped_genre("science fiction anime and manga"), "science fiction")

    def test_fiction_is_stripped_only_when_something_real_survives(self):
        """Otherwise "science fiction" becomes "science", which is not a TMDB genre and not a thing. And
        the guard is a suffix rather than an equality: "hard science fiction" and "military science
        fiction" are real referenced genres that an equality guard mangles."""
        self.assertEqual(wikidata.stripped_genre("crime fiction"), "crime")
        self.assertEqual(wikidata.stripped_genre("science fiction"), "science fiction")
        self.assertEqual(wikidata.stripped_genre("hard science fiction"), "hard science fiction")
        self.assertEqual(wikidata.stripped_genre("military science fiction"), "military science fiction")

    def test_a_label_that_is_only_a_medium_says_nothing(self):
        """Every film is a film. The caller drops the empty, which is how "film" and "television series"
        stop reaching the composed document as genres."""
        self.assertEqual(wikidata.stripped_genre("film"), "")
        self.assertEqual(wikidata.stripped_genre("television series"), "")

    def test_a_label_with_no_medium_is_unchanged(self):
        self.assertEqual(wikidata.stripped_genre("cyberpunk"), "cyberpunk")


class Facts(unittest.TestCase):
    """`doc_facts` over a stubbed property fetch."""

    def setUp(self):
        self.original = wikidata.fetch_property
        self.answers = {}
        wikidata.fetch_property = lambda ids, media, prop, cache=None: self.answers.get(prop, {})

    def tearDown(self):
        wikidata.fetch_property = self.original

    def test_it_pairs_the_two_properties_per_id(self):
        self.answers = {wikidata.DIRECTOR: {11: ["Lucas"]},
                        wikidata.GENRE: {11: ["space opera film"], 12: ["drama film"]}}
        self.assertEqual(wikidata.doc_facts([11, 12], "movie"),
                         {11: {"directors": ["Lucas"], "genres": ["space opera"]},
                          12: {"directors": [], "genres": ["drama"]}})

    def test_an_id_wikidata_states_neither_for_is_absent(self):
        """Absent, not empty: unknown is not "none", and the caller is what decides how to record it."""
        self.answers = {wikidata.DIRECTOR: {11: ["Lucas"]}, wikidata.GENRE: {}}
        self.assertEqual(sorted(wikidata.doc_facts([11, 12], "movie")), [11])

    def test_a_genre_that_strips_to_nothing_does_not_count_as_a_genre(self):
        self.answers = {wikidata.DIRECTOR: {}, wikidata.GENRE: {11: ["film"]}}
        self.assertEqual(wikidata.doc_facts([11], "movie"), {})

    def test_genres_are_deduplicated_and_sorted(self):
        """Two Wikidata labels can strip to one genre, and the composed document must not say it twice."""
        self.answers = {wikidata.DIRECTOR: {},
                        wikidata.GENRE: {11: ["comedy film", "comedy television series", "drama film"]}}
        self.assertEqual(wikidata.doc_facts([11], "movie")[11]["genres"], ["comedy", "drama"])

    def test_an_empty_batch_asks_nothing(self):
        self.assertEqual(wikidata.doc_facts([], "movie"), {})


if __name__ == "__main__":
    unittest.main()
