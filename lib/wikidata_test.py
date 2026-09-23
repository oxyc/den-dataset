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
        wikidata.fetch_property = lambda ids, media, prop, cache=None, excluded=None: self.answers.get(prop, {})

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


class Wikipedias(unittest.TestCase):
    """The admission gate's lookup: how many Wikipedias have an article on each title TMDB's count leaves
    short."""

    def test_the_query_matches_the_media_and_counts_wikipedia_sitelinks_only(self):
        query = wikidata.wikipedias_query([12, 11, 11], "tv")
        self.assertIn('VALUES ?tmdb { "11" "12" }', query, "sorted and unique, so one set is one key")
        self.assertIn("wdt:P4983 ?tmdb", query)
        self.assertIn("COUNT(DISTINCT ?article)", query)
        self.assertIn('".wikipedia.org/"', query, "Commons, Wikiquote and Wikisource are not Wikipedias")
        self.assertIn("OPTIONAL", query, "an item with no article is a row counting 0, not a missing row")

    def test_the_count_is_read_per_title_and_a_malformed_one_is_refused(self):
        self.assertEqual(wikidata.parse_wikipedias(rows({"tmdb": cell("1"), "wikis": cell("22")},
                                                        {"tmdb": cell("2"), "wikis": cell("0")})), {1: 22, 2: 0})
        with self.assertRaises(wikidata.WikidataError):
            wikidata.parse_wikipedias(rows({"tmdb": cell("1"), "wikis": cell("many")}))

    def test_a_body_that_is_not_a_result_is_refused_and_not_kept(self):
        """Read as no bindings, a WDQS maintenance page would judge the whole batch on TMDB alone."""
        with tempfile.TemporaryDirectory() as directory:
            cache = caching.ResponseCache("wiki", directory, 3600)
            key = cache.key("sparql-wikipedias", {"q": wikidata.wikipedias_query([1], "movie"), "day": "d1"})
            with mock.patch.object(wikidata.http, "request", return_value=b"<html>busy</html>"):
                with self.assertRaises(wikidata.WikidataError):
                    wikidata.wikipedias([1], "movie", "d1", cache)
            self.assertIsNone(cache.read(key))
            answer = rows({"tmdb": cell("1"), "wikis": cell("7")})
            with mock.patch.object(wikidata.http, "request", return_value=answer) as sent:
                self.assertEqual(wikidata.wikipedias([1], "movie", "d1", cache), {1: 7})
                self.assertEqual(wikidata.wikipedias([1], "movie", "d1", cache), {1: 7})
            self.assertEqual(sent.call_count, 1, "a real answer is served from disk the same day")

    def test_the_next_day_asks_again(self):
        """The count moves, and a below-floor title is judged again daily in a batch much like yesterday's,
        so a cache keyed on the query alone would answer it with the same count for 180 days."""
        with tempfile.TemporaryDirectory() as directory:
            cache = caching.ResponseCache("wiki", directory, 3600)
            with mock.patch.object(wikidata.http, "request",
                                   return_value=rows({"tmdb": cell("1"), "wikis": cell("4")})) as sent:
                wikidata.wikipedias([1], "movie", "d1", cache)
                wikidata.wikipedias([1], "movie", "d2", cache)
            self.assertEqual(sent.call_count, 2)

    def test_no_ids_asks_nothing(self):
        with mock.patch.object(wikidata.http, "request") as sent:
            self.assertEqual(wikidata.wikipedias([], "movie", "d1"), {})
        sent.assert_not_called()


