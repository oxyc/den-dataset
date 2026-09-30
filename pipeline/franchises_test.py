#!/usr/bin/env python3
"""The franchises stage against a fixture out-dir, with Wikidata and Jev stubbed.

No network. What is tested is what the stage decides from Wikidata alone, what a title is sent, that a
second run buys nothing, what the answers derive, and that the golden set decides whether anything is
written. The facts are shaped like the cases oxyc/den-atlas#92 measured: Spider-Man's films nest in one
series (automatic, with eras), Beck's 1997– films and its 1993 films are two groups no Wikidata series
links (asked; a continuing cast lets them merge), and Studio Ghibli is a studio's films typed a film series (asked).
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from . import franchise_groups as fg
from . import franchises
from . import run_combined as rc
from .contract import Context, StageError

VERSION = "testver"
#: Actors of both Beck groups: the continuing cast that lets the two merge (oxyc/den-dataset#158).
BECK_CAST = ["Qpersbrandt", "Qhirdwall", "Qcarlsson"]
RECORDS = [
    (1, "Beck – Mannen med ikonerna", 1997, {"franchise": ["Qbeck"], "cast": BECK_CAST}),
    (2, "Beck – Vita nätter", 1998, {"franchise": ["Qbeck"]}),
    (3, "Roseanna", 1993, {"basedOn": ["Qn1"], "cast": BECK_CAST}),
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


def decision(choice="A", confidence=0.9, probability=0.9, one=0.1, separate=0.1):
    return {"fr__group": {"type": "choice", "choice": choice, "confidence": confidence,
                           "probabilities": {choice: probability}},
            "fr__one_franchise": {"type": "noul", "noul": one},
            "fr__separate_adaptation": {"type": "noul", "noul": separate}}


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

    def paid_files(self):
        [answers] = [os.path.join(self.out, name) for name in os.listdir(self.out)
                     if name.startswith("franchise-answers-v1-") and name.endswith(".jsonl")]
        return answers, answers + ".manifest.json"

    def keys_file(self, *keys):
        path = os.path.join(self.dir, "pilot-keys.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("".join(key + "\n" for key in keys))
        return path

    def test_wikidata_alone_decides_the_nested_series_and_leaves_the_rest_unasked_without_spend(self):
        self.golden({**GOLDEN, "floors": {**GOLDEN["floors"], "togetherRecall": 0.0}})
        self.run_stage()
        doc = self.derived()
        self.assertEqual(Jev.sent, [], "nothing is bought without --spend")
        spider = doc["franchises"]["Qsm"]
        self.assertEqual((spider["name"], spider["source"]), ("Spider-Man in film", "wikidata"))
        self.assertEqual([(m["key"], m["eraId"], m["order"]) for m in spider["members"]],
                         [("movie:5", "Qsm:era:Qraimi", 0), ("movie:6", "Qsm:era:Qraimi", 1),
                          ("movie:7", "Qsm:era:Qwebb", 2), ("movie:12", "Qsm:era:Qwebb", 3)])
        self.assertEqual(doc["titles"]["movie:5"], {"primary": "Qsm"})
        for key in ("movie:1", "movie:3", "movie:8"):
            self.assertNotIn(key, doc["titles"], "asked, and not answered yet")

    def test_own_wikidata_name_distinguishes_the_norwegian_olsen_line(self):
        titles = {
            "movie:1": fg.Title("movie:1", "The Olsen Gang", 1968),
            "movie:2": fg.Title("movie:2", "The Olsen Gang in a Fix", 1969),
            "movie:24667": fg.Title("movie:24667", "Olsenbanden", 1969),
            "movie:3": fg.Title("movie:3", "Olsenbanden og Dynamitt-Harry", 1970),
        }
        groups = {
            "Q1047863": fg.Group("Q1047863", "series", "Olsen Gang", {"movie:1", "movie:2"}),
            "Q3905033": fg.Group("Q3905033", "series", "Olsen Gang", {"movie:24667", "movie:3"}),
        }
        names = {qid: group.name for qid, group in groups.items()}
        _, _, asked = franchises.plan_before_display_names(titles, groups, names, {
            "_": "fixture", "Q3905033": "Olsenbanden",
            "Q1047863": "Olsen Gang (Danish films)",
        })
        self.assertEqual(asked["movie:1"], ["Q1047863", "Q3905033"],
                         "the viewer-facing rename must not invalidate the paid candidate list")
        self.assertEqual(groups["Q1047863"].name, "Olsen Gang (Danish films)")
        self.assertEqual(groups["Q3905033"].name, "Olsenbanden")

    def test_duplicate_display_names_are_a_build_failure_and_list_every_group(self):
        duplicate = {"Q1047863": {"name": "Olsen Gang"}, "Q3905033": {"name": "Olsen Gang"}}
        with self.assertRaisesRegex(StageError, "'Olsen Gang': Q1047863, Q3905033"):
            franchises.check_unique_names(duplicate)

    def test_other_duplicate_names_are_qualified_by_first_release_and_medium(self):
        duplicate = {
            "film": {"name": "Assassination Classroom", "members": [{"key": "movie:1", "year": 2015}]},
            "tv": {"name": "Assassination Classroom", "members": [{"key": "tv:2", "year": 2015}]},
        }
        changed = franchises.disambiguate_duplicate_names(duplicate)
        self.assertEqual(changed, {"film": "Assassination Classroom (2015 films)",
                                   "tv": "Assassination Classroom (2015 series)"})
        franchises.check_unique_names(duplicate)

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
        self.assertEqual([(m["key"], m["eraId"], m["order"]) for m in beck["members"]],
                         [("movie:3", "Qbeck:era:Qnovels", 0), ("movie:4", "Qbeck:era:Qnovels", 1),
                          ("movie:1", "Qbeck:era:main", 2), ("movie:2", "Qbeck:era:main", 3)])
        self.assertEqual((beck["id"], beck["confidence"], beck["source"]),
                         ("Qbeck", 0.9, "jev-franchise-v1"))
        for key in ("movie:8", "movie:9", "movie:10"):
            self.assertNotIn(key, doc["titles"])
        self.assertEqual(doc["derivation"]["eval"]["togetherRecall"], 1.0)

        self.run_stage(spend=True)
        self.assertEqual(len(Jev.sent), 7, "a title answered in any shard is never asked again")

    def test_a_separate_adaptation_is_asked_the_line_question_in_the_same_run_and_stays(self):
        class RoseannaApart(Jev):
            """Calls Roseanna a separate adaptation, then says it continues the line."""

            def ask_with_metadata(self, state, questions):
                if "fr__continues_line" in questions:
                    type(self).sent.append(state)
                    return ({"fr__continues_line": {"type": "noul", "noul": 0.9}},
                            {"model": self.model, "usage": {"input_tokens": 900, "output_tokens": 10}})
                answers, meta = super().ask_with_metadata(state, questions)
                if state["requestedTarget"]["title"] == "Roseanna":
                    answers["fr__separate_adaptation"]["noul"] = 0.9
                return answers, meta

        RoseannaApart.sent = []
        with mock.patch.object(franchises.rc, "TypeSafe", RoseannaApart):
            franchises.run(Context(out_dir=self.out, spend=True), cache=object())
        self.assertEqual(len(RoseannaApart.sent), 8, "seven titles, then the one separate adaptation")
        self.assertEqual(RoseannaApart.sent[-1]["requestedTarget"]["title"], "Roseanna")
        self.assertIn("movie:3", self.derived()["titles"], "the line pass kept Roseanna in Beck")
        with open(os.path.join(self.out, "franchise-line-decisions-v1.json"), encoding="utf-8") as fh:
            self.assertEqual(list(json.load(fh)["decisions"]), ["movie:3"])
        with mock.patch.object(franchises.rc, "TypeSafe", RoseannaApart):
            franchises.run(Context(out_dir=self.out, spend=True), cache=object())
        self.assertEqual(len(RoseannaApart.sent), 8, "neither pass asks a title twice")

    def test_compact_decisions_rebuild_without_private_states_or_raw_answers(self):
        self.run_stage(spend=True)
        expected = self.derived()
        compact = os.path.join(self.out, "franchise-decisions-v1.json")
        with open(compact, encoding="utf-8") as fh:
            durable = json.load(fh)
        self.assertEqual(durable["usage"], {"calls": 7, "inputTokens": 6300, "outputTokens": 350,
                                             "costUSD": 0.0002646})
        prose = json.dumps(durable)
        for _number, title, _year, _extra in RECORDS:
            self.assertNotIn(title, prose)
        for name in list(os.listdir(self.out)):
            if name.startswith("franchise-states-") or name.startswith("franchise-answers-v1-"):
                os.unlink(os.path.join(self.out, name))
        self.run_stage()
        self.assertEqual(self.derived(), expected)

    def test_character_candidates_have_one_canonical_durable_shape(self):
        row = {"answers": {
                   "fr__group": {"type": "choice", "choice": "none", "confidence": 0.9,
                                  "probabilities": {"A": 0.01, "B": 0.01, "C": 0.01, "D": 0.01,
                                                    "none": 0.94, "other": 0.01,
                                                    "not-stated": 0.01}},
                   "fr__one_franchise": {"type": "noul", "noul": 0.1},
                   "fr__separate_adaptation": {"type": "noul", "noul": 0.1}},
               "calls": [{"responseModel": franchises.PINNED_MODEL,
                           "inputTokens": 123, "outputTokens": 4}]}
        made = franchises.decisions.from_paid_row(
            "movie:1", row, [("characters", ("movie:1", "tv:2"))])
        self.assertEqual(made["candidates"], [["characters", ["movie:1", "tv:2"]]])
        self.assertEqual(json.loads(json.dumps(made)), made)

    def test_keys_select_an_exact_pilot_and_plan_reports_its_measured_cost(self):
        keys = self.keys_file("movie:1", "movie:3")
        self.run_stage(spend=True, keys=keys)
        self.assertEqual(len(Jev.sent), 2)
        with open(os.path.join(self.out, "franchise-decisions-v1.json"), encoding="utf-8") as fh:
            durable = json.load(fh)
        self.assertEqual(set(durable["decisions"]), {"movie:1", "movie:3"})
        self.assertEqual(durable["usage"]["costUSD"], 0.0000756)
        said = self.run_stage(plan=True, keys=keys)
        self.assertIn("0 titles to ask", said)

    def test_keys_refuse_a_title_that_does_not_need_judgment(self):
        with self.assertRaisesRegex(StageError, "need no franchise judgment"):
            self.run_stage(spend=True, keys=self.keys_file("movie:5"))
        self.assertEqual(Jev.sent, [])

    def test_an_umbrella_is_a_title_label_not_an_exclusion_for_every_franchise_member(self):
        titles = {"movie:1": franchises.fg.Title("movie:1", "One", 2001),
                  "movie:2": franchises.fg.Title("movie:2", "Two", 2002)}
        groups = {"Qprimary": franchises.fg.Group("Qprimary", "series", "Primary", titles),
                  "Quniverse": franchises.fg.Group("Quniverse", "franchise", "Universe", ["movie:1"])}
        grouped = {"Qprimary": {"name": "Primary", "source": franchises.JEV, "confidence": 0.899,
                                "umbrellaOf": {"movie:1": "Quniverse"},
                                "members": {"movie:1": None, "movie:2": None}}}
        doc, title_rows = franchises.document(grouped, groups, titles, {})
        self.assertEqual(title_rows["movie:1"],
                         {"primary": "Qprimary", "umbrella": {"id": "Quniverse", "name": "Universe"}})
        self.assertEqual(title_rows["movie:2"], {"primary": "Qprimary"})
        self.assertNotIn("umbrella", doc["Qprimary"])
        self.assertEqual(doc["Qprimary"]["confidence"], 0.89)

    def test_a_flagged_catalogue_needs_clear_support_across_its_members(self):
        titles = {f"movie:{i}": franchises.fg.Title(f"movie:{i}", f"Part {i}", 2000 + i)
                  for i in range(1, 4)}
        groups = {"Qtheme": franchises.fg.Group("Qtheme", "series", "Theme trilogy", titles)}
        asked = {key: ["Qtheme"] for key in titles}
        answers = {key: (decision(confidence=0.7, probability=0.7), ["Qtheme"]) for key in titles}
        got, counts = franchises.resolve(titles, groups, {"Qtheme": "catalogue"}, {}, asked, answers, {})
        self.assertEqual(got, {})
        self.assertEqual(counts["no franchise"], 3)

    def test_a_shared_universe_is_never_merged_into_a_primary_franchise(self):
        titles = {"movie:1": franchises.fg.Title("movie:1", "Iron", 2008),
                  "movie:2": franchises.fg.Title("movie:2", "Thunder", 2011)}
        groups = {"Quniverse": franchises.fg.Group("Quniverse", "franchise", "Universe", titles),
                  "Qstory": franchises.fg.Group("Qstory", "series", "Thunder", ["movie:2"])}
        asked = {"movie:1": ["Quniverse"], "movie:2": ["Quniverse", "Qstory"]}
        answers = {"movie:1": (decision(), ["Quniverse"]),
                   "movie:2": (decision(choice="B", one=0.99), ["Quniverse", "Qstory"])}
        got, _ = franchises.resolve(titles, groups, {"Quniverse": "universe"}, {}, asked, answers, {})
        self.assertEqual(got["Quniverse"]["members"], {"movie:1": None})
        self.assertEqual(got["Qstory"]["members"], {"movie:2": None})
        self.assertEqual(got["Qstory"]["umbrellaOf"], {"movie:2": "Quniverse"})

    def test_a_separate_adaptation_of_a_book_series_joins_no_franchise(self):
        titles = {"movie:1": franchises.fg.Title("movie:1", "Loose relocation", 1973),
                  "movie:2": franchises.fg.Title("movie:2", "Another production", 2011)}
        groups = {"Qbooks": franchises.fg.Group("Qbooks", "book-series", "The books", titles)}
        asked = {key: ["Qbooks"] for key in titles}
        answers = {"movie:1": (decision(confidence=0.8, separate=0.9), ["Qbooks"]),
                   "movie:2": (decision(confidence=0.95, separate=0.9), ["Qbooks"])}
        got, counts = franchises.resolve(titles, groups, {}, {}, asked, answers, {})
        self.assertEqual(got, {})
        self.assertEqual(counts["separate production"], 2)

    def bond(self):
        """Eon's films nest in the James Bond series, which also names the 1967 Casino Royale."""
        titles = {"movie:1": franchises.fg.Title("movie:1", "Dr. No", 1962, series=["Qbond", "Qeon"]),
                  "movie:2": franchises.fg.Title("movie:2", "GoldenEye", 1995, series=["Qbond", "Qeon"]),
                  "movie:3": franchises.fg.Title("movie:3", "Casino Royale", 1967, series=["Qbond"]),
                  "movie:4": franchises.fg.Title("movie:4", "Casino Royale", 2006, series=["Qbond", "Qeon"]),
                  "movie:5": franchises.fg.Title("movie:5", "Skyfall", 2012, series=["Qbond", "Qeon"])}
        groups = {"Qbond": franchises.fg.Group("Qbond", "series", "James Bond", titles),
                  "Qeon": franchises.fg.Group("Qeon", "series", "Eon", ["movie:1", "movie:2", "movie:4", "movie:5"])}
        asked = {key: ["Qbond", "Qeon"] for key in titles}
        return titles, groups, asked

    def test_an_answer_split_between_a_franchise_and_its_era_joins_the_franchise_in_that_era(self):
        titles, groups, asked = self.bond()
        split = decision(choice="B", confidence=0.48, probability=0.56)
        split["fr__group"]["probabilities"]["A"] = 0.43
        answers = {"movie:1": (decision(), asked["movie:1"]), "movie:2": (split, asked["movie:2"])}
        got, _ = franchises.resolve(titles, groups, {}, {}, asked, answers, {})
        self.assertEqual(got["Qbond"]["members"], {"movie:1": "Qeon", "movie:2": "Qeon"})
        self.assertEqual(got["Qbond"]["confidence"], 0.9)

    def test_another_production_leaves_and_a_reboot_in_the_line_starts_an_era(self):
        titles, groups, asked = self.bond()
        answers = {key: (decision(separate=0.9 if key in ("movie:3", "movie:4") else 0.1), asked[key])
                   for key in titles}
        got, counts = franchises.resolve(titles, groups, {}, {}, asked, answers, {})
        self.assertEqual(got["Qbond"]["members"], {"movie:1": "Qeon", "movie:2": "Qeon",
                                                   "movie:4": "adaptation:movie:4", "movie:5": "adaptation:movie:4"})
        self.assertEqual(counts["separate production"], 1, "the 1967 Casino Royale is outside the Eon series")

    def hannibal(self):
        """A book series and a Wikidata series holding the same films, as Hannibal Lecter's do; Red Dragon is a
        second adaptation of Manhunter's novel, and the Hopkins films' prequel."""
        titles = {"movie:1": franchises.fg.Title("movie:1", "Manhunter", 1986),
                  "movie:2": franchises.fg.Title("movie:2", "The Silence of the Lambs", 1991),
                  "movie:3": franchises.fg.Title("movie:3", "Hannibal", 2001),
                  "movie:4": franchises.fg.Title("movie:4", "Red Dragon", 2002)}
        groups = {"Qbooks": franchises.fg.Group("Qbooks", "book-series", "Hannibal Lecter", titles),
                  "Qseries": franchises.fg.Group("Qseries", "series", "Hannibal Lecter", titles)}
        asked = {key: ["Qbooks", "Qseries"] for key in titles}
        answers = {key: (decision(choice="B", separate=0.76 if key == "movie:4" else 0.1), asked[key])
                   for key in titles}
        return titles, groups, asked, answers

    def test_a_separate_adaptation_the_line_pass_says_continues_the_line_stays_in_it(self):
        titles, groups, asked, answers = self.hannibal()
        line = {"movie:4": ({"fr__continues_line": {"type": "noul", "noul": 0.9}}, asked["movie:4"])}
        got, counts = franchises.resolve(titles, groups, {}, {}, asked, answers, {}, line)
        self.assertIn("movie:4", got["Qseries"]["members"])
        self.assertFalse(str(got["Qseries"]["members"]["movie:4"]).startswith("adaptation:"),
                         "a continuation is one more title of the line, not a reboot starting an era")
        self.assertEqual(counts["separate production"], 0)

    def test_without_a_clear_yes_from_the_line_pass_a_separate_adaptation_leaves(self):
        titles, groups, asked, answers = self.hannibal()
        for line in ({}, {"movie:4": ({"fr__continues_line": {"type": "noul", "noul": 0.6}}, asked["movie:4"])},
                     {"movie:4": ({"fr__continues_line": {"type": "noul", "noul": 0.9}}, ["Qseries"])}):
            got, counts = franchises.resolve(titles, groups, {}, {}, asked, answers, {}, line)
            self.assertNotIn("movie:4", got["Qseries"]["members"], line)
            self.assertEqual(counts["separate production"], 1)

    def test_the_line_pass_asks_only_separate_adaptations_answered_under_their_candidates(self):
        titles, groups, asked, answers = self.hannibal()
        self.assertEqual(franchises.line_targets(asked, answers), {"movie:4": ["Qbooks", "Qseries"]})
        stale = {**asked, "movie:4": ["Qseries"]}
        self.assertEqual(franchises.line_targets(stale, answers), {})

    def test_a_group_listed_by_a_shared_name_word_is_not_merged_on_one_sides_word(self):
        titles = {"movie:1": franchises.fg.Title("movie:1", "Into the Spider-Verse", 2018),
                  "movie:2": franchises.fg.Title("movie:2", "Across the Spider-Verse", 2023),
                  "tv:3": franchises.fg.Title("tv:3", "Hawaii Five-O", 1968),
                  "tv:4": franchises.fg.Title("tv:4", "Hawaii Five-0", 2010)}
        groups = {"Qverse": franchises.fg.Group("Qverse", "series", "Spider-Verse", ["movie:1", "movie:2"]),
                  "Qhawaii": franchises.fg.Group("Qhawaii", "series", "Lenkov-verse", ["tv:3", "tv:4"])}
        asked = {"tv:3": ["Qhawaii", "Qverse"], "tv:4": ["Qhawaii", "Qverse"]}
        answers = {key: (decision(one=0.9), asked[key]) for key in asked}
        automatic = {"movie:1": ("Qverse", None), "movie:2": ("Qverse", None)}
        got, _ = franchises.resolve(titles, groups, {}, automatic, asked, answers, {})
        self.assertEqual(set(got), {"Qverse", "Qhawaii"})

    def name_word_pair(self, **extra):
        """Die Hard and A Hard Day's Night: two groups with no title in common, each title listing both
        and saying they are one franchise."""
        titles = {"movie:1": franchises.fg.Title("movie:1", "Die Hard", 1988, **extra.get("movie:1", {})),
                  "movie:2": franchises.fg.Title("movie:2", "Die Hard 2", 1990),
                  "movie:3": franchises.fg.Title("movie:3", "A Hard Day's Night", 1964,
                                                 **extra.get("movie:3", {})),
                  "movie:4": franchises.fg.Title("movie:4", "Help!", 1965)}
        groups = {"Qdie": franchises.fg.Group("Qdie", "series", "Die Hard", ["movie:1", "movie:2"]),
                  "Qday": franchises.fg.Group("Qday", "series", "A Hard Day's Night", ["movie:3", "movie:4"])}
        asked = {"movie:1": ["Qdie", "Qday"], "movie:2": ["Qdie", "Qday"],
                 "movie:3": ["Qday", "Qdie"], "movie:4": ["Qday", "Qdie"]}
        answers = {key: (decision(one=0.9), asked[key]) for key in asked}
        return titles, groups, asked, answers

    def test_groups_sharing_only_a_name_word_are_not_merged_on_the_vote_alone(self):
        titles, groups, asked, answers = self.name_word_pair()
        got, _ = franchises.resolve(titles, groups, {}, {}, asked, answers, {})
        self.assertEqual(set(got), {"Qdie", "Qday"})

    def test_a_continuing_cast_merges_two_groups_sharing_only_a_name_word(self):
        cast = {"cast": ["Qa", "Qb", "Qc"]}
        titles, groups, asked, answers = self.name_word_pair(**{"movie:1": cast, "movie:3": cast})
        groups["Qday"] = franchises.fg.Group("Qday", "book-series", "A Hard Day's Night",
                                              ["movie:3", "movie:4"])
        got, _ = franchises.resolve(titles, groups, {}, {}, asked, answers, {})
        self.assertEqual(set(got), {"Qday"})

        two = {"cast": ["Qa", "Qb"]}
        titles, groups, asked, answers = self.name_word_pair(**{"movie:1": two, "movie:3": two})
        groups["Qday"] = franchises.fg.Group("Qday", "book-series", "A Hard Day's Night",
                                              ["movie:3", "movie:4"])
        got, _ = franchises.resolve(titles, groups, {}, {}, asked, answers, {})
        self.assertEqual(set(got), {"Qdie", "Qday"}, "two shared actors are not a continuing cast")

    def test_cast_overlap_does_not_join_two_explicit_production_series(self):
        cast = {"cast": ["Qa", "Qb", "Qc"]}
        titles, groups, asked, answers = self.name_word_pair(**{"movie:1": cast, "movie:3": cast})
        got, _ = franchises.resolve(titles, groups, {}, {}, asked, answers, {})
        self.assertEqual(set(got), {"Qdie", "Qday"})

    def test_a_wikidata_series_holding_titles_of_both_merges_them(self):
        titles, groups, asked, answers = self.name_word_pair()
        groups["Qline"] = franchises.fg.Group("Qline", "franchise", "The line", ["movie:1", "movie:3"])
        got, _ = franchises.resolve(titles, groups, {}, {}, asked, answers, {})
        self.assertEqual(set(got), {"Qday"})

        groups["Qline"] = franchises.fg.Group("Qline", "book-series", "The books", ["movie:1", "movie:3"])
        got, _ = franchises.resolve(titles, groups, {}, {}, asked, answers, {})
        self.assertEqual(set(got), {"Qdie", "Qday"}, "a book series' adaptations are no line")

    def test_a_title_choosing_a_disjoint_group_does_not_merge_them(self):
        titles, groups, asked, answers = self.name_word_pair()
        answers["movie:4"] = (decision(choice="B", one=0.9), asked["movie:4"])
        got, _ = franchises.resolve(titles, groups, {}, {}, asked, answers, {})
        self.assertEqual(set(got), {"Qdie", "Qday"})
        self.assertEqual(set(got["Qdie"]["members"]), {"movie:1", "movie:2"})
        self.assertEqual(set(got["Qday"]["members"]), {"movie:3", "movie:4"})

    def test_titles_of_one_year_are_in_release_order_where_wikidata_dates_them(self):
        titles = {"movie:1": franchises.fg.Title("movie:1", "October", 1997, date="1997-10-31"),
                  "movie:2": franchises.fg.Title("movie:2", "June", 1997, date="1997-06-27")}
        groups = {"Qs": franchises.fg.Group("Qs", "series", "Series", titles)}
        grouped = {"Qs": {"name": "Series", "source": franchises.WIKIDATA, "confidence": 1.0, "umbrellaOf": {},
                          "members": {"movie:1": None, "movie:2": None}}}
        doc, _ = franchises.document(grouped, groups, titles, {})
        self.assertEqual([m["key"] for m in doc["Qs"]["members"]], ["movie:2", "movie:1"])

    def test_a_tv_title_in_a_mixed_group_gets_its_own_era(self):
        titles = {"movie:1": franchises.fg.Title("movie:1", "The film", 2000),
                  "tv:2": franchises.fg.Title("tv:2", "The series", 2001)}
        groups = {"Qmixed": franchises.fg.Group("Qmixed", "franchise", "Mixed", titles)}
        got, _ = franchises.resolve(titles, groups, {},
                                    {"movie:1": ("Qmixed", None), "tv:2": ("Qmixed", None)},
                                    {}, {}, {})
        self.assertEqual(got["Qmixed"]["members"],
                         {"movie:1": None, "tv:2": "adaptation:tv:2"})

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

    def test_a_paid_row_whose_article_provenance_was_changed_is_refused(self):
        self.run_stage(spend=True)
        answers, _ = self.paid_files()
        with open(answers, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh]
        rows[0]["articleSha256"] = "0" * 64
        with open(answers, "w", encoding="utf-8") as fh:
            fh.write("".join(json.dumps(row) + "\n" for row in rows))
        with self.assertRaisesRegex(StageError, "article content hash differs"):
            self.run_stage()

    def test_a_rehashed_manifest_for_other_franchise_questions_is_refused(self):
        self.run_stage(spend=True)
        answers, manifest_path = self.paid_files()
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        manifest["config"]["globalQuestions"]["fr__group"]["instructions"] = "A different question"
        manifest["config"]["globalQuestionsSha256"] = rc.sha256_text(
            rc.canonical(manifest["config"]["globalQuestions"]))
        manifest["configSha256"] = rc.sha256_text(rc.canonical(manifest["config"]))
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)
        with open(answers, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh]
        for row in rows:
            row["configSha256"] = manifest["configSha256"]
        with open(answers, "w", encoding="utf-8") as fh:
            fh.write("".join(json.dumps(row) + "\n" for row in rows))
        with self.assertRaisesRegex(StageError, "does not record today's franchise questions"):
            self.run_stage()

    def test_plan_writes_nothing(self):
        report = self.run_stage(plan=True)
        self.assertIn("7 titles to ask", report)
        self.assertEqual(os.listdir(self.out).count("franchises.json"), 0)
        self.assertEqual([p for p in os.listdir(self.out) if p.startswith("franchise-")], [])


if __name__ == "__main__":
    unittest.main()
