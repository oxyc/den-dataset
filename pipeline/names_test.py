#!/usr/bin/env python3
"""`pipeline/names.py` against a QLever stand-in: which name and year each item gets, one item per TMDB id,
the paging, and what the daily step does with a new generation, the same one, and a failed fetch."""
import datetime
import json
import os
import tempfile
import unittest

from . import names

ENTITY = "http://www.wikidata.org/entity/"
XSD = "http://www.w3.org/2001/XMLSchema#dateTime"


def uri(qid):
    return {"type": "uri", "value": ENTITY + qid}


def text(value, lang=None):
    cell = {"type": "literal", "value": value}
    if lang:
        cell["xml:lang"] = lang
    return cell


def date(value):
    return {"type": "literal", "datatype": XSD, "value": value}


class Wikidata:
    """Answers the queries `names.scan` and `names.evidence` send, from per-media tables of bindings."""

    def __init__(self, media):
        self.media = media
        self.asked = []

    def __call__(self, query):
        self.asked.append(query)
        if "schema:about" in query:
            return self.media.get("evidence", [])
        tv = "wdt:P4983" in query
        table = self.media["tv" if tv else "movie"]
        if "SELECT ?item ?id" in query:
            rows = table.get("claims", [])
        elif "wdt:P1476" in query:
            rows = table.get("titles", [])
        elif "FILTER(LANG(?label) = ?code)" in query:
            rows = table.get("native", [])
        elif "?item ?code" in query:
            rows = table.get("codes", [])
        elif "rdfs:label" in query:
            rows = table.get("labels", [])
        elif "wdt:P580 ?d" in query:
            rows = table.get("started", [])
        elif "wdt:P577 ?d" in query:
            rows = table.get("published", [])
        else:
            raise AssertionError(f"unexpected query: {query}")
        offset = int(query.rsplit("OFFSET ", 1)[1]) if "OFFSET " in query else 0
        limit = int(query.split("LIMIT ", 1)[1].split()[0]) if "LIMIT " in query else len(rows)
        return rows[offset:offset + limit]


def claims(*pairs):
    return [{"item": uri(qid), "id": text(str(tmdb_id))} for qid, tmdb_id in pairs]


WIKIDATA = {
    "movie": {
        # Q1 is uncontested. Q2 and Q3 both claim 20; Q3 also claims 21, so Q2, which states no other id,
        # answers for 20. Q4 and Q5 both claim 30 and nothing tells them apart, so 30 is left out. Q6 has
        # no name at all.
        "claims": claims(("Q1", 10), ("Q2", 20), ("Q3", 20), ("Q3", 21), ("Q4", 30), ("Q5", 30),
                         ("Q6", 40)),
        "labels": [
            {"item": uri("Q1"), "label": text("Alpha (1999 film)", "en")},
            {"item": uri("Q2"), "label": text("Beta", "mul")},
            {"item": uri("Q3"), "label": text("Gamma", "en")},
            {"item": uri("Q4"), "label": text("Delta", "en")},
            {"item": uri("Q5"), "label": text("Delta", "en")},
        ],
        "titles": [
            {"item": uri("Q1"), "title": text("Alfa", "sv")},
            {"item": uri("Q1"), "title": text("Alpha", "en")},
            {"item": uri("Q2"), "title": text("Beta", "en")},
        ],
        "codes": [{"item": uri("Q1"), "code": text("sv")}],
        "published": [
            {"item": uri("Q1"), "d": date("2001-05-01T00:00:00Z")},
            {"item": uri("Q1"), "d": date("1999-12-31T00:00:00Z")},
            {"item": uri("Q2"), "d": text("unknown")},
        ],
    },
    "tv": {
        # Q7: no English or multilingual label, so its own-language label names it. Its start time is its
        # year, not its later publication date.
        "claims": claims(("Q7", 10)),
        "native": [{"item": uri("Q7"), "label": text("Sju", "sv")}],
        "started": [{"item": uri("Q7"), "d": date("2010-01-01T00:00:00Z")}],
        "published": [{"item": uri("Q7"), "d": date("2008-01-01T00:00:00Z")}],
    },
    "evidence": [
        {"film": uri("Q2"), "claim": text("20")},
        {"film": uri("Q3"), "claim": text("20")},
        {"film": uri("Q3"), "claim": text("21")},
        {"film": uri("Q4"), "claim": text("30")},
        {"film": uri("Q5"), "claim": text("30")},
    ],
}