class Kinds(unittest.TestCase):
    """What a title IS — its P136 genres and its P31 types — which is what the anime exclusion reads."""

    def test_both_properties_ride_one_request_as_a_union(self):
        """Two multi-valued OPTIONALs return their cross product, which is why `doc_facts` sends one
        request per property. A UNION is a disjunction: each value is a row and the sets concatenate."""
        query = wikidata.kind_query([12, 11, 11], "movie")
        self.assertIn('VALUES ?tmdb { "11" "12" }', query, "sorted and unique, so one set is one key")
        self.assertIn("{ ?film wdt:P136 ?v . } UNION { ?film wdt:P31 ?v . }", query)
        self.assertNotIn("OPTIONAL", query)
        self.assertIn("wdt:P4983 ?tmdb", wikidata.kind_query([1], "tv"))

    def test_it_asks_for_labels_and_the_service_covers_en_and_mul(self):
        """Wikidata mints a new `<genre> anime and manga` item whenever an editor needs one, so a caller
        matching pinned Q-ids silently stops seeing the ones made after it was written."""
        self.assertIn("?vLabel", wikidata.kind_query([1], "movie"))
        self.assertIn('wikibase:language "en,mul"', wikidata.kind_query([1], "movie"))

    def test_every_label_a_title_carries_accumulates_and_a_bare_qid_is_dropped(self):
        payload = bindings((1, "anime television series"), (1, "Q123"), (1, "adventure anime and manga"),
                           (2, "film"))
        self.assertEqual(wikidata.parse_labelled(payload),
                         {1: ["anime television series", "adventure anime and manga"], 2: ["film"]})

    def test_a_body_that_is_not_a_result_is_refused_and_not_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = caching.ResponseCache("wiki", directory, 3600)
            with mock.patch.object(wikidata.http, "request", return_value=b"<html>busy</html>"):
                with self.assertRaises(wikidata.WikidataError):
                    wikidata.kinds([1], "movie", cache)
            self.assertIsNone(cache.read(cache.key("sparql-kind", {"q": wikidata.kind_query([1], "movie")})))
            with mock.patch.object(wikidata.http, "request", return_value=bindings((1, "anime film"))) as sent:
                self.assertEqual(wikidata.kinds([1], "movie", cache), {1: ["anime film"]})
                self.assertEqual(wikidata.kinds([1], "movie", cache), {1: ["anime film"]})
            self.assertEqual(sent.call_count, 1, "a real answer is served from disk the second time")

    def test_no_ids_asks_nothing(self):
        with mock.patch.object(wikidata.http, "request") as sent:
            self.assertEqual(wikidata.kinds([], "movie"), {})
        sent.assert_not_called()


class Languages(unittest.TestCase):
    """What a title is IN — P364 — which orders the other-language plot fallback."""

    def test_the_query_matches_the_media_and_resolves_the_code_through_p218(self):
        """A language item is not a code. P218 is ISO 639-1, which is what a Wikipedia sitelink is keyed by."""
        film, series = wikidata.language_query([12, 11, 11], "movie"), wikidata.language_query([1], "tv")
        self.assertIn('VALUES ?tmdb { "11" "12" }', film, "sorted and unique, so one set is one key")
        self.assertIn("wdt:P4947 ?tmdb", film)
        self.assertIn("wdt:P4983 ?tmdb", series)
        self.assertIn("wdt:P364 ?v", film)
        self.assertIn("wdt:P218 ?code", film)

    def test_it_is_not_asked_on_the_mapping_query(self):
        """The mapping's text is its cache key and ~770 of its bodies are on disk; a line added to it
        re-asks WDQS for every one of them."""
        self.assertNotIn("P364", wikidata.mapping_query([1], "movie", LANGUAGES))

    def test_every_code_is_kept_lower_cased_and_sorted(self):
        """A co-production states several, where TMDB names one. The caller tries each of them before the
        wikis the title says nothing about."""
        payload = rows({"tmdb": cell("1"), "code": cell("SV")}, {"tmdb": cell("1"), "code": cell("fr")},
                       {"tmdb": cell("1"), "code": cell("sv")}, {"tmdb": cell("2"), "code": cell("")})
        self.assertEqual(wikidata.parse_languages(payload), {1: ["fr", "sv"]})

    def test_a_body_that_is_not_a_result_is_refused_and_not_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = caching.ResponseCache("wiki", directory, 3600)
            with mock.patch.object(wikidata.http, "request", return_value=b"<html>busy</html>"):
                with self.assertRaises(wikidata.WikidataError):
                    wikidata.languages([1], "movie", cache)
            self.assertIsNone(cache.read(cache.key("sparql-language",
                                                   {"q": wikidata.language_query([1], "movie")})))
            answer = rows({"tmdb": cell("1"), "code": cell("de")})
            with mock.patch.object(wikidata.http, "request", return_value=answer) as sent:
                self.assertEqual(wikidata.languages([1], "movie", cache), {1: ["de"]})
                self.assertEqual(wikidata.languages([1], "movie", cache), {1: ["de"]})
            self.assertEqual(sent.call_count, 1, "a real answer is served from disk the second time")

    def test_no_ids_asks_nothing(self):
        with mock.patch.object(wikidata.http, "request") as sent:
            self.assertEqual(wikidata.languages([], "movie"), {})
        sent.assert_not_called()


