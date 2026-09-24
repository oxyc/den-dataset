#!/usr/bin/env python3
"""The facts sidecar's Wikidata queries: the text (a cache key), and what each answer is turned into.

The query texts below are the Swift's, not a transcription of it: a scrape of 2,000 titles through the
merge-base binary filled an empty cache with 1,600 per-property answers, and this module computed the key
of every one of them — 1,600 of 1,600 — and replayed them without asking WDQS anything (oxyc/den-dataset#27).
"""
import json
import random
import re
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
        """A year-precision value is never read as the 1st of January, and a stated day inside a stated
        year is the finer answer to the same question — not a later date. String order alone put "2001"
        before "2001-05-02", which is the Swift's rule and shipped the year while the day was stated."""
        rows = [{"tmdb": "1", "v": "+2001-05-02T00:00:00Z", "prec": "11"},
                {"tmdb": "1", "v": "+2001-01-01T00:00:00Z", "prec": "9"},
                {"tmdb": "2", "v": "+1999-03-31T00:00:00Z", "prec": "10"},
                {"tmdb": "3", "v": "+0800-01-01T00:00:00Z", "prec": "7"},
                {"tmdb": "4", "v": "+2001-01-01T00:00:00Z", "prec": "9"},
                {"tmdb": "4", "v": "+2000-12-31T00:00:00Z", "prec": "11"},
                {"tmdb": "5", "v": "+1999-05-02T00:00:00Z", "prec": "11"},
                {"tmdb": "5", "v": "+1999-03-01T00:00:00Z", "prec": "10"},
                {"tmdb": "5", "v": "+1999-03-31T00:00:00Z", "prec": "11"},
                {"tmdb": "5", "v": "+1999-01-01T00:00:00Z", "prec": "9"}]
        for seed in range(8):
            shuffled = list(rows)
            random.Random(seed).shuffle(shuffled)
            with self.subTest(seed=seed):
                self.assertEqual(wd.parse_facts(body(*shuffled), SPEC["released"]),
                                 {1: {"date": "2001-05-02", "precision": "day"},
                                  2: {"date": "1999-03", "precision": "month"},
                                  4: {"date": "2000-12-31", "precision": "day"},
                                  5: {"date": "1999-03-31", "precision": "day"}})

    def test_a_body_that_is_not_a_result_is_refused(self):
        with self.assertRaises(wd.WikidataError):
            wd.parse_facts(b"<html>maintenance</html>", SPEC["cast"])

    def test_every_p179_target_is_kept_for_the_series_filter(self):
        """WALL-E's: a critics' list and a series. Collapsed to the least, the list was all that survived."""
        got = wd.parse_facts(body({"tmdb": "1", "v": "http://www.wikidata.org/entity/Q9000"},
                                  {"tmdb": "1", "v": "http://www.wikidata.org/entity/Q26705935"}),
                             SPEC["franchise"])
        self.assertEqual(got, {1: ["Q26705935", "Q9000"]})


class Series(unittest.TestCase):
    LIST, SERIES, CATALOG = "Q26705935", "Q9000", "Q56070713"

    def test_a_list_is_dropped_and_the_most_specific_series_leads_whatever_the_order(self):
        """A studio canon is typed an animated film series and holds every film the studio made; the
        story's own series is fewer members. Equal counts fall to the lower Q-id NUMBER, not string."""
        members = {self.SERIES: 4, self.CATALOG: 67, "Q10": 4}
        for seed in range(6):
            targets = [self.LIST, self.CATALOG, self.SERIES, "Q10"]
            random.Random(seed).shuffle(targets)
            with self.subTest(targets=targets):
                self.assertEqual(wd.franchises(targets, members), ["Q10", self.SERIES, self.CATALOG])
        self.assertEqual(wd.franchises([self.LIST], members), [])

    def test_the_query_walks_the_class_hierarchy_for_the_four_kinds(self):
        query = wd.series_query([self.SERIES, self.LIST, self.SERIES])
        self.assertIn(f"VALUES ?item {{ wd:{self.LIST} wd:{self.SERIES} }}", query)
        self.assertIn("?item wdt:P31/wdt:P279* ?class", query)
        self.assertIn("VALUES ?class { wd:Q24856 wd:Q5398426 wd:Q196600 wd:Q138337574 }", query)

    def test_an_answer_is_cached_per_batch(self):
        asked = []
        payload = body({"item": ENTITY + self.SERIES, "members": "4"})
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(wd, "_sparql", lambda query: asked.append(query) or payload):
            cache = caching.ResponseCache("wiki", directory, 3600)
            first = wd.series([self.SERIES, self.LIST], cache)
            second = wd.series([self.LIST, self.SERIES], cache)
        self.assertEqual((first, second, len(asked)), ({self.SERIES: 4}, {self.SERIES: 4}, 1))