def table(wikidata=None, decisions=None):
    return names.build(wikidata or Wikidata(WIKIDATA), decisions={} if decisions is None else decisions,
                       describe=lambda: "a dump", today=datetime.date(2026, 10, 11))


class Build(unittest.TestCase):
    def test_each_title_gets_one_items_name_and_year(self):
        built = table()
        self.assertEqual(built["rows"], [
            # The label, unstripped (the store strips it); the Swedish title, in the original language.
            ["movie", 10, "Alpha (1999 film)", 1999, "Alfa"],
            # The item stating no other id answers for 20; its multilingual label names it.
            ["movie", 20, "Beta", None, None],
            ["movie", 21, "Gamma", None, None],
            ["tv", 10, "Sju", 2010, None],
        ])
        self.assertEqual(built["counts"]["movie"]["ambiguous"], 1, "30 is claimed twice, undecided")
        self.assertEqual(built["counts"]["movie"]["named"], 3, "40's item has no name")
        self.assertEqual(built["source"], {"name": "wikidata", "license": "CC0-1.0",
                                           "endpoint": "https://qlever.dev/api/wikidata",
                                           "index": "a dump", "fetched": "2026-10-11"})

    def test_a_committed_decision_chooses_a_contested_item(self):
        built = table(decisions={("movie", 30): "Q5"})
        self.assertIn(["movie", 30, "Delta", None, None], built["rows"])

    def test_scans_are_paged_until_a_short_page(self):
        many = {"movie": {"claims": claims(*((f"Q{i}", i) for i in range(1, 6)))}, "tv": {}}
        asker = Wikidata(many)
        original = names.PAGE
        names.PAGE = 2
        try:
            names.build(asker, decisions={}, describe=lambda: "a dump")
        finally:
            names.PAGE = original
        offsets = [q.rsplit("OFFSET ", 1)[1] for q in asker.asked if "SELECT ?item ?id" in q
                   and "wdt:P4947" in q]
        self.assertEqual(offsets, ["0", "2", "4"])

    def test_an_answer_that_is_not_a_result_is_refused(self):
        class Broken(Wikidata):
            def __call__(self, query):
                raise names.NamesError("names: QLever answered something that is not a SPARQL result")
        with self.assertRaises(names.NamesError):
            table(Broken(WIKIDATA))


class Refresh(unittest.TestCase):
    def test_a_new_generation_gets_a_new_table(self):
        with tempfile.TemporaryDirectory() as out:
            path = os.path.join(out, "names.json")
            what, _ = names.refresh(path, True, build_table=table)
            self.assertEqual(what, "refreshed")
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["rows"][0], ["movie", 10, "Alpha (1999 film)", 1999, "Alfa"])

    def test_the_live_generation_keeps_its_copy(self):
        with tempfile.TemporaryDirectory() as out:
            path = os.path.join(out, "names.json")
            with open(path, "w") as fh:
                fh.write("carried")

            def refuse():
                raise AssertionError("the live generation must not be asked again")
            self.assertEqual(names.refresh(path, False, build_table=refuse)[0], "kept")
            with open(path) as fh:
                self.assertEqual(fh.read(), "carried")

    def test_a_new_generation_keeps_a_copy_younger_than_a_week(self):
        """The scan is the stage's whole cost, and a week-old names table is as good as today's."""
        today = datetime.date(2026, 10, 13)

        def carried(fetched):
            with open(path, "w") as fh:
                json.dump({"source": {"fetched": fetched}, "rows": []}, fh)

        def refuse():
            raise AssertionError("a young carried table was rebuilt")
        with tempfile.TemporaryDirectory() as out:
            path = os.path.join(out, "names.json")
            carried("2026-10-07")
            what, why = names.refresh(path, True, build_table=refuse, today=today)
            self.assertEqual(what, "kept")
            self.assertIn("6 day(s) ago", why)
            carried("2026-10-06")
            self.assertEqual(names.refresh(path, True, build_table=table, today=today)[0], "refreshed")

    def test_a_failed_fetch_keeps_the_carried_copy_and_says_so(self):
        def fail():
            raise names.NamesError("names: QLever failed: HTTP 503")
        with tempfile.TemporaryDirectory() as out:
            path = os.path.join(out, "names.json")
            with open(path, "w") as fh:
                fh.write("carried")
            what, why = names.refresh(path, True, build_table=fail)
            self.assertEqual(what, "failed")
            self.assertIn("HTTP 503", why)
            self.assertIn("copy is kept", why)
            with open(path) as fh:
                self.assertEqual(fh.read(), "carried")


if __name__ == "__main__":
    unittest.main()