class Sources(unittest.TestCase):
    """Whether each P144 work a title is based on is a film or a series — what the source-work fallback
    refuses to read."""

    def test_the_query_matches_the_media_and_walks_the_screen_classes_up_from_the_work(self):
        film, series = wikidata.source_query([12, 11, 11], "movie"), wikidata.source_query([1], "tv")
        self.assertIn('VALUES ?tmdb { "11" "12" }', film, "sorted and unique, so one set is one key")
        self.assertIn("wdt:P4947 ?tmdb", film)
        self.assertIn("wdt:P4983 ?tmdb", series)
        self.assertIn("?film wdt:P144 ?basedOn", film)
        self.assertIn("schema:isPartOf <https://en.wikipedia.org/>", film,
                      "the English article, which is the name the mapping carries")
        self.assertIn("?basedOn wdt:P31/wdt:P279* ?class", film)
        for qid in ("Q11424", "Q15416", "Q526877"):   # film, television program, web series
            self.assertIn(f"wd:{qid}", film)

    def test_it_is_not_asked_on_the_mapping_query(self):
        """The mapping's text is its cache key; a line added to it re-asks WDQS for every body on disk."""
        self.assertNotIn("P279", wikidata.mapping_query([1], "movie", LANGUAGES))

    def test_each_article_says_whether_it_is_a_screen_work(self):
        """An article two works share is a screen work when either is."""
        url = "https://en.wikipedia.org/wiki/"
        payload = rows(
            {"tmdb": cell("1"), "sourceArticle": cell(url + "The_Office_(British_TV_series)"), "screen": cell("true")},
            {"tmdb": cell("2"), "sourceArticle": cell(url + "The_Wonderful_Wizard_of_Oz"), "screen": cell("false")},
            {"tmdb": cell("2"), "sourceArticle": cell(url + "The_Wizard_of_Oz_(1939_film)"), "screen": cell("true")},
            {"tmdb": cell("3"), "sourceArticle": cell(url + "Shared"), "screen": cell("true")},
            {"tmdb": cell("3"), "sourceArticle": cell(url + "Shared"), "screen": cell("false")})
        self.assertEqual(wikidata.parse_sources(payload), {
            1: {"The Office (British TV series)": True},
            2: {"The Wonderful Wizard of Oz": False, "The Wizard of Oz (1939 film)": True},
            3: {"Shared": True}})

    def test_a_body_that_is_not_a_result_is_refused_and_not_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = caching.ResponseCache("wiki", directory, 3600)
            with mock.patch.object(wikidata.http, "request", return_value=b"<html>busy</html>"):
                with self.assertRaises(wikidata.WikidataError):
                    wikidata.sources([1], "movie", cache)
            self.assertIsNone(cache.read(cache.key("sparql-source", {"q": wikidata.source_query([1], "movie")})))
            answer = rows({"tmdb": cell("1"), "sourceArticle": cell("https://en.wikipedia.org/wiki/F"),
                           "screen": cell("false")})
            with mock.patch.object(wikidata.http, "request", return_value=answer) as sent:
                self.assertEqual(wikidata.sources([1], "movie", cache), {1: {"F": False}})
                self.assertEqual(wikidata.sources([1], "movie", cache), {1: {"F": False}})
            self.assertEqual(sent.call_count, 1, "a real answer is served from disk the second time")
            self.assertEqual(cache.read(cache.key("sparql-source", {"q": wikidata.source_query([1], "movie")})),
                             answer, "under its own namespace")

    def test_no_ids_asks_nothing(self):
        with mock.patch.object(wikidata.http, "request") as sent:
            self.assertEqual(wikidata.sources([], "movie"), {})
        sent.assert_not_called()


