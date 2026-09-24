#!/usr/bin/env python3
"""The franchises stage against a fixture out-dir, with Wikidata and Jev stubbed.

No network. What is tested is what the stage decides from Wikidata alone, what a title is sent, that a
second run buys nothing, what the answers derive, and that the golden set decides whether anything is
written. The facts are shaped like the cases oxyc/den-atlas#92 measured: Spider-Man's films nest in one
series (automatic, with eras), Beck's 1997– films and its 1993 films are two groups nothing on Wikidata
links (asked), and Studio Ghibli is a studio's films typed a film series (asked).
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from . import franchises
from .contract import Context, StageError

VERSION = "testver"
RECORDS = [
    (1, "Beck – Mannen med ikonerna", 1997, {"franchise": ["Qbeck"]}),
    (2, "Beck – Vita nätter", 1998, {"franchise": ["Qbeck"]}),
    (3, "Roseanna", 1993, {"basedOn": ["Qn1"]}),
    (4, "The Man on the Balcony", 1993, {"basedOn": ["Qn2"]}),
    (5, "Spider-Man", 2002, {"franchise": ["Qraimi"]}),
    (6, "Spider-Man 2", 2004, {"franchise": ["Qraimi"]}),
    (7, "The Amazing Spider-Man", 2012, {"franchise": ["Qwebb"]}),
    (12, "The Amazing Spider-Man 2", 2014, {"franchise": ["Qwebb"]}),
    (8, "Spirited Away", 2001, {"franchise": ["Qghibli"]}),
    (9, "Princess Mononoke", 1997, {"franchise": ["Qghibli"]}),
    (10, "My Neighbor Totoro", 1988, {"franchise": ["Qghibli"]}),
]
ENTITIES = {"Qbeck": {"en": "Beck"}, "Qraimi": {"en": "Spider-Man trilogy"},
            "Qwebb": {"en": "The Amazing Spider-Man series"}, "Qghibli": {"en": "Studio Ghibli Feature Films"}}
GOLDEN = {
    "cases": [{"name": "Beck", "together": ["movie:1", "movie:2", "movie:3", "movie:4"],
               "eras": [["movie:3", "movie:4"], ["movie:1", "movie:2"]]},
              {"name": "Spider-Man", "together": ["movie:5", "movie:6", "movie:7", "movie:12"],
               "eras": [["movie:5", "movie:6"], ["movie:7", "movie:12"]]}],
    "apart": [["movie:8", "movie:10"]],
    "none": ["movie:8", "movie:9"],
    "floors": {"togetherRecall": 1.0, "eraAgreement": 1.0, "apartViolations": 0, "noneViolations": 0},
}


class Jev:
    """The provider: a title whose candidates name a studio answers `none`; every other title takes group A
    and says the listed groups are one franchise."""

    sent = []
    RATE_PER_INPUT_TOKEN = 0.042 / 1_000_000

    def __init__(self, model=None):
        self.model = model
        self.calls = self.input_tokens = self.output_tokens = 0
        self.spend = 0.0

    def ask_with_metadata(self, state, questions):
        type(self).sent.append(state)
        self.calls += 1
        listed = next(s["text"] for s in state["article"]["sections"].values() if s["heading"] == franchises.HEADING)
        choice = "none" if "Studio Ghibli" in listed else "A"
        criteria = list(questions["fr__group"]["criteria"])
        rest = 0.1 / (len(criteria) - 1)
        return ({"fr__group": {"type": "choice", "choice": choice, "confidence": 0.9,
                               "probabilities": {c: 0.9 if c == choice else rest for c in criteria}},
                 "fr__one_franchise": {"type": "noul", "noul": 0.9},
                 "fr__separate_adaptation": {"type": "noul", "noul": 0.1}},
                {"model": self.model, "usage": {"input_tokens": 900, "output_tokens": 50}})

    def summary(self):
        return "stub"


class Stage(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.out = os.path.join(self.dir, "out")
        os.makedirs(self.out)
        with open(os.path.join(self.out, "dataset.meta.json"), "w", encoding="utf-8") as fh:
            json.dump({"datasetVersion": VERSION}, fh)
        records = [{"mediaType": "movie", "tmdbId": i, "titles": {"en": name},
                    "released": {"date": str(year), "precision": "year"}, **extra}
                   for i, name, year, extra in RECORDS]
        with open(os.path.join(self.out, f"facts-{VERSION}.json"), "w", encoding="utf-8") as fh:
            json.dump({"schema": 1, "datasetVersion": VERSION, "records": records, "entities": ENTITIES}, fh)
        with open(os.path.join(self.out, "articles.jsonl"), "w", encoding="utf-8") as fh:
            for i, name, year, _ in RECORDS:
                fh.write(json.dumps({"mediaType": "movie", "tmdbId": i, "title": name, "year": year,
                                     "article": name, "language": "en", "revId": 1,
                                     "text": f"{name} is a film.\n\n== Plot ==\nSomething happens.\n"}) + "\n")
        self.golden(GOLDEN)
        stubs = {"source_series": lambda qids, cache=None: {q: ["Qnovels"] for q in qids if q in ("Qn1", "Qn2")},
                 "tmdb_keys": lambda qids, cache=None: {},
                 "parents": lambda qids, cache=None: {q: ["Qsm"] for q in qids if q in ("Qraimi", "Qwebb")},
                 "entity_details": lambda qids: {q: {"name": {"Qnovels": "Martin Beck",
                                                              "Qsm": "Spider-Man in film"}[q]}
                                                 for q in qids if q in ("Qnovels", "Qsm")}}
        for name, stub in stubs.items():
            patched = mock.patch.object(franchises.wd, name, stub)
            patched.start()
            self.addCleanup(patched.stop)
        Jev.sent = []

    def golden(self, doc):
        path = os.path.join(self.dir, "golden.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        patched = mock.patch.object(franchises, "GOLDEN", path)
        patched.start()
        self.addCleanup(patched.stop)

    def run_stage(self, **kw):
        with mock.patch.object(franchises.rc, "TypeSafe", Jev):
            return franchises.run(Context(out_dir=self.out, **kw), cache=object())

    def derived(self):
        with open(os.path.join(self.out, "franchises.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_wikidata_alone_decides_the_nested_series_and_leaves_the_rest_unasked_without_spend(self):
        self.golden({**GOLDEN, "floors": {**GOLDEN["floors"], "togetherRecall": 0.0}})
        self.run_stage()
        doc = self.derived()
        self.assertEqual(Jev.sent, [], "nothing is bought without --spend")
        spider = doc["franchises"]["Qsm"]
        self.assertEqual((spider["name"], spider["source"]), ("Spider-Man in film", "wikidata"))
        self.assertEqual([(m["key"], m["era"]) for m in spider["members"]],
                         [("movie:5", "Spider-Man trilogy"), ("movie:6", "Spider-Man trilogy"),
                          ("movie:7", "The Amazing Spider-Man series"),
                          ("movie:12", "The Amazing Spider-Man series")])
        for key in ("movie:1", "movie:3", "movie:8"):
            self.assertNotIn(key, doc["titles"], "asked, and not answered yet")

    def test_the_ask_sends_the_candidates_and_the_answers_join_beck_and_leave_ghibli_out(self):
        self.run_stage(spend=True)
        self.assertEqual(len(Jev.sent), 7)
        roseanna = next(s for s in Jev.sent if s["requestedTarget"]["title"] == "Roseanna")
        headings = [s["heading"] for s in roseanna["article"]["sections"].values()]
        self.assertEqual(headings, ["Lead", franchises.HEADING], "the lead and the candidates, nothing else")
        listed = roseanna["article"]["sections"][max(roseanna["article"]["sections"])]["text"]
        self.assertIn('A: adaptations of the book series "Martin Beck", 2 titles', listed)
        self.assertIn('B: the Wikidata series "Beck", 2 titles', listed)
        doc = self.derived()
        beck = doc["franchises"]["Qbeck"]
        self.assertEqual((beck["name"], beck["source"]), ("Beck", "jev-franchise-v1"))
        self.assertEqual([(m["key"], m["era"]) for m in beck["members"]],
                         [("movie:3", "Martin Beck"), ("movie:4", "Martin Beck"), ("movie:1", None),
                          ("movie:2", None)])
        for key in ("movie:8", "movie:9", "movie:10"):
            self.assertNotIn(key, doc["titles"])
        self.assertEqual(doc["derivation"]["eval"]["togetherRecall"], 1.0)

        self.run_stage(spend=True)
        self.assertEqual(len(Jev.sent), 7, "a title answered in any shard is never asked again")

    def test_below_a_floor_nothing_is_written(self):
        self.golden({**GOLDEN, "apart": [["movie:5", "movie:7"]]})
        with self.assertRaises(StageError) as refused:
            self.run_stage(spend=True)
        self.assertIn("apartViolations 1 > floor 0", str(refused.exception))
        self.assertFalse(os.path.exists(os.path.join(self.out, "franchises.json")))

    def rewrite_facts(self, change):
        facts = os.path.join(self.out, f"facts-{VERSION}.json")
        with open(facts, encoding="utf-8") as fh:
            blob = json.load(fh)
        change(blob["records"])
        with open(facts, "w", encoding="utf-8") as fh:
            json.dump(blob, fh)

    def test_an_answer_is_used_while_its_candidates_stand_and_set_aside_once_they_change(self):
        self.run_stage(spend=True)
        self.golden({**GOLDEN, "floors": {**GOLDEN["floors"], "togetherRecall": 0.0, "eraAgreement": 0.0}})
        # A new member changes the titles a group lists, not which groups a title is shown.
        self.rewrite_facts(lambda records: records.append(
            {"mediaType": "movie", "tmdbId": 11, "titles": {"en": "Beck – Öga för öga"},
             "released": {"date": "1998", "precision": "year"}, "franchise": ["Qbeck"]}))
        self.run_stage()
        doc = self.derived()
        self.assertNotIn("answered under other candidates", doc["derivation"]["counts"])
        self.assertIn("movie:1", doc["titles"])
        # A new group for movie:1 and movie:2 is a new list of candidates: their letters meant another one.
        self.rewrite_facts(lambda records: [r.update(mediaFranchise=["Qother"]) for r in records
                                            if r["tmdbId"] in (1, 2)])
        self.run_stage()
        doc = self.derived()
        self.assertEqual(doc["derivation"]["counts"]["answered under other candidates"], 2)
        self.assertNotIn("movie:1", doc["titles"])

    def test_plan_writes_nothing(self):
        report = self.run_stage(plan=True)
        self.assertIn("7 titles to ask", report)
        self.assertEqual(os.listdir(self.out).count("franchises.json"), 0)
        self.assertEqual([p for p in os.listdir(self.out) if p.startswith("franchise-")], [])


if __name__ == "__main__":
    unittest.main()
