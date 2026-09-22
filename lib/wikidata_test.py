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
import hashlib
import json
import tempfile
import unittest
from unittest import mock

from . import cache as caching
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

    def test_the_text_is_the_swift_passs_byte_for_byte(self):
        """It is hashed into the cache key, so a second spelling of one question is a second scrape of
        ~770 batches. This is `WikipediaSource.docFacts`'s multi-line literal as Swift renders it: two-space
        indent, and NO trailing newline — the closing delimiter sits on its own line."""
        self.assertEqual(wikidata.query_text([11, 12], "movie", "P57"),
                         'SELECT ?tmdb ?vLabel WHERE {\n'
                         '  VALUES ?tmdb { "11" "12" }\n'
                         '  ?film wdt:P4947 ?tmdb .\n'
                         '  ?film wdt:P57 ?v .\n'
                         '  SERVICE wikibase:label { bd:serviceParam wikibase:language "en,mul". }\n'
                         '}\n'
                         'ORDER BY ?tmdb ?vLabel')


class Fetch(unittest.TestCase):
    """`fetch_property` against a real cache directory, with the request stubbed."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = caching.ResponseCache("wiki", self.directory.name, 3600)
        self.requests = 0
        patch = mock.patch.object(wikidata.http, "request", self.answer)
        patch.start()
        self.addCleanup(patch.stop)

    def answer(self, host, path, params=None, **kwargs):
        self.requests += 1
        return self.payload

    def test_a_body_that_does_not_parse_is_not_kept(self):
        """A WDQS maintenance page arrives as a 200. Kept, it outlives the outage by the whole TTL, and
        every batch it answers reads as a batch of films with no director and no genre."""
        self.payload = b"<html>Service temporarily unavailable</html>"
        with self.assertRaises(wikidata.WikidataError):
            wikidata.fetch_property([11], "movie", wikidata.DIRECTOR, self.cache)
        key = self.cache.key("sparql-docfacts", {"q": wikidata.query_text([11], "movie", wikidata.DIRECTOR)})
        self.assertIsNone(self.cache.read(key))

    def test_a_result_is_kept_under_the_query_text_and_served_from_disk(self):
        self.payload = bindings((11, "Lucas"))
        first = wikidata.fetch_property([11], "movie", wikidata.DIRECTOR, self.cache)
        second = wikidata.fetch_property([11], "movie", wikidata.DIRECTOR, self.cache)
        self.assertEqual((self.requests, first, second), (1, {11: ["Lucas"]}, {11: ["Lucas"]}))


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


def rows(*bindings_):
    return json.dumps({"results": {"bindings": list(bindings_)}}).encode()


def cell(value):
    return {"type": "literal", "value": value}


#: sha256 of `mapping_query([603, 27205], "movie", <the twelve plot languages>)`. The generator that produces
#: it was checked against the Swift pass's own cache: over out-repass's 199 enriched batches, the keys it
#: derives name 180 SPARQL bodies already on disk — every batch whose survivors were the whole mapping set.
#: One changed character in the text, a comment line included, moves every one of those keys.
MAPPING_DIGEST = "92f9932aa1dc2704b20a4562a080b6b48db03c25a5ec937ef0b9b44f7cdc87c3"
LANGUAGES = ("da", "de", "es", "fr", "it", "ja", "ko", "nl", "pl", "pt", "ru", "sv")


class Mapping(unittest.TestCase):
    def test_the_query_text_is_the_one_the_cache_is_keyed_on(self):
        query = wikidata.mapping_query([27205, 603, 603], "movie", LANGUAGES)
        self.assertEqual(hashlib.sha256(query.encode()).hexdigest(), MAPPING_DIGEST)
        self.assertIn('VALUES ?tmdb { "603" "27205" }', query, "sorted and unique, as Swift sent them")
        self.assertTrue(query.endswith("ORDER BY ?tmdb ?article"), "no trailing newline")

    def test_a_series_is_mapped_on_the_series_property(self):
        self.assertIn("?film wdt:P4983 ?tmdb .", wikidata.mapping_query([1], "tv", LANGUAGES))

    def test_article_imdb_and_absence(self):
        parsed = wikidata.parse_mapping(rows(
            {"tmdb": cell("27205"), "article": cell("https://en.wikipedia.org/wiki/Inception"),
             "imdb": cell("tt1375666")},
            {"tmdb": cell("603"), "article": cell("https://en.wikipedia.org/wiki/The_Matrix")}))
        self.assertEqual((parsed[27205]["article"], parsed[27205]["imdb"]), ("Inception", "tt1375666"))
        self.assertEqual(parsed[603]["article"], "The Matrix")
        self.assertIsNone(parsed[603]["imdb"])
        self.assertNotIn(999, parsed)

    def test_creators_accumulate_and_the_shortest_runtime_wins(self):
        """A title binds once PER creator — first-wins would drop the second Duffer brother — and a series
        with a 50- and a 70-minute cut answers "have I got time for this" with the 50."""
        parsed = wikidata.parse_mapping(rows(
            {"tmdb": cell("66732"), "runtime": cell("70"), "creatorLabel": cell("Ross Duffer")},
            {"tmdb": cell("66732"), "runtime": cell("49.5"), "creatorLabel": cell("Matt Duffer")}))
        self.assertEqual(parsed[66732]["creators"], ["Matt Duffer", "Ross Duffer"])
        self.assertEqual(parsed[66732]["runtimeMinutes"], 50, "rounded half away from zero, as Swift did")

    def test_other_language_articles_are_keyed_by_their_wiki(self):
        parsed = wikidata.parse_mapping(rows(
            {"tmdb": cell("1"), "anyArticle": cell("https://de.wikipedia.org/wiki/Schachnovelle_(2021)"),
             "anySite": cell("https://de.wikipedia.org/")},
            {"tmdb": cell("1"), "anyArticle": cell("https://en.wikipedia.org/wiki/Chess_Story"),
             "anySite": cell("https://en.wikipedia.org/")}))
        self.assertEqual(parsed[1]["articlesByLang"], {"de": "Schachnovelle (2021)"},
                         "English is the own article, never a fallback")

    def test_the_source_work_is_carried_apart_from_the_own_article(self):
        parsed = wikidata.parse_mapping(rows(
            {"tmdb": cell("125988"), "article": cell("https://en.wikipedia.org/wiki/Silo_(TV_series)"),
             "sourceArticle": cell("https://en.wikipedia.org/wiki/Wool_(novel)")}))
        self.assertEqual((parsed[125988]["article"], parsed[125988]["sourceArticle"]),
                         ("Silo (TV series)", "Wool (novel)"))

    def test_zero_bindings_is_an_answer_and_an_undecodable_body_is_not(self):
        """The first is "these titles have no article" and is recorded; the second must retry the batch. A
        WDQS maintenance page read as "no bindings" made a whole batch plotless and checkpointed."""
        self.assertEqual(wikidata.parse_mapping(rows()), {})
        for body in (b"<html>service unavailable</html>", b"", b'{"results":{}}'):
            with self.assertRaises(wikidata.WikidataError):
                wikidata.parse_mapping(body)

    def test_an_article_name_is_percent_decoded_unless_the_escape_is_broken(self):
        self.assertEqual(wikidata.article_title("https://en.wikipedia.org/wiki/Am%C3%A9lie"), "Amélie")
        self.assertEqual(wikidata.article_title("https://en.wikipedia.org/wiki/100%_Wolf"), "100% Wolf",
                         "a bare % leaves the whole name undecoded rather than half-decoded")
        self.assertIsNone(wikidata.article_title("https://example.com/no-wiki-path"))

    def test_a_body_that_does_not_parse_is_not_kept(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        cache = caching.ResponseCache("wiki", directory.name, 3600)
        with mock.patch.object(wikidata.http, "request", return_value=b"<html>maintenance</html>"):
            with self.assertRaises(wikidata.WikidataError):
                wikidata.mapping([1], "movie", LANGUAGES, cache)
        key = cache.key("sparql", {"q": wikidata.mapping_query([1], "movie", LANGUAGES)})
        self.assertIsNone(cache.read(key))
        with mock.patch.object(wikidata.http, "request", return_value=rows()) as sent:
            wikidata.mapping([1], "movie", LANGUAGES, cache)
            wikidata.mapping([1], "movie", LANGUAGES, cache)
        self.assertEqual(sent.call_count, 1, "a real answer is served from disk the second time")


if __name__ == "__main__":
    unittest.main()