PROP = "http://www.wikidata.org/prop/direct/"


class Awards(unittest.TestCase):
    """What the Academy Award for Best Picture and its neighbours actually carry on Wikidata."""

    def test_part_of_a_group_beats_an_instance_of_one_and_both_beat_the_awarding_body(self):
        self.assertEqual(wd.ceremony({"P361": [("Q19020", True)], "P31": [("Q96474687", False)],
                                      "P1027": [("Q212329", False)]}), "Q19020")
        # A BAFTA category is an instance of the ceremony and of "class of award", which is no group.
        self.assertEqual(wd.ceremony({"P31": [("Q732997", True), ("Q38033430", False)],
                                      "P1027": [("Q159661", False)]}), "Q732997")

    def test_a_group_credited_outright_is_its_own_ceremony(self):
        self.assertEqual(wd.ceremony({"P31": [("Q107655869", False)], "self": [("Q1011547", True)]}),
                         "Q1011547")

    def test_the_awarding_body_stands_in_only_when_no_group_is_found(self):
        self.assertEqual(wd.ceremony({"P31": [("Q4220917", False)], "P1027": [("Q42", False), ("Q7", False)]}),
                         "Q7", "the lowest Q-id number, whatever order the rows came in")
        self.assertIsNone(wd.ceremony({"P31": [("Q107467117", False)]}), "an order of chivalry is no ceremony")

    def test_the_answer_parses_every_link_and_the_group_flag(self):
        got = wd.parse_awards(body(
            {"item": ENTITY + "Q102427", "p": PROP + "P361", "t": ENTITY + "Q19020", "group": "true"},
            {"item": ENTITY + "Q102427", "p": PROP + "P31", "t": ENTITY + "Q96474687", "group": "false"},
            {"item": ENTITY + "Q19020", "p": "self", "t": ENTITY + "Q19020", "group": "true"}))
        self.assertEqual(got, {"Q102427": {"P361": [("Q19020", True)], "P31": [("Q96474687", False)]},
                               "Q19020": {"self": [("Q19020", True)]}})

    def test_the_query_asks_the_three_links_and_the_group_class(self):
        query = wd.award_query(["Q2", "Q1", "Q2"])
        self.assertIn("VALUES ?item { wd:Q1 wd:Q2 }", query)
        self.assertIn("VALUES ?p { wdt:P361 wdt:P31 wdt:P1027 }", query)
        self.assertIn("wdt:P31/wdt:P279* wd:Q107655869", query)


class ImdbIds(unittest.TestCase):
    def test_only_a_persons_id_is_kept_and_the_lowest_of_two(self):
        got = wd.parse_imdb(body({"item": ENTITY + "Q1", "id": "nm0000200"},
                                 {"item": ENTITY + "Q1", "id": "nm0000100"},
                                 {"item": ENTITY + "Q2", "id": "co0000001"},
                                 {"item": ENTITY + "Q3", "id": "ch0000001"}))
        self.assertEqual(got, {"Q1": "nm0000100"})

    def test_an_answer_is_cached_per_batch(self):
        asked = []
        payload = body({"item": ENTITY + "Q1", "id": "nm1"})
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(wd, "_sparql", lambda query: asked.append(query) or payload):
            cache = caching.ResponseCache("wiki", directory, 3600)
            first = wd.imdb_ids(["Q1", "Q2"], cache)
            second = wd.imdb_ids(["Q2", "Q1"], cache)
        self.assertEqual((first, second, len(asked)), ({"Q1": "nm1"}, {"Q1": "nm1"}, 1))