class Targets(unittest.TestCase):
    """What names a title to the classify pass — Wikidata's label and year, in place of TMDB's."""

    def test_the_query_matches_the_media_and_a_series_asks_for_its_start(self):
        film, series = wikidata.target_query([12, 11, 11], "movie"), wikidata.target_query([1], "tv")
        self.assertIn('VALUES ?tmdb { "11" "12" }', film, "sorted and unique, so one set is one key")
        self.assertIn("wdt:P4947 ?tmdb", film)
        self.assertIn("wdt:P577 ?released", film)
        self.assertNotIn("P580", film)
        self.assertIn("wdt:P4983 ?tmdb", series)
        self.assertIn("wdt:P580 ?start", series)
        self.assertIn('wikibase:language "en,mul"', film)

    def test_the_earliest_year_and_a_series_start_wins(self):
        payload = rows(
            {"tmdb": cell("1"), "filmLabel": cell("Solaris"), "released": cell("1972-05-13T00:00:00Z")},
            {"tmdb": cell("1"), "filmLabel": cell("Solaris"), "released": cell("+1972-03-20T00:00:00Z")},
            {"tmdb": cell("1"), "filmLabel": cell("Solaris"), "released": cell("1973-01-01T00:00:00Z")},
            {"tmdb": cell("2"), "filmLabel": cell("The Wire"), "released": cell("2001-01-01T00:00:00Z"),
             "start": cell("2002-06-02T00:00:00Z")})
        self.assertEqual(wikidata.parse_targets(payload),
                         {1: {"title": "Solaris", "year": 1972}, 2: {"title": "The Wire", "year": 2002}})

    def test_a_bare_qid_is_no_title_and_an_item_with_nothing_is_present_and_empty(self):
        """The label service answers the Q-id when the item has no `en` or `mul` label: an identifier, not a
        name. An unknown or blank date is no year, not year zero."""
        payload = rows({"tmdb": cell("3"), "filmLabel": cell("Q123"), "released": cell("t2891")})
        self.assertEqual(wikidata.parse_targets(payload), {3: {"title": None, "year": None}})

    def test_a_body_that_is_not_a_result_is_refused_and_not_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = caching.ResponseCache("wiki", directory, 3600)
            with mock.patch.object(wikidata.http, "request", return_value=b"<html>busy</html>"):
                with self.assertRaises(wikidata.WikidataError):
                    wikidata.targets([1], "movie", cache)
            self.assertIsNone(cache.read(cache.key("sparql-target", {"q": wikidata.target_query([1], "movie")})))
            answer = rows({"tmdb": cell("1"), "filmLabel": cell("F")})
            with mock.patch.object(wikidata.http, "request", return_value=answer) as sent:
                self.assertEqual(wikidata.targets([1], "movie", cache), {1: {"title": "F", "year": None}})
                self.assertEqual(wikidata.targets([1], "movie", cache), {1: {"title": "F", "year": None}})
            self.assertEqual(sent.call_count, 1, "a real answer is served from disk the second time")

    def test_no_ids_asks_nothing(self):
        with mock.patch.object(wikidata.http, "request") as sent:
            self.assertEqual(wikidata.targets([], "movie"), {})
        sent.assert_not_called()


BONN, BOON = "Q116226000", "Q132860965"
ENTITY = "http://www.wikidata.org/entity/"
#: Series 2559 as Wikidata states it: Bonn (2023) also states its own id 215780 and carries Boon's 1986
#: start date; Boon states nothing but the id and its IMDb id.
EVIDENCE = {BONN: {"imdb": ["tt13905034"], "years": [1986, 2022], "claims": [2559, 215780], "articles": []},
            BOON: {"imdb": ["tt0090400"], "years": [], "claims": [2559], "articles": ["Boon (TV series)"]}}


