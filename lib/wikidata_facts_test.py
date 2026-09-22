#!/usr/bin/env python3
"""The facts sidecar's Wikidata queries: the text (a cache key), and what each answer is turned into.

The query texts below are the Swift's, not a transcription of it: a scrape of 2,000 titles through the
merge-base binary filled an empty cache with 1,600 per-property answers, and this module computed the key
of every one of them — 1,600 of 1,600 — and replayed them without asking WDQS anything (oxyc/den-dataset#27).
"""
import json
import random
import tempfile
import unittest
from unittest import mock

from . import cache as caching
from . import wikidata_facts as wd

SPEC = {item.key: item for item in wd.SPECS}


def body(*rows):
    return json.dumps({"results": {"bindings": [
        {name: {"value": value} for name, value in row.items()} for row in rows]}}).encode()


class Query(unittest.TestCase):
    def test_an_entity_query(self):
        self.assertEqual(wd.facts_query([12, 11, 12], "movie", SPEC["directors"]),
                         'SELECT ?tmdb ?v WHERE {\n  VALUES ?tmdb { "11" "12" }\n  ?film wdt:P4947 ?tmdb .\n'
                         '  ?film wdt:P57 ?v .\n}')

    def test_an_iso_query_reads_the_code_off_the_value(self):
        self.assertEqual(wd.facts_query([5], "tv", SPEC["languages"]),
                         'SELECT ?tmdb ?code WHERE {\n  VALUES ?tmdb { "5" }\n  ?film wdt:P4983 ?tmdb .\n'
                         '  ?film wdt:P364 ?v . ?v wdt:P218 ?code .\n}')

    def test_a_date_query_asks_for_the_precision(self):
        self.assertEqual(wd.facts_query([5], "movie", SPEC["released"]),
                         'SELECT ?tmdb ?v ?prec WHERE {\n  VALUES ?tmdb { "5" }\n  ?film wdt:P4947 ?tmdb .\n'
                         '  ?film p:P577 ?st . ?st psv:P577 ?node . ?node wikibase:timeValue ?v ; '
                         'wikibase:timePrecision ?prec .\n}')


class Parse(unittest.TestCase):
    def test_entities_are_q_ids_deduplicated_and_sorted(self):
        got = wd.parse_facts(body({"tmdb": "1", "v": "http://www.wikidata.org/entity/Q9"},
                                  {"tmdb": "1", "v": "http://www.wikidata.org/entity/Q10"},
                                  {"tmdb": "1", "v": "http://www.wikidata.org/entity/Q9"},
                                  {"tmdb": "1", "v": "http://www.wikidata.org/entity/L5"}), SPEC["cast"])
        self.assertEqual(got, {1: ["Q10", "Q9"]})

    def test_a_single_field_keeps_the_first_in_sorted_order(self):
        """Two IMDb ids on a merged item. A single-element list made every consumer unwrap it."""
        got = wd.parse_facts(body({"tmdb": "1", "v": "tt2"}, {"tmdb": "1", "v": "tt1"}), SPEC["imdbId"])
        self.assertEqual(got, {1: "tt1"})

    def test_a_numeric_field_is_a_number_rounded_half_away_from_zero(self):
        for raw, expected in (("42", 42), ("42.0", 42), ("42.5", 43), ("+7", 7), ("forty", None)):
            got = wd.parse_facts(body({"tmdb": "1", "v": raw}), SPEC["runtimeMinutes"])
            self.assertEqual(got.get(1), expected, raw)

    def test_iso_codes_are_upper_cased(self):
        self.assertEqual(wd.parse_facts(body({"tmdb": "1", "code": "fi"}, {"tmdb": "1", "code": "FI"}),
                                        SPEC["countries"]), {1: ["FI"]})

    def test_a_date_is_the_earliest_cut_to_its_precision(self):
        """A year-precision value is never read as the 1st of January. Earliest by STRING, so a bare year
        sorts before any day in it — the Swift's rule, kept so the shipped file does not move."""
        got = wd.parse_facts(body({"tmdb": "1", "v": "+2001-05-02T00:00:00Z", "prec": "11"},
                                  {"tmdb": "1", "v": "+2001-01-01T00:00:00Z", "prec": "9"},
                                  {"tmdb": "2", "v": "+1999-03-31T00:00:00Z", "prec": "10"},
                                  {"tmdb": "3", "v": "+0800-01-01T00:00:00Z", "prec": "7"}), SPEC["released"])
        self.assertEqual(got, {1: {"date": "2001", "precision": "year"},
                               2: {"date": "1999-03", "precision": "month"}})

    def test_a_body_that_is_not_a_result_is_refused(self):
        with self.assertRaises(wd.WikidataError):
            wd.parse_facts(b"<html>maintenance</html>", SPEC["cast"])