class People(unittest.TestCase):
    """A person's traits (oxyc/den#136), as WDQS answers `people_query`."""

    def row(self, qid, prop, value, prec=None):
        out = {"item": ENTITY + qid, "p": prop, "v": value}
        if prec is not None:
            out["prec"] = str(prec)
        return out

    def test_every_trait_parses_and_a_gender_is_whatever_item_wikidata_names(self):
        got = wd.parse_people(body(
            self.row("Q1", "P21", ENTITY + "Q48270"),
            self.row("Q1", "P27", ENTITY + "Q34"), self.row("Q1", "P27", ENTITY + "Q33"),
            self.row("Q1", "P106", ENTITY + "Q33999"),
            self.row("Q1", "P569", "+1946-06-14T00:00:00Z", 11),
            self.row("Q1", "P570", "+2011-03-00T00:00:00Z", 10),
            # "Unknown value" is a blank node, not an item.
            self.row("Q2", "P21", "http://www.wikidata.org/.well-known/genid/abc123")))
        self.assertEqual(got, {"Q1": {"gender": ["Q48270"], "citizenship": ["Q33", "Q34"],
                                      "occupation": ["Q33999"],
                                      "born": {"date": "1946-06-14", "precision": "day"},
                                      "died": {"date": "2011-03", "precision": "month"}}})

    def test_a_date_keeps_the_precision_wikidata_asserts_and_its_era(self):
        self.assertEqual(wd.person_date("-0496-01-01T00:00:00Z", 9), {"date": "-0496", "precision": "year"})
        self.assertEqual(wd.person_date("+1850-00-00T00:00:00Z", 8), {"date": "1850", "precision": "decade"})
        self.assertIsNone(wd.person_date("+1000-00-00T00:00:00Z", 6), "a millennium is not a birth date")

    def test_a_stated_day_beats_the_year_containing_it_and_the_earliest_wins(self):
        day = {"date": "1946-06-14", "precision": "day"}
        year = {"date": "1946", "precision": "year"}
        self.assertEqual(wd.earliest_person_date([year, day]), day)
        self.assertEqual(wd.earliest_person_date([{"date": "1947", "precision": "year"}, day]), day)
        # Before the common era a larger number is earlier; string order gets this backwards.
        self.assertEqual(wd.earliest_person_date([{"date": "-0496", "precision": "year"},
                                                  {"date": "-0497", "precision": "year"}])["date"], "-0497")

    def test_one_query_asks_all_five_at_best_rank(self):
        query = wd.people_query(["Q2", "Q1", "Q2"])
        self.assertIn("VALUES ?item { wd:Q1 wd:Q2 }", query)
        for prop in ("P21", "P27", "P106"):
            self.assertIn(f"?item wdt:{prop} ?v", query)
        for prop in ("P569", "P570"):
            self.assertIn(f"?st a wikibase:BestRank ; psv:{prop} ?node", query)

    def test_an_answer_is_cached_per_batch(self):
        asked = []
        payload = body(self.row("Q1", "P21", ENTITY + "Q6581072"))
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(wd, "_sparql", lambda query: asked.append(query) or payload):
            cache = caching.ResponseCache("wiki", directory, 3600)
            first = wd.people(["Q1", "Q2"], cache)
            second = wd.people(["Q2", "Q1"], cache)
        self.assertEqual((first, second, len(asked)), ({"Q1": {"gender": ["Q6581072"]}},) * 2 + (1,))