class OneItemPerTitle(unittest.TestCase):
    """Two items can state one TMDB id, and every per-title query is keyed by that id — so without a choice,
    a title's fields came from both works: series 2559 shipped Bonn's name beside Boon's IMDb id."""

    def test_tmdbs_imdb_id_picks_the_item_whatever_order_the_claimants_arrive_in(self):
        for order in ([BONN, BOON], [BOON, BONN]):
            self.assertEqual(wikidata.choose(order, EVIDENCE, {"imdb": "tt0090400", "year": 1986}), (BOON, "imdb"))

    def test_the_year_alone_would_pick_the_wrong_item_so_it_comes_second(self):
        """Bonn carries Boon's 1986 start date; with no IMDb id from TMDB the year narrows to it."""
        self.assertEqual(wikidata.choose([BOON, BONN], EVIDENCE, {"imdb": None, "year": 1986}), (BONN, "year"))

    def test_the_item_claiming_no_other_tmdb_id_decides_when_tmdb_says_nothing(self):
        self.assertEqual(wikidata.choose([BONN, BOON], EVIDENCE, {}), (BOON, "sole-claim"))

    def test_nothing_that_singles_one_out_chooses_none(self):
        """"Charité" and "Charité at War" both state series 70837, TMDB's IMDb id and year, and an article."""
        evidence = {"Q1": {"imdb": ["tt1"], "years": [1990], "claims": [5], "articles": ["A"]},
                    "Q2": {"imdb": ["tt1"], "years": [1990], "claims": [5], "articles": ["B"]}}
        for order in (["Q1", "Q2"], ["Q2", "Q1"]):
            self.assertEqual(wikidata.choose(order, evidence, {"imdb": "tt1", "year": 1990}), (None, "ambiguous"))

    def test_the_item_with_an_article_beats_its_season_or_stub(self):
        """Film 25623, House (1977): a second, unlinked item states the same IMDb id and year."""
        evidence = {"Q1132905": {"imdb": ["tt0076162"], "years": [1977], "claims": [25623],
                                 "articles": ["House (1977 film)"]},
                    "Q64879501": {"imdb": ["tt0076162"], "years": [1977], "claims": [25623], "articles": []}}
        self.assertEqual(wikidata.choose(["Q64879501", "Q1132905"], evidence, {"imdb": "tt0076162", "year": 1977}),
                         ("Q1132905", "article"))

    def test_a_rule_that_holds_for_none_does_not_narrow(self):
        """TMDB's IMDb id matching neither item says nothing; the year still decides."""
        evidence = {"Q1": {"imdb": ["tt1"], "years": [1990], "claims": [5, 6]},
                    "Q2": {"imdb": ["tt2"], "years": [2001], "claims": [5, 7]}}
        self.assertEqual(wikidata.choose(["Q1", "Q2"], evidence, {"imdb": "tt9", "year": 2001}), ("Q2", "year"))

    def test_resolve_chooses_for_contested_ids_only_and_asks_tmdb_only_about_them(self):
        asked = []
        with mock.patch.object(wikidata, "claimants", return_value={2559: [BONN, BOON], 1399: ["Q23572"]}), \
                mock.patch.object(wikidata, "item_evidence", return_value=EVIDENCE) as evidence:
            resolved = wikidata.resolve([2559, 1399, 7], "tv", None,
                                        lambda i: asked.append(i) or {"imdb": "tt0090400", "year": 1986})
        self.assertEqual(asked, [2559])
        evidence.assert_called_once_with([BONN, BOON], "tv", None)
        self.assertEqual(resolved, {2559: {"item": BOON, "candidates": [BONN, BOON], "rule": "imdb"},
                                    1399: {"item": "Q23572"}})
        self.assertEqual(wikidata.set_aside(resolved), {2559: [BONN]})
        self.assertEqual(wikidata.provenance(resolved[2559]),
                         {"wikidataItem": BOON, "wikidataCandidates": [BONN, BOON]})
        self.assertEqual(wikidata.provenance(resolved[1399]), {}, "an uncontested row is unchanged")

    def resolve_with(self, claimed, decisions, ids=(6618,)):
        with mock.patch.object(wikidata, "claimants", return_value=claimed), \
                mock.patch.object(wikidata, "item_evidence", return_value={}) as evidence:
            return wikidata.resolve(list(ids), "tv", None, lambda i: {}, decisions), evidence

    def test_a_committed_decision_chooses_before_the_rules_and_asks_no_evidence(self):
        """Total Drama (the franchise) and Total Drama Island both state series 6618, TMDB's IMDb id and
        year, and an article; no rule tells them apart, and without a decision the title has no card."""
        resolved, evidence = self.resolve_with({6618: ["Q754334", "Q116195"]}, {("tv", 6618): "Q754334"})
        self.assertEqual(resolved[6618], {"item": "Q754334", "candidates": ["Q116195", "Q754334"],
                                          "rule": "decision"})
        evidence.assert_not_called()

    def test_a_decision_for_an_id_no_longer_contested_is_refused(self):
        """A stale entry: someone fixed Wikidata, and the judgement nobody re-made must not linger."""
        with self.assertRaisesRegex(wikidata.DecisionError, "stale"):
            self.resolve_with({6618: ["Q754334"]}, {("tv", 6618): "Q754334"})
        with self.assertRaisesRegex(wikidata.DecisionError, "stale"):
            self.resolve_with({}, {("tv", 6618): "Q754334"})

    def test_a_decision_for_an_item_that_no_longer_claims_the_id_is_refused(self):
        with self.assertRaisesRegex(wikidata.DecisionError, "no longer one of its claimants"):
            self.resolve_with({6618: ["Q116195", "Q9"]}, {("tv", 6618): "Q754334"})

    def test_a_decision_about_an_id_outside_the_batch_is_not_asked_about(self):
        resolved, _ = self.resolve_with({7: ["Q1"]}, {("tv", 6618): "Q754334"}, ids=(7,))
        self.assertEqual(resolved, {7: {"item": "Q1"}})

    def test_the_committed_decisions_are_well_formed(self):
        with open(wikidata.DECISIONS, encoding="utf-8") as handle:
            rows = json.load(handle)["decisions"]
        keys = [(row["mediaType"], row["tmdbId"]) for row in rows]
        self.assertEqual(len(keys), len(set(keys)), "one decision per title")
        for row in rows:
            self.assertIn(row["mediaType"], ("movie", "tv"))
            self.assertIsInstance(row["tmdbId"], int)
            self.assertRegex(row["item"], r"^Q\d+$")
            self.assertTrue(row["why"].strip() and row["upstream"].strip(), row)
        self.assertEqual(wikidata.load_decisions()[("tv", 6618)], "Q754334")

    def test_an_ambiguous_title_sets_every_claimant_aside(self):
        resolved = {5: {"item": None, "candidates": ["Q1", "Q2"], "rule": "ambiguous"}}
        self.assertEqual(wikidata.set_aside(resolved), {5: ["Q1", "Q2"]})
        self.assertEqual(wikidata.provenance(resolved[5]), {"wikidataCandidates": ["Q1", "Q2"]})

    def test_the_claimants_are_sorted_by_qid_number(self):
        payload = rows({"tmdb": cell("2559"), "film": cell(ENTITY + BOON)},
                       {"tmdb": cell("2559"), "film": cell(ENTITY + BONN)},
                       {"tmdb": cell("1"), "film": cell(ENTITY + "Q99")}, {"tmdb": cell("1"), "film": cell(ENTITY + "Q100")})
        self.assertEqual(wikidata.parse_claimants(payload), {2559: [BONN, BOON], 1: ["Q99", "Q100"]})

    def test_the_evidence_is_every_imdb_id_year_and_tmdb_id_an_item_states(self):
        payload = rows({"film": cell(ENTITY + BONN), "claim": cell("215780")},
                       {"film": cell(ENTITY + BONN), "claim": cell("2559")},
                       {"film": cell(ENTITY + BONN), "date": cell("2022-10-22T00:00:00Z")},
                       {"film": cell(ENTITY + BONN), "date": cell("1986-01-14T00:00:00Z")},
                       {"film": cell(ENTITY + BONN), "imdb": cell("tt13905034")},
                       {"film": cell(ENTITY + BOON), "claim": cell("2559")},
                       {"film": cell(ENTITY + BOON), "imdb": cell("tt0090400")},
                       {"film": cell(ENTITY + BOON), "article": cell("https://en.wikipedia.org/wiki/Boon_(TV_series)")})
        self.assertEqual(wikidata.parse_evidence(payload), EVIDENCE)
        self.assertIn("wdt:P580 ?date", wikidata.evidence_query([BONN], "tv"))
        self.assertNotIn("P580", wikidata.evidence_query([BONN], "movie"))
        self.assertIn("wdt:P4983 ?claim", wikidata.evidence_query([BONN], "tv"))

    def test_every_per_title_query_leaves_the_set_aside_items_out_pair_by_pair(self):
        excluded = {2559: [BONN]}
        minus = '  MINUS { VALUES (?tmdb ?film) { ("2559" wd:Q116226000) } }'
        for query in (wikidata.query_text([2559, 3], "tv", "P57", excluded),
                      wikidata.mapping_query([2559, 3], "tv", LANGUAGES, excluded),
                      wikidata.wikipedias_query([2559, 3], "tv", excluded),
                      wikidata.kind_query([2559, 3], "tv", excluded),
                      wikidata.language_query([2559, 3], "tv", excluded),
                      wikidata.source_query([2559, 3], "tv", excluded), wikidata.target_query([2559, 3], "tv", excluded)):
            self.assertIn("?film wdt:P4983 ?tmdb .\n" + minus + "\n", query)

    def test_a_batch_with_no_contested_id_sends_the_text_already_on_disk(self):
        """The query text is the cache key; a batch the choice does not touch must not be asked again."""
        for build in (lambda e: wikidata.query_text([3], "tv", "P57", e),
                      lambda e: wikidata.mapping_query([3], "tv", LANGUAGES, e),
                      lambda e: wikidata.wikipedias_query([3], "tv", e), lambda e: wikidata.kind_query([3], "tv", e),
                      lambda e: wikidata.language_query([3], "tv", e), lambda e: wikidata.source_query([3], "tv", e),
                      lambda e: wikidata.target_query([3], "tv", e)):
            self.assertEqual(build({2559: [BONN]}), build(None))


if __name__ == "__main__":
    unittest.main()