class Fetch(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = caching.ResponseCache("wiki", self.directory.name, 3600)
        self.asked = 0
        patch = mock.patch.object(wd.http, "request", self.answer)
        patch.start()
        self.addCleanup(patch.stop)

    def answer(self, *args, **kwargs):
        self.asked += 1
        return self.payload

    def test_an_answer_is_kept_and_served_from_disk(self):
        self.payload = body({"tmdb": "1", "v": "tt1"})
        first = wd.fetch_facts([1], "movie", SPEC["imdbId"], self.cache)
        second = wd.fetch_facts([1], "movie", SPEC["imdbId"], self.cache)
        self.assertEqual((first, second, self.asked), (({1: "tt1"}, True), ({1: "tt1"}, False), 1))

    def test_a_maintenance_page_is_not_kept(self):
        self.payload = b"<html>maintenance</html>"
        with self.assertRaises(wd.WikidataError):
            wd.fetch_facts([1], "movie", SPEC["imdbId"], self.cache)
        key = self.cache.key(wd.CACHE_PATH, {"q": wd.facts_query([1], "movie", SPEC["imdbId"])})
        self.assertIsNone(self.cache.read(key))


ENTITY = "http://www.wikidata.org/entity/"


class Lookups(unittest.TestCase):
    """The three uncached hops, answered per query: the main request, then the aliases."""

    def setUp(self):
        self.answers = {}
        patch = mock.patch.object(wd, "_sparql", lambda query: self.answers["alias" if "altLabel" in query else "main"])
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_title_strings_do_not_depend_on_row_order(self):
        """WDQS returns the cross product of a title's articles, P1476 values and labels in no fixed order.
        Every order of the same rows gives the same strings: an `en` label over a `mul` one, otherwise the
        least value by code point."""
        rows = [{"tmdb": "1", "article": "https://en.wikipedia.org/wiki/" + article, "orig": orig,
                 "origLang": "ja", "label": label, "labelLang": lang}
                for article in ("Tokyo_Revengers", "Tokyo_Revengers_(TV_series)")
                for orig in ("東京卍リベンジャーズ", "東京リベンジャーズ")
                for label, lang in (("Tokyo Revengers", "mul"), ("Tokyo Revengers (anime)", "en"))]
        for seed in range(12):
            shuffled = list(rows)
            random.Random(seed).shuffle(shuffled)
            with self.subTest(seed=seed):
                self.answers = {"main": body(*shuffled), "alias": body()}
                self.assertEqual(wd.titles([1], "movie", {1: ["JA"]})[1],
                                 {"article": "Tokyo Revengers", "label": "Tokyo Revengers (anime)",
                                  "original": "東京リベンジャーズ", "aliases": []})

    def test_the_original_title_is_the_one_in_the_titles_own_language(self):
        """P1476 also carries translations. Measured: a Swedish film whose P1476 holds the Swedish and a
        German title, and a Ukrainian one with a Finnish — the least value alone picked the translation."""
        rows = [{"tmdb": "1", "orig": "Miraklet i Gullspång", "origLang": "sv"},
                {"tmdb": "1", "orig": "Das Gullspång Geheimnis", "origLang": "de"},
                {"tmdb": "2", "orig": "Віддалений гавкіт собак", "origLang": "uk"},
                {"tmdb": "2", "orig": "Koirien kaukainen haukkuminen", "origLang": "fi"}]
        for seed in range(6):
            shuffled = list(rows)
            random.Random(seed).shuffle(shuffled)
            with self.subTest(seed=seed):
                self.answers = {"main": body(*shuffled), "alias": body()}
                got = wd.titles([1, 2], "movie", {1: ["SV"], 2: ["UK"]})
                self.assertEqual((got[1]["original"], got[2]["original"]),
                                 ("Miraklet i Gullspång", "Віддалений гавкіт собак"))
        self.answers = {"main": body(*rows), "alias": body()}
        self.assertEqual(wd.titles([1], "movie")[1]["original"], "Das Gullspång Geheimnis",
                         "with no language to go on, the least value — deterministic, and no better")

    def test_a_mul_label_is_taken_when_there_is_no_en_one(self):
        self.answers = {"main": body({"tmdb": "1", "label": "Parasite", "labelLang": "mul"},
                                     {"tmdb": "1", "label": "Gisaengchung", "labelLang": "mul"}),
                        "alias": body()}
        self.assertEqual(wd.titles([1], "movie")[1]["label"], "Gisaengchung")

    def test_the_label_query_asks_for_its_language(self):
        """Without it, `en` and `mul` rows cannot be told apart and the rule above has nothing to go on."""
        asked = []
        self.answers = {"main": body(), "alias": body()}
        with mock.patch.object(wd, "_sparql", lambda query: asked.append(query) or body()):
            wd.titles([1], "movie")
        self.assertIn("(LANG(?label) AS ?labelLang) (LANG(?orig) AS ?origLang)", asked[0])

    def test_an_unresolved_entity_label_is_not_a_name(self):
        """The label service answers a miss with the item's own Q-id."""
        self.answers = {"main": body({"item": ENTITY + "Q9", "itemLabel": "Q9", "pid": "55"},
                                     {"item": ENTITY + "Q8", "itemLabel": "Ada Director"}),
                        "alias": body({"item": ENTITY + "Q9", "alias": "Nine"})}
        got = wd.entity_details(["Q9", "Q8"])
        self.assertEqual(got["Q9"], {"name": None, "tmdbPersonId": "55", "aliases": ["Nine"]})
        self.assertEqual(got["Q8"]["name"], "Ada Director")

    def test_an_unlabelled_type_is_not_a_type_name(self):
        """It answers with the TYPE's Q-id — kept, it would fold to `other` and outrank nothing, or stand
        alone as the source's kind."""
        self.answers = {"main": body({"item": ENTITY + "Q30", "type": ENTITY + "Q7725634", "typeLabel": "novel"},
                                     {"item": ENTITY + "Q30", "type": ENTITY + "Q999", "typeLabel": "Q999"},
                                     {"item": ENTITY + "Q31", "type": ENTITY + "Q998", "typeLabel": "Q998"})}
        self.assertEqual(wd.instance_of(["Q30", "Q31"]), {"Q30": ["novel"]})


class Vocabulary(unittest.TestCase):
    """The source-kind cases are the Swift `SourceKindTests`, carried over when the Swift went."""

    def test_literary_types_are_books(self):
        """212 of 400 sampled source works are "literary work" and another 32 "written work"."""
        for label in ("literary work", "written work", "novel", "novella", "memoir", "fairy tale",
                      "short story", "autobiography", "light novel", "  Literary Work  "):
            self.assertEqual(wd.source_kind(label), "book", label)

    def test_unlisted_variants_fall_to_the_right_family(self):
        for label, kind in (("serial novel", "book"), ("mystery novel", "book"),
                            ("children's literature", "book"), ("seinen manga", "comic"), ("radio play", "play"),
                            ("film", "screen"), ("anime television series", "screen"),
                            ("media franchise", "franchise"), ("archaeological site", "other"), ("", "other")):
            self.assertEqual(wd.source_kind(label), kind, label)

    def test_characters_are_not_texts(self):
        """"Based on Batman" is not "based on a novel" — folded together, a books row fills with superheroes."""
        for label in ("film character", "comics character", "fictional human", "superhero team",
                      "television character"):
            self.assertEqual(wd.source_kind(label), "character", label)

    def test_the_strongest_source_kind_wins(self):
        self.assertEqual(wd.strongest_kind(["written work", "literary work"]), "book")
        self.assertEqual(wd.strongest_kind(["archaeological site", "novel"]), "book")
        self.assertEqual(wd.strongest_kind(["film character", "comic book series"]), "comic")
        self.assertEqual(wd.strongest_kind(["archaeological site"]), "other")
        self.assertIsNone(wd.strongest_kind([]))

    def test_a_disambiguator_naming_a_medium_is_dropped(self):
        self.assertEqual(wd.stripped_article_suffix("Solaris (1972 film)"), "Solaris")
        self.assertEqual(wd.stripped_article_suffix("Paris (city)"), "Paris (city)")
        self.assertEqual(wd.stripped_article_suffix("Fargo (TV series)"), "Fargo")

    def test_the_genre_map_matches_exactly_except_for_animation(self):
        """Animation is the one genre with a hide rule behind it, so it is matched by shape; a hybrid is
        not what someone hiding animation means."""
        got = wd.genre_map({"Q1": {"en": "drama"}, "Q2": {"en": "animated sitcom"}, "Q3": {"en": "cyberpunk"},
                            "Q4": {"en": "live-action/animated"}, "Q5": {"en": "history"}, "Q6": {}})
        self.assertEqual(got, {"Q1": {"movie": 18, "tv": 18}, "Q2": {"movie": 16, "tv": 16},
                               "Q5": {"movie": 36}})

    def test_an_article_url_becomes_its_title(self):
        self.assertEqual(wd.article_title("https://en.wikipedia.org/wiki/L%C3%A9on:_The_Professional"),
                         "Léon: The Professional")
        self.assertEqual(wd.article_title("https://en.wikipedia.org/wiki/100%_Wolf"), "100% Wolf")
        self.assertIsNone(wd.article_title("https://en.wikipedia.org/"))


if __name__ == "__main__":
    unittest.main()