class Birthplaces(unittest.TestCase):
    """Where a person was born and the country it is in, a country's code, a work's authors
    (oxyc/den-dataset#114)."""

    def test_a_place_and_its_countries_parse_and_unknown_is_no_place(self):
        got = wd.parse_birthplaces(body(
            {"item": ENTITY + "Q1", "place": ENTITY + "Q1757", "country": ENTITY + "Q33"},
            {"item": ENTITY + "Q1", "place": ENTITY + "Q1754", "country": ENTITY + "Q34"},
            # A place in no country, and a place Wikidata states as "unknown value".
            {"item": ENTITY + "Q2", "place": ENTITY + "Q1128337"},
            {"item": ENTITY + "Q3", "place": "http://www.wikidata.org/.well-known/genid/abc123"}))
        self.assertEqual(got, {"Q1": {"birthplace": ["Q1754", "Q1757"], "birthcountry": ["Q33", "Q34"]},
                               "Q2": {"birthplace": ["Q1128337"]}})

    def test_the_queries(self):
        self.assertEqual(wd.birthplace_query(["Q2", "Q1"]),
                         "SELECT ?item ?place ?country WHERE {\n  VALUES ?item { wd:Q1 wd:Q2 }\n"
                         "  ?item wdt:P19 ?place .\n  OPTIONAL { ?place wdt:P17 ?country . }\n}")
        self.assertIn("?item wdt:P297 ?iso .", wd.iso_query(["Q34"]))
        self.assertIn("?item wdt:P50 ?author .", wd.author_query(["Q30"]))

    def test_only_an_alpha_2_code_is_a_country_code(self):
        got = wd.parse_iso(body({"item": ENTITY + "Q34", "iso": "SE"}, {"item": ENTITY + "Q15180", "iso": "su"},
                                {"item": ENTITY + "Q1", "iso": "USA"}))
        self.assertEqual(got, {"Q34": "SE"})

    def test_a_works_authors_parse_sorted_by_number(self):
        got = wd.parse_authors(body({"item": ENTITY + "Q30", "author": ENTITY + "Q39829"},
                                    {"item": ENTITY + "Q30", "author": ENTITY + "Q10"},
                                    {"item": ENTITY + "Q31", "author": "http://www.wikidata.org/.well-known/genid/x"}))
        self.assertEqual(got, {"Q30": ["Q10", "Q39829"]})

    def test_an_answer_is_cached_per_batch_and_qlever_stands_behind_wdqs(self):
        asked = []
        payload = body({"item": ENTITY + "Q30", "author": ENTITY + "Q10"})

        def sparql(query, fallback=False):
            asked.append(fallback)
            return payload

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(wd, "_sparql", sparql):
            cache = caching.ResponseCache("wiki", directory, 3600)
            first = wd.authors(["Q30", "Q31"], cache)
            second = wd.authors(["Q31", "Q30"], cache)
        self.assertEqual((first, second, asked), ({"Q30": ["Q10"]}, {"Q30": ["Q10"]}, [True]))


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


class Fallback(unittest.TestCase):
    """The per-batch queries' second endpoint. Each host answers from a script: a status to raise, or a
    body; every request is recorded as (host, the text sent, the attempts it was allowed)."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = caching.ResponseCache("wiki", self.directory.name, 3600)
        self.sent, self.script = [], {}
        for patch in (mock.patch.object(wd.http, "request", self.answer),
                      mock.patch.dict(wd.os.environ, {}, clear=False),
                      mock.patch.object(wd, "ANSWERED", wd.collections.Counter())):
            patch.start()
            self.addCleanup(patch.stop)
        wd.os.environ.pop(wd.PREFER_VAR, None)

    def answer(self, host, path, params=None, body=None, attempts=wd.http.ATTEMPTS, **kwargs):
        self.sent.append((host, body.decode("utf-8"), attempts))
        reply = self.script[host]
        if isinstance(reply, int):
            raise wd.http.HTTPError(reply, f"https://{host}{path}")
        return reply

    def test_a_throttled_wdqs_hands_the_query_to_qlever_and_the_answer_is_kept_under_the_wdqs_text(self):
        self.script = {wd.HOST: 429, wd.QLEVER_HOST: body({"tmdb": "1", "v": "tt1"})}
        query = wd.facts_query([1], "movie", SPEC["imdbId"])
        self.assertEqual(wd.fetch_facts([1], "movie", SPEC["imdbId"], self.cache), ({1: "tt1"}, True))
        self.assertEqual(self.sent, [(wd.HOST, query, 1), (wd.QLEVER_HOST, wd.QLEVER_PREFIXES + query,
                                                          wd.http.ATTEMPTS)])
        self.assertIsNotNone(self.cache.read(self.cache.key(wd.CACHE_PATH, {"q": query})))
        self.assertEqual(dict(wd.ANSWERED), {"qlever": 1})

    def test_a_timeout_and_a_5xx_hand_over_too(self):
        for status in (0, 503):
            with self.subTest(status=status):
                self.script = {wd.HOST: status, wd.QLEVER_HOST: body()}
                self.sent = []
                wd._sparql("SELECT ?x WHERE {}", fallback=True)
                self.assertEqual([host for host, _, _ in self.sent], [wd.HOST, wd.QLEVER_HOST])

    def test_a_wdqs_refusal_of_the_query_itself_is_its_answer(self):
        self.script = {wd.HOST: 400, wd.QLEVER_HOST: body()}
        with self.assertRaises(wd.http.HTTPError):
            wd._sparql("SELECT ?x WHERE {}", fallback=True)
        self.assertEqual([host for host, _, _ in self.sent], [wd.HOST])

    def test_preferred_qlever_is_asked_first_and_wdqs_only_when_it_fails(self):
        env = {wd.PREFER_VAR: "qlever"}
        self.script = {wd.HOST: body({"x": "wdqs"}), wd.QLEVER_HOST: body({"x": "qlever"})}
        self.assertEqual(wd._sparql("SELECT ?x WHERE {}", fallback=True, env=env), body({"x": "qlever"}))
        self.script[wd.QLEVER_HOST] = 400
        self.assertEqual(wd._sparql("SELECT ?x WHERE {}", fallback=True, env=env), body({"x": "wdqs"}))
        self.assertEqual([(host, attempts) for host, _, attempts in self.sent],
                         [(wd.QLEVER_HOST, wd.http.ATTEMPTS), (wd.QLEVER_HOST, wd.http.ATTEMPTS),
                          (wd.HOST, wd.http.ATTEMPTS)])
        self.assertEqual(dict(wd.ANSWERED), {"qlever": 1, "wdqs": 1})

    def test_the_label_service_and_the_entity_steps_stay_on_wdqs(self):
        """QLever has no `SERVICE wikibase:label`; the entity and source steps never fall back."""
        env = {wd.PREFER_VAR: "qlever"}
        self.script = {wd.HOST: body(), wd.QLEVER_HOST: body()}
        wd._sparql('SELECT ?item ?itemLabel WHERE { SERVICE wikibase:label { bd:serviceParam '
                   'wikibase:language "en". } }', fallback=True, env=env)
        wd.os.environ[wd.PREFER_VAR] = "qlever"
        wd.series(["Q1"])
        self.assertEqual([(host, attempts) for host, _, attempts in self.sent],
                         [(wd.HOST, wd.http.ATTEMPTS)] * 2)

    def test_the_prefix_block_declares_every_prefix_a_per_batch_query_uses(self):
        """WDQS predeclares them and QLever does not, so one missing here fails every batch on QLever."""
        asked = []
        with mock.patch.object(wd, "_sparql", lambda query, **_: asked.append(query) or body()):
            wd.titles([1], "movie", excluded={1: ["Q9"]})
        asked += [wd.facts_query([1, 2], media, item, excluded={1: ["Q9"]})
                  for item in wd.SPECS for media in ("movie", "tv")]
        declared = set(re.findall(r"PREFIX (\w+):", wd.QLEVER_PREFIXES))
        for query in asked:
            used = set(re.findall(r"(?<![\w<?])([a-z]+):\w", query)) - {"http", "https"}
            self.assertLessEqual(used, declared, query)


ENTITY ="http://www.wikidata.org/entity/"


class Lookups(unittest.TestCase):
    """The three uncached hops, answered per query: the main request, then the aliases."""

    def setUp(self):
        self.answers = {}
        patch = mock.patch.object(wd, "_sparql",
                                  lambda query, **_: self.answers["alias" if "altLabel" in query else "main"])
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
        with mock.patch.object(wd, "_sparql", lambda query, **_: asked.append(query) or body()):
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

    def test_a_person_with_two_tmdb_ids_ships_the_lowest_whatever_the_row_order(self):
        """Measured: Claudine Dupuis (Q2978480) states 1118633 and 142921. The lowest by NUMBER, which the
        least string ("1118633") is not."""
        rows = [{"item": ENTITY + "Q2978480", "itemLabel": "Claudine Dupuis", "pid": "1118633"},
                {"item": ENTITY + "Q2978480", "itemLabel": "Claudine Dupuis", "pid": "142921"}]
        for order in (rows, rows[::-1]):
            with self.subTest(first=order[0]["pid"]):
                self.answers = {"main": body(*order), "alias": body()}
                self.assertEqual(wd.entity_details(["Q2978480"])["Q2978480"]["tmdbPersonId"], "142921")

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
