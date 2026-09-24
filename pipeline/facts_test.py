#!/usr/bin/env python3
"""The facts stage — both scrape passes and the merge that ships.

Wikidata is stubbed at the four calls `lib/wikidata_facts.py` makes; what is under test is what the stage
does with the answers: which ids it asks about, what it checkpoints, what each pass writes and that the
merge gets both. Equivalence with the Swift `facts` was measured separately (oxyc/den-dataset#27): over
1,500 corpus titles and 500 delta ids, replayed from the cache the binary filled, the output and all three
checkpoints were byte-identical — bar one delta record whose P1476 has two values, which WDQS returns in no
fixed order and which the binary itself read first-wins. Three of the Swift's rules were then changed on
purpose, each measured in its commit: a genre's rename keeps the rest of its entity, a title's strings are
chosen by rule rather than by row order, and a stated day wins over the year that contains it.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

import pipeline

from . import artifacts, corpus, facts, store
from .contract import Context, StageError, bind
from lib import wikidata_facts as wd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION = "testver"
SPEC = {item.key: item for item in wd.SPECS}


class Wikidata:
    """The four calls, answered from tables, every question recorded."""

    def __init__(self):
        self.facts = {}         # (media, tmdbId) -> {spec key: value}
        self.titles = {}        # (media, tmdbId) -> titles row
        self.names = {}         # qid -> entity details
        self.types = {}         # qid -> [P31 labels]
        self.members = {}       # qid -> P179 member count, for the targets that are a series
        self.nconst = {}        # qid -> IMDb person id
        self.links = {}         # award qid -> what it belongs to, as `wd.award_links` answers
        self.traits = {}        # person qid -> traits, as `wd.people` answers
        self.births = {}        # person qid -> birthplace row, as `wd.birthplaces` answers
        self.codes = {}         # country qid -> ISO code
        self.authors = {}       # work qid -> [author qid]
        self.asked = {"facts": [], "titles": [], "names": [], "types": [], "series": [], "imdb": [],
                      "awards": [], "people": [], "births": [], "codes": [], "authors": []}
        self.failing = set()    # (media, tmdbId) whose batch raises
        self.languages = []     # (media, the languages the titles hop was told) per call
        self.live = False       # answered from the cache, so nothing is paced
        # Items per (media, tmdbId), when more than the tables above answer for; each item's own facts and
        # evidence; and what each call was told to leave out.
        self.claimants = {}     # (media, tmdbId) -> [qid]
        self.by_item = {}       # qid -> {spec key: value}
        self.evidence = {}      # qid -> item_evidence row
        self.excluded = []

    def answer(self, media, tmdb_id, excluded, table):
        """A contested title answers from its items that were not left out, merged the way WDQS merges
        rows keyed by the TMDB id — which is the bug when more than one is left in."""
        qids = self.claimants.get((media, tmdb_id))
        if not qids:
            return table.get((media, tmdb_id), {})
        kept = [q for q in qids if q not in (excluded or {}).get(tmdb_id, ())]
        merged = {}
        for qid in kept:
            for name, value in self.by_item.get(qid, {}).items():
                if isinstance(value, list):
                    merged[name] = sorted(set(merged.get(name, [])) | set(value))
                else:
                    # The least value wins, as `collapse` and `titles` choose among several.
                    merged[name] = min((merged[name], value) if name in merged else (value,),
                                       key=lambda v: json.dumps(v, sort_keys=True))
        return merged

    def fetch_facts(self, ids, media, item, cache=None, excluded=None):
        self.asked["facts"].append((media, tuple(ids), item.key))
        self.excluded.append((media, excluded))
        # Late in the batch, so the properties before it have already landed on the row.
        if item.key == "cast" and any((media, i) in self.failing for i in ids):
            raise wd.WikidataError("maintenance page")
        found = {i: self.answer(media, i, excluded, self.facts)[item.key] for i in ids
                 if item.key in self.answer(media, i, excluded, self.facts)}
        return found, self.live

    def titles_of(self, ids, media, languages=None, excluded=None):
        self.asked["titles"].append((media, tuple(ids)))
        self.languages.append((media, languages))
        out = {}
        for i in ids:
            found = self.answer(media, i, excluded, {k: {"t": v} for k, v in self.titles.items()})
            if "t" in found:
                out[i] = found["t"]
        return out

    def claimants_of(self, ids, media, cache=None):
        return {i: self.claimants[(media, i)] for i in ids if (media, i) in self.claimants}

    def item_evidence(self, qids, media, cache=None):
        return {q: self.evidence[q] for q in qids if q in self.evidence}

    def entity_details(self, qids):
        self.asked["names"].append(tuple(qids))
        return {q: self.names[q] for q in qids if q in self.names}

    def instance_of(self, qids):
        self.asked["types"].append(tuple(qids))
        return {q: self.types[q] for q in qids if q in self.types}

    def series(self, qids, cache=None):
        self.asked["series"].append(tuple(qids))
        return {q: self.members[q] for q in qids if q in self.members}

    def award_links(self, qids, cache=None):
        self.asked["awards"].append(tuple(qids))
        return {q: self.links[q] for q in qids if q in self.links}

    def imdb_ids(self, qids, cache=None):
        self.asked["imdb"].append(tuple(qids))
        return {q: self.nconst[q] for q in qids if q in self.nconst}

    def people(self, qids, cache=None):
        self.asked["people"].append(tuple(qids))
        return {q: self.traits[q] for q in qids if q in self.traits}

    def birthplaces(self, qids, cache=None):
        self.asked["births"].append(tuple(qids))
        return {q: self.births[q] for q in qids if q in self.births}

    def country_codes(self, qids, cache=None):
        self.asked["codes"].append(tuple(qids))
        return {q: self.codes[q] for q in qids if q in self.codes}

    def authors_of(self, qids, cache=None):
        self.asked["authors"].append(tuple(qids))
        return {q: self.authors[q] for q in qids if q in self.authors}


class Staged(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.out = self.directory.name
        self.wd = Wikidata()
        for name, stub in (("fetch_facts", self.wd.fetch_facts), ("titles", self.wd.titles_of),
                           ("entity_details", self.wd.entity_details), ("instance_of", self.wd.instance_of),
                           ("series", self.wd.series), ("imdb_ids", self.wd.imdb_ids),
                           ("award_links", self.wd.award_links), ("people", self.wd.people),
                           ("birthplaces", self.wd.birthplaces), ("country_codes", self.wd.country_codes),
                           ("authors", self.wd.authors_of)):
            original = getattr(facts.wd, name)
            setattr(facts.wd, name, stub)
            self.addCleanup(setattr, facts.wd, name, original)
        # The committed decisions name real ids the stubs say nothing claims; read, they refuse as stale.
        for name, stub in (("claimants", self.wd.claimants_of), ("item_evidence", self.wd.item_evidence),
                           ("load_decisions", lambda path=None: {})):
            original = getattr(facts.wikidata, name)
            setattr(facts.wikidata, name, stub)
            self.addCleanup(setattr, facts.wikidata, name, original)
        self.wd.facts = {
            ("movie", 1): {"imdbId": "tt1", "directors": ["Q10"], "genres": ["Q20"], "basedOn": ["Q30"],
                           "franchise": ["Q40"], "released": {"date": "1999", "precision": "year"}},
            ("tv", 1): {"imdbId": "tt9", "broadcaster": ["Q50"]},
            ("movie", 7): {"imdbId": "tt7"},
        }
        self.wd.titles = {("movie", 1): {"article": "One (film)", "label": "One", "original": None,
                                         "aliases": ["Uno", "Eins"]}}
        self.wd.names = {"Q10": {"name": "Ada Director", "tmdbPersonId": "55", "aliases": ["A. D.", "Ada"]},
                         "Q20": {"name": "science fiction film", "tmdbPersonId": None, "aliases": ["sf"]},
                         "Q40": {"name": "The Saga", "tmdbPersonId": None, "aliases": []},
                         "Q50": {"name": "HBO", "tmdbPersonId": None, "aliases": []}}
        self.wd.types = {"Q30": ["novel", "literary work"]}
        self.wd.members = {"Q40": 3}
        with open(os.path.join(self.out, "labels-t02.json"), "w", encoding="utf-8") as fh:
            json.dump({"records": [{"mediaType": "movie", "tmdbId": 1}, {"mediaType": "tv", "tmdbId": 1}]}, fh)
        with open(os.path.join(self.out, "facts-delta-ids.txt"), "w", encoding="utf-8") as fh:
            fh.write("movie:7\nmovie:1\n")
        with open(os.path.join(self.out, "dataset.meta.json"), "w", encoding="utf-8") as fh:
            json.dump({"datasetVersion": VERSION, "labelsFile": "labels-t02.json"}, fh)

    def context(self, version=VERSION):
        return Context(out_dir=self.out, dataset_version=version)

    def run_stage(self, version=VERSION):
        return facts.run(self.context(version), cache=object())

    def read(self, name):
        with open(os.path.join(self.out, name), encoding="utf-8") as fh:
            return json.load(fh)


class Passes(Staged):
    def test_each_pass_states_hasvector_for_every_record_it_writes(self):
        """/recommend must never let a vectorless record into an ANN path, and one pass cannot state both."""
        self.run_stage()
        corpus_pass = self.read(f"facts-{VERSION}.pre-merge.json")
        delta_pass = self.read(f"facts-{VERSION}.delta.json")
        self.assertEqual({(r["mediaType"], r["tmdbId"], r["hasVector"]) for r in corpus_pass["records"]},
                         {("movie", 1, True), ("tv", 1, True)})
        self.assertEqual({(r["mediaType"], r["tmdbId"], r["hasVector"]) for r in delta_pass["records"]},
                         {("movie", 1, False), ("movie", 7, False)})

    def test_a_checkpointed_title_no_longer_asked_for_is_not_written(self):
        """The checkpoint outlives the labels it was scraped for. A title the classify pass later dropped is
        still in it, and written out it would claim a vector it no longer has."""
        self.run_stage()
        with open(os.path.join(self.out, "labels-t02.json"), "w", encoding="utf-8") as fh:
            json.dump({"records": [{"mediaType": "movie", "tmdbId": 1}]}, fh)
        self.run_stage()
        self.assertEqual(sorted(self.read("facts-fields.json")), ["movie:1", "tv:1"], "the checkpoint keeps it")
        corpus_pass = self.read(f"facts-{VERSION}.pre-merge.json")
        self.assertEqual([(r["mediaType"], r["tmdbId"]) for r in corpus_pass["records"]], [("movie", 1)])
        merged = self.read(f"facts-{VERSION}.json")
        self.assertNotIn(("tv", 1, True), {(r["mediaType"], r["tmdbId"], r["hasVector"]) for r in merged["records"]})

    def test_the_passes_keep_separate_checkpoints(self):
        """Shared, the delta file would carry every corpus record, stamped vectorless."""
        self.run_stage()
        self.assertEqual(sorted(self.read("facts-fields.json")), ["movie:1", "tv:1"])
        self.assertEqual(sorted(self.read("facts-delta/facts-fields.json")), ["movie:1", "movie:7"])

    def test_the_record_as_it_ships(self):
        self.run_stage()
        record = next(r for r in self.read(f"facts-{VERSION}.pre-merge.json")["records"]
                      if r["mediaType"] == "movie")
        self.assertEqual(record["titles"], {"aliases": ["Eins", "Uno"], "en": "One", "orig": "One"})
        self.assertEqual(record["basedOnKind"], ["book"])
        self.assertEqual(record["released"], {"date": "1999", "precision": "year"})
        self.assertEqual(record["franchise"], ["Q40"])

    def test_the_entity_map_as_it_ships(self):
        """A single-valued Q-id is resolved too — a harvest that walked only lists left all 3,019 franchises
        unnamed. Aliases are a list at the boundary: a joined string made a 27 MB file unparseable for
        atlas. A genre loses its medium from its name, and nothing else."""
        self.run_stage()
        shipped = self.read(f"facts-{VERSION}.pre-merge.json")
        self.assertEqual(shipped["entities"]["Q10"], {"aliases": ["A. D.", "Ada"], "en": "Ada Director",
                                                      "tmdbPersonId": "55"})
        self.assertEqual(shipped["entities"]["Q20"], {"aliases": ["sf"], "en": "science fiction"})
        self.assertEqual(shipped["entities"]["Q40"], {"en": "The Saga"})
        self.assertEqual(shipped["genreMap"], {"Q20": {"movie": 878, "tv": 10765}})
        self.assertEqual(self.read("facts-entities.json")["Q10"]["aliases"], "A. D.\x1fAda")

    def test_a_genre_that_is_also_a_person_keeps_what_the_person_needs(self):
        """One Q-id, one entry: a composer credit on one title and a genre on another share it, so the genre
        rename must not take the composer's person id and aliases with it."""
        self.wd.facts[("movie", 1)]["genres"] = ["Q20", "Q60"]
        self.wd.facts[("tv", 1)]["composers"] = ["Q60"]
        self.wd.names["Q60"] = {"name": "drama film", "tmdbPersonId": "99", "aliases": ["drama movie"]}
        self.run_stage()
        self.assertEqual(self.read(f"facts-{VERSION}.pre-merge.json")["entities"]["Q60"],
                         {"aliases": ["drama movie"], "en": "drama", "tmdbPersonId": "99"})

    def test_an_unknown_source_work_is_remembered_so_it_is_not_asked_again(self):
        del self.wd.types["Q30"]
        self.run_stage()
        self.assertEqual(self.read("facts-source-types.json"), {"Q30": []})
        self.assertNotIn("basedOnKind", self.read("facts-fields.json")["movie:1"],
                         "derived kinds are not checkpointed")

    def test_a_source_work_with_no_type_has_no_kind(self):
        """Adapted from a novel and from a work Wikidata types as nothing: the kind is the novel's, and a
        title adapted only from the untyped one ships no kind at all."""
        self.wd.facts[("movie", 1)]["basedOn"] = ["Q30", "Q31"]
        self.wd.facts[("tv", 1)]["basedOn"] = ["Q31"]
        self.run_stage()
        records = {(r["mediaType"], r["tmdbId"]): r for r in self.read(f"facts-{VERSION}.pre-merge.json")["records"]}
        self.assertEqual(records[("movie", 1)]["basedOnKind"], ["book"])
        self.assertNotIn("basedOnKind", records[("tv", 1)])

    def test_the_titles_hop_is_told_each_titles_languages(self):
        """The original title is chosen in the title's own language, which the property requests fetched."""
        self.wd.facts[("tv", 1)]["languages"] = ["JA"]
        self.run_stage()
        self.assertIn(("tv", {1: ["JA"]}), self.wd.languages)

    def test_the_file_is_the_swift_encoders_bytes(self):
        """Keys sorted by code point, compact, `/` escaped, UTF-8 raw. Measured identical to the binary's
        over 1,500 titles; pinned here on one."""
        self.wd.facts = {("movie", 1): {"imdbId": "tt1", "genres": ["Q20"]}, ("tv", 1): {}}
        self.wd.titles = {("movie", 1): {"article": None, "label": "Amélie/2", "original": None, "aliases": []}}
        self.wd.names, self.wd.types = {"Q20": {"name": "drama film", "tmdbPersonId": None, "aliases": []}}, {}
        self.run_stage()
        with open(os.path.join(self.out, f"facts-{VERSION}.pre-merge.json"), "rb") as fh:
            self.assertEqual(fh.read().decode("utf-8"),
                             '{"datasetVersion":"testver","entities":{"Q20":{"en":"drama"}},'
                             '"genreMap":{"Q20":{"movie":18,"tv":18}},"records":[{"genres":["Q20"],'
                             '"hasVector":true,"imdbId":"tt1","mediaType":"movie",'
                             '"titles":{"en":"Amélie\\/2","orig":"Amélie\\/2"},"tmdbId":1}],"schema":1}')


class Franchise(Staged):
    """P179 also files a title into critics' and editors' lists. WALL-E's targets are "BBC's 100 Greatest
    Films of the 21st Century" (Q26705935, a Wikimedia list article) and a real series; the list sorts
    first, so the older scrape shipped it as the franchise and linked WALL-E to Amélie and Inception."""

    LIST, SERIES, CATALOG = "Q26705935", "Q9000", "Q56070713"

    def shipped(self):
        return {(r["mediaType"], r["tmdbId"]): r for r in self.read(f"facts-{VERSION}.pre-merge.json")["records"]}

    def rescrape(self, targets):
        """The corpus pass from scratch, with movie:1 filed under `targets`."""
        for name in ("facts-fields.json", "facts-entities.json"):
            if os.path.exists(os.path.join(self.out, name)):
                os.remove(os.path.join(self.out, name))
        self.wd.facts[("movie", 1)]["franchise"] = targets
        self.run_stage()

    def test_a_list_is_not_a_franchise_whatever_order_the_targets_arrive_in(self):
        self.wd.members = {self.SERIES: 4}
        for targets in ([self.LIST, self.SERIES], [self.SERIES, self.LIST]):
            with self.subTest(targets=targets):
                self.rescrape(targets)
                self.assertEqual(self.shipped()[("movie", 1)]["franchise"], [self.SERIES])
                self.assertNotIn(self.LIST, self.read(f"facts-{VERSION}.pre-merge.json")["entities"])
                self.assertEqual(sorted(self.read("facts-fields.json")["movie:1"]["franchise"]),
                                 sorted(targets), "the checkpoint keeps every target")

    def test_a_title_filed_only_under_a_list_has_no_franchise(self):
        self.wd.facts[("movie", 1)]["franchise"] = [self.LIST]
        self.run_stage()
        self.assertNotIn("franchise", self.shipped()[("movie", 1)])

    def test_the_most_specific_series_comes_first(self):
        """A studio's canon is typed a film series too; the story's own series is the one two titles share
        because they are one story. Fewest members first, whatever order the targets came in."""
        self.wd.members = {self.SERIES: 4, self.CATALOG: 67}
        for targets in ([self.CATALOG, self.SERIES, self.LIST], [self.LIST, self.SERIES, self.CATALOG]):
            with self.subTest(targets=targets):
                self.rescrape(targets)
                self.assertEqual(self.shipped()[("movie", 1)]["franchise"], [self.SERIES, self.CATALOG])

    def test_a_title_that_is_a_series_its_siblings_name_belongs_to_it(self):
        """The Beck television series is the item its films are "part of the series" of, and nothing states
        P179 from an item to itself: the series joins its own franchise, so it shares one with its films.
        Derived, like the rest of the franchise, so the checkpoint is unchanged. A title whose own item is
        a list, or is nobody's target, gains nothing."""
        self.wd.members = {self.SERIES: 27}
        self.wd.facts[("movie", 1)]["franchise"] = [self.SERIES]
        self.wd.claimants[("tv", 1)] = [self.SERIES]
        self.wd.by_item[self.SERIES] = {"imdbId": "tt9", "broadcaster": ["Q50"]}
        self.run_stage()
        self.assertEqual(self.shipped()[("tv", 1)]["franchise"], [self.SERIES])
        self.assertEqual(self.shipped()[("movie", 1)]["franchise"], [self.SERIES])
        self.assertNotIn("franchise", self.read("facts-fields.json")["tv:1"])
        for own in (self.LIST, "Q9999"):
            with self.subTest(own=own):
                os.remove(os.path.join(self.out, "facts-fields.json"))
                self.wd.facts[("movie", 1)]["franchise"] = [self.SERIES, self.LIST]
                self.wd.claimants[("tv", 1)] = [own]
                self.wd.by_item[own] = {"imdbId": "tt9"}
                self.run_stage()
                self.assertNotIn("franchise", self.shipped()[("tv", 1)])

    def test_a_row_an_older_scrape_collapsed_is_asked_again(self):
        """The older scrape kept only the least Q-id — the list — so the series was never checkpointed."""
        self.wd.members = {self.SERIES: 4}
        self.wd.facts[("movie", 1)]["franchise"] = [self.LIST, self.SERIES]
        self.run_stage()
        fields = self.read("facts-fields.json")
        fields["movie:1"]["franchise"] = self.LIST
        with open(os.path.join(self.out, "facts-fields.json"), "w", encoding="utf-8") as fh:
            json.dump(fields, fh)
        asked = len(self.wd.asked["facts"])
        self.run_stage()
        self.assertEqual(self.wd.asked["facts"][asked:], [("movie", (1,), "franchise")])
        self.assertEqual(self.read("facts-fields.json")["movie:1"]["franchise"], [self.LIST, self.SERIES])
        self.assertEqual(self.shipped()[("movie", 1)]["franchise"], [self.SERIES])


class Backfill(Staged):
    """Properties added after a checkpoint was written (oxyc/den#135): awards, and people's IMDb ids."""

    def test_a_row_scraped_before_the_awards_is_asked_for_them_once(self):
        self.wd.facts[("movie", 1)]["awardsWon"] = ["Q900"]
        self.run_stage()
        fields = self.read("facts-fields.json")
        self.assertEqual(fields["tv:1"]["awardsWon"], [], "asked, and has none")
        for row in fields.values():
            for key in facts.BACKFILLED:
                del row[key]
        with open(os.path.join(self.out, "facts-fields.json"), "w", encoding="utf-8") as fh:
            json.dump(fields, fh)
        asked = len(self.wd.asked["facts"])
        self.run_stage()
        self.assertEqual(sorted(self.wd.asked["facts"][asked:]),
                         sorted((media, (1,), key) for media in ("movie", "tv") for key in facts.BACKFILLED))
        self.assertEqual(self.read("facts-fields.json")["movie:1"]["awardsWon"], ["Q900"])
        asked = len(self.wd.asked["facts"])
        self.run_stage()
        self.assertEqual(self.wd.asked["facts"][asked:], [], "a row that was asked is not asked again")

    def test_awards_ship_by_ceremony_and_a_win_outranks_a_nomination(self):
        """Won Best Picture, nominated for Best Director at the same ceremony and for a Golden Globe: the
        record says won at the Academy Awards and nominated only at the Globes. The ceremonies are named;
        the categories are not, and an award no ceremony is found for ships under neither."""
        self.wd.facts[("movie", 1)].update(awardsWon=["Q102427"],
                                           awardsNominated=["Q103360", "Q1011547", "Q193622"])
        self.wd.links = {"Q102427": {"P361": [("Q19020", True)]}, "Q103360": {"P361": [("Q19020", True)]},
                         "Q1011547": {"self": [("Q1011547", True)]}, "Q193622": {"P31": [("Q1", False)]}}
        self.wd.names.update({"Q19020": {"name": "Academy Awards", "tmdbPersonId": None, "aliases": []},
                              "Q1011547": {"name": "Golden Globe Awards", "tmdbPersonId": None,
                                           "aliases": []}})
        self.run_stage()
        shipped = self.read(f"facts-{VERSION}.pre-merge.json")
        record = next(r for r in shipped["records"] if r["mediaType"] == "movie")
        self.assertEqual(record["awardsWonAt"], ["Q19020"])
        self.assertEqual(record["awardsNominatedAt"], ["Q1011547"])
        self.assertEqual(record["awardsWon"], ["Q102427"], "the award items ship too")
        self.assertEqual(shipped["entities"]["Q19020"], {"en": "Academy Awards"})
        self.assertNotIn("Q102427", shipped["entities"])
        self.assertNotIn(("Q102427",), self.wd.asked["names"])
        self.assertNotIn("awardsWonAt", self.read("facts-fields.json")["movie:1"], "derived, not checkpointed")

    def test_an_empty_award_list_does_not_ship(self):
        """`[]` is the checkpoint's "asked, has none"; a record carries the field only when it has awards."""
        self.run_stage()
        for record in self.read(f"facts-{VERSION}.pre-merge.json")["records"]:
            for key in facts.BACKFILLED:
                self.assertNotIn(key, record)

    def test_a_person_imdb_id_ships_and_is_asked_once(self):
        self.wd.nconst = {"Q10": "nm0000010"}
        self.run_stage()
        shipped = self.read(f"facts-{VERSION}.pre-merge.json")["entities"]
        self.assertEqual(shipped["Q10"]["imdbId"], "nm0000010")
        self.assertNotIn("imdbId", shipped["Q50"])
        self.assertEqual(self.read("facts-entities.json")["Q50"]["imdbId"], "", "asked, has none")
        asked = len(self.wd.asked["imdb"])
        self.run_stage()
        self.assertEqual(self.wd.asked["imdb"][asked:], [], "an entity that was asked is not asked again")

    def test_an_entity_named_before_imdb_ids_existed_is_asked(self):
        self.run_stage()
        names = self.read("facts-entities.json")
        for entry in names.values():
            del entry["imdbId"]
        with open(os.path.join(self.out, "facts-entities.json"), "w", encoding="utf-8") as fh:
            json.dump(names, fh)
        self.wd.nconst = {"Q10": "nm0000010"}
        self.run_stage()
        self.assertEqual(self.read("facts-entities.json")["Q10"]["imdbId"], "nm0000010")


class FranchiseEvidence(Staged):
    """What groups titles into a franchise beyond P179 (oxyc/den-atlas#92): the media franchise, the sequel
    order and the fictional characters. Asked of every row once, and shipped as raw Q-ids; only the media
    franchise is named, because a franchise row shows it."""

    def test_the_evidence_ships_and_only_the_media_franchise_is_named(self):
        self.wd.facts[("movie", 1)].update(mediaFranchise=["Q60"], follows=["Q61"], followedBy=["Q62"],
                                           characters=["Q63"])
        self.wd.names.update({q: {"name": f"name {q}", "tmdbPersonId": None, "aliases": []}
                              for q in ("Q60", "Q61", "Q62", "Q63")})
        self.run_stage()
        shipped = self.read(f"facts-{VERSION}.pre-merge.json")
        record = next(r for r in shipped["records"] if r["mediaType"] == "movie" and r["tmdbId"] == 1)
        self.assertEqual((record["mediaFranchise"], record["follows"], record["followedBy"], record["characters"]),
                         (["Q60"], ["Q61"], ["Q62"], ["Q63"]))
        self.assertEqual(shipped["entities"]["Q60"], {"en": "name Q60"})
        named = {q for batch in self.wd.asked["names"] for q in batch}
        self.assertEqual(named & {"Q61", "Q62", "Q63"}, set(), "a sequel or a character is not named")
        for q in ("Q61", "Q62", "Q63"):
            self.assertNotIn(q, shipped["entities"])

    def test_a_title_with_no_evidence_ships_none_and_is_not_asked_again(self):
        self.run_stage()
        record = next(r for r in self.read(f"facts-{VERSION}.pre-merge.json")["records"] if r["mediaType"] == "tv")
        for key in facts.FRANCHISE_EVIDENCE:
            self.assertNotIn(key, record)
            self.assertEqual(self.read("facts-fields.json")["tv:1"][key], [], "asked, has none")
        asked = len(self.wd.asked["facts"])
        self.run_stage()
        self.assertEqual(self.wd.asked["facts"][asked:], [])

    def test_the_evidence_does_not_count_as_an_award(self):
        """Awards are filed by ceremony from `AWARDS` alone; a character is never asked for as an award."""
        self.wd.facts[("movie", 1)].update(characters=["Q63"])
        self.run_stage()
        asked = {q for batch in self.wd.asked["awards"] for q in batch}
        self.assertNotIn("Q63", asked)


class PersonTraits(Staged):
    """Gender, birth, death, citizenship and occupation, for credited people only (oxyc/den#136)."""

    TRAITS = {"gender": ["Q6581072"], "born": {"date": "1946-06-14", "precision": "day"},
              "occupation": ["Q2526255"]}

    def test_a_persons_traits_ship_by_name_and_are_asked_once(self):
        """Q10 directs movie:1. The genre, the source work, the series and the broadcaster are no one's
        credit, so they are not asked. The items a trait names are named, and ship like any entity."""
        self.wd.traits = {"Q10": self.TRAITS}
        self.wd.names["Q6581072"] = {"name": "female", "tmdbPersonId": None, "aliases": []}
        self.run_stage()
        # Once per pass: each keeps its own entity checkpoint.
        self.assertEqual(self.wd.asked["people"], [("Q10",), ("Q10",)])
        shipped = self.read(f"facts-{VERSION}.pre-merge.json")["entities"]
        self.assertEqual({k: shipped["Q10"][k] for k in self.TRAITS}, self.TRAITS)
        self.assertEqual(shipped["Q6581072"], {"en": "female"})
        self.assertIn(("Q2526255", "Q6581072"), self.wd.asked["names"], "the trait values are named")
        asked = len(self.wd.asked["people"])
        self.run_stage()
        self.assertEqual(self.wd.asked["people"][asked:], [], "a person who was asked is not asked again")

    def test_a_person_with_no_traits_is_remembered_as_asked(self):
        self.run_stage()
        self.assertEqual(self.read("facts-entities.json")["Q10"]["traits"], {}, "asked, has none")
        shipped = self.read(f"facts-{VERSION}.pre-merge.json")["entities"]["Q10"]
        self.assertFalse(set(shipped) & {"gender", "born", "died", "citizenship", "occupation", "traits"})

    def test_a_person_named_before_traits_existed_is_asked(self):
        self.run_stage()
        names = self.read("facts-entities.json")
        for entry in names.values():
            entry.pop("traits", None)
        with open(os.path.join(self.out, "facts-entities.json"), "w", encoding="utf-8") as fh:
            json.dump(names, fh)
        self.wd.traits = {"Q10": self.TRAITS}
        self.run_stage()
        self.assertEqual(self.read("facts-entities.json")["Q10"]["traits"], self.TRAITS)


class Birthplaces(Staged):
    """Where a credited person was born, and the country that place is in (oxyc/den-dataset#114)."""

    BIRTH = {"birthplace": ["Q1754"], "birthcountry": ["Q34"]}

    def test_a_birthplace_ships_named_with_its_countrys_code_and_is_asked_once(self):
        """Q10 directs movie:1. The place and the country are named like any entity, and the country, and
        the country of Q10's citizenship, are asked for their ISO code."""
        self.wd.births = {"Q10": self.BIRTH}
        self.wd.traits = {"Q10": {"citizenship": ["Q33"]}}
        self.wd.names.update({"Q1754": {"name": "Stockholm", "tmdbPersonId": None, "aliases": []},
                              "Q34": {"name": "Sweden", "tmdbPersonId": None, "aliases": []},
                              "Q33": {"name": "Finland", "tmdbPersonId": None, "aliases": []}})
        self.wd.codes = {"Q34": "SE", "Q33": "FI"}
        self.run_stage()
        self.assertEqual(self.wd.asked["births"], [("Q10",), ("Q10",)])
        shipped = self.read(f"facts-{VERSION}.pre-merge.json")["entities"]
        self.assertEqual({k: shipped["Q10"][k] for k in self.BIRTH}, self.BIRTH)
        self.assertEqual(shipped["Q1754"], {"en": "Stockholm"})
        self.assertEqual((shipped["Q34"], shipped["Q33"]["iso"]), ({"en": "Sweden", "iso": "SE"}, "FI"))
        self.assertIn(("Q33", "Q34"), self.wd.asked["codes"])
        asked = {name: len(self.wd.asked[name]) for name in ("births", "codes")}
        self.run_stage()
        self.assertEqual({name: self.wd.asked[name][asked[name]:] for name in asked},
                         {"births": [], "codes": []}, "asked once, answer or none")

    def test_a_person_with_no_birthplace_and_a_country_with_no_code_are_remembered(self):
        self.wd.births = {"Q10": {"birthplace": ["Q1754"], "birthcountry": ["Q15180"]}}
        self.wd.names["Q15180"] = {"name": "Soviet Union", "tmdbPersonId": None, "aliases": []}
        self.run_stage()
        names = self.read("facts-entities.json")
        self.assertEqual(names["Q15180"]["iso"], "", "asked, has none")
        self.assertNotIn("iso", self.read(f"facts-{VERSION}.pre-merge.json")["entities"]["Q15180"])
        self.wd.births = {}
        os.remove(os.path.join(self.out, "facts-entities.json"))
        self.run_stage()
        self.assertEqual(self.read("facts-entities.json")["Q10"]["birth"], {}, "asked, has none")
        shipped = self.read(f"facts-{VERSION}.pre-merge.json")["entities"]["Q10"]
        self.assertFalse(set(shipped) & {"birthplace", "birthcountry", "birth"})

    def test_a_person_asked_for_traits_before_birthplaces_existed_is_asked(self):
        """A checkpoint from #100's facts stage records `traits` and no `birth`: asked once, traits not
        again."""
        self.run_stage()
        names = self.read("facts-entities.json")
        for entry in names.values():
            entry.pop("birth", None)
        with open(os.path.join(self.out, "facts-entities.json"), "w", encoding="utf-8") as fh:
            json.dump(names, fh)
        self.wd.births = {"Q10": self.BIRTH}
        people = len(self.wd.asked["people"])
        self.run_stage()
        self.assertEqual(self.read("facts-entities.json")["Q10"]["birth"], self.BIRTH)
        self.assertEqual(self.wd.asked["people"][people:], [], "the traits were asked already")


class SourceAuthors(Staged):
    """Who wrote the work a title is adapted from: P50 of each `basedOn` target (oxyc/den-dataset#114)."""

    def test_a_titles_source_authors_ship_named_and_each_work_is_asked_once(self):
        """movie:1 adapts Q30 and Q31, tv:1 adapts Q31. Q11 wrote both works, so it is listed once; the
        authors are named, though nothing credits them, and are not asked for person traits."""
        self.wd.facts[("movie", 1)]["basedOn"] = ["Q30", "Q31"]
        self.wd.facts[("tv", 1)]["basedOn"] = ["Q31"]
        self.wd.authors = {"Q30": ["Q12", "Q11"], "Q31": ["Q11"]}
        self.wd.names["Q11"] = {"name": "A Novelist", "tmdbPersonId": None, "aliases": ["A. N."]}
        self.run_stage()
        shipped = self.read(f"facts-{VERSION}.pre-merge.json")
        authors = {(r["mediaType"], r["tmdbId"]): r.get("sourceAuthors") for r in shipped["records"]}
        self.assertEqual(authors, {("movie", 1): ["Q11", "Q12"], ("tv", 1): ["Q11"]})
        self.assertEqual(shipped["entities"]["Q11"], {"en": "A Novelist", "aliases": ["A. N."]})
        self.assertEqual(self.read("facts-source-authors.json"), {"Q30": ["Q12", "Q11"], "Q31": ["Q11"]})
        self.assertNotIn("Q11", {q for batch in self.wd.asked["people"] for q in batch})
        asked = len(self.wd.asked["authors"])
        self.run_stage()
        self.assertEqual(self.wd.asked["authors"][asked:], [], "a work that was asked is not asked again")

    def test_a_work_with_no_author_is_remembered_and_its_title_has_none(self):
        self.run_stage()
        self.assertEqual(self.read("facts-source-authors.json"), {"Q30": []})
        record = next(r for r in self.read(f"facts-{VERSION}.pre-merge.json")["records"]
                      if r["mediaType"] == "movie")
        self.assertNotIn("sourceAuthors", record)


class Resume(Staged):
    def test_a_finished_title_is_not_asked_again(self):
        """Nor is a name already resolved: re-resolving all of them on every restart is how a resumed run
        at 16,500 titles spent its whole life resolving and never reached a new batch. A Q-id Wikidata
        names nothing for (Q30 here) is asked again, as it always was."""
        self.run_stage()
        asked = len(self.wd.asked["facts"])
        self.run_stage()
        self.assertEqual(len(self.wd.asked["facts"]), asked)
        self.assertEqual(self.wd.asked["names"][2:], [("Q30",), ("Q30",)])

    def test_a_failed_batch_is_dropped_whole_and_the_stage_refuses(self):
        """A row holding the properties that landed before the timeout would read as finished and ship
        short. And a pass that skipped batches is not finished, so nothing is merged from it."""
        self.wd.failing = {("tv", 1)}
        with self.assertRaises(StageError) as refused:
            self.run_stage()
        self.assertIn("skipped", str(refused.exception))
        self.assertNotIn("tv:1", self.read("facts-fields.json"))
        self.assertFalse(os.path.exists(os.path.join(self.out, f"facts-{VERSION}.json")))
        self.wd.failing = set()
        self.run_stage()
        self.assertIn("tv:1", self.read("facts-fields.json"))

    def test_a_checkpoint_that_will_not_parse_is_refused(self):
        with open(os.path.join(self.out, "facts-fields.json"), "w", encoding="utf-8") as fh:
            fh.write("{torn")
        with self.assertRaises(StageError) as refused:
            self.run_stage()
        self.assertIn("does not parse", str(refused.exception))
        self.assertEqual(self.wd.asked["facts"], [])

    def test_the_batches_are_the_ones_the_cache_was_filled_with(self):
        """The id batch is inside the query text, and the query text is the cache key."""
        with open(os.path.join(self.out, "labels-t02.json"), "w", encoding="utf-8") as fh:
            json.dump({"records": [{"mediaType": "movie", "tmdbId": i} for i in range(60)]}, fh)
        self.run_stage()
        sizes = {len(ids) for media, ids, key in self.wd.asked["facts"] if key == "imdbId" and 0 in ids[:1]}
        self.assertEqual(facts.BATCH, 25)
        self.assertEqual(sizes, {25})

    def test_a_cached_answer_is_not_paced(self):
        slept = []
        original = facts.time.sleep
        facts.time.sleep = slept.append
        self.addCleanup(setattr, facts.time, "sleep", original)
        self.run_stage()
        self.assertEqual(slept, [])
        self.wd.live = True
        os.remove(os.path.join(self.out, "facts-fields.json"))
        self.run_stage()
        self.assertTrue(slept and set(slept) == {facts.PACE})


class Merge(Staged):
    def test_the_merged_file_is_the_hand_typed_merges_bytes(self):
        made = self.run_stage()
        reference = os.path.join(self.out, "reference.json")
        done = subprocess.run([sys.executable, facts.SCRIPT, os.path.join(self.out, f"facts-{VERSION}.pre-merge.json"),
                               os.path.join(self.out, f"facts-{VERSION}.delta.json"), reference,
                               "--version", VERSION], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        with open(reference, "rb") as a, open(made, "rb") as b:
            self.assertEqual(a.read(), b.read())

    def test_the_corpus_pass_wins_a_collision(self):
        """The first file the merge takes wins. Swapped, every overlapping title would ship vectorless."""
        merged = self.read(os.path.basename(self.run_stage()))
        rows = {(r["mediaType"], r["tmdbId"]): r for r in merged["records"]}
        self.assertTrue(rows[("movie", 1)]["hasVector"])
        self.assertFalse(rows[("movie", 7)]["hasVector"])
        self.assertEqual(merged["datasetVersion"], VERSION)

    def test_a_missing_delta_list_stops_the_stage_and_names_where_it_comes_from(self):
        """The 137-record failure, as a refusal: a merge without the delta pass is short by every title only
        it covers."""
        os.remove(os.path.join(self.out, "facts-delta-ids.txt"))
        with self.assertRaises(StageError) as refused:
            self.run_stage()
        self.assertIn("docs/OPERATE.md", str(refused.exception))


class Version(Staged):
    def test_the_version_is_the_manifests(self):
        """Left off, it is read from the manifest finalize wrote, and names every file and the merged file's
        own `datasetVersion` alike."""
        made = self.run_stage(version="")
        self.assertEqual(os.path.basename(made), f"facts-{VERSION}.json")
        self.assertEqual(self.read(os.path.basename(made))["datasetVersion"], VERSION)
        self.assertEqual(self.read(f"facts-{VERSION}.pre-merge.json")["datasetVersion"], VERSION)

    def test_a_version_that_disagrees_with_the_manifest_is_refused_before_anything_is_written(self):
        """Obeyed, it would write facts-<flag>.json for a generation it does not describe — the shipped file
        renamed by hand, again."""
        with self.assertRaises(StageError) as refused:
            self.run_stage(version="c85c707b0b18")
        self.assertIn(f"Pass --dataset-version {VERSION}", str(refused.exception))
        self.assertEqual(self.wd.asked["facts"], [])
        self.assertFalse([name for name in os.listdir(self.out) if name.startswith("facts-c85c")])

    def test_no_manifest_is_refused_naming_what_writes_it(self):
        os.remove(os.path.join(self.out, "dataset.meta.json"))
        with self.assertRaises(StageError) as refused:
            self.run_stage(version="")
        self.assertIn("./den stage finalize", str(refused.exception))


BONN, BOON = "Q116226000", "Q132860965"


class OneItemPerTitle(Staged):
    """Series 2559, as Wikidata has it: "Boon" (1986) states the TMDB id, and so does "Bonn – Alte Freunde,
    neue Feinde" (2023), which also states its own 215780 and carries Boon's 1986 start date. Every query
    was keyed by the TMDB id, so the shipped row read "Bonn" with Boon's IMDb id."""

    def setUp(self):
        super().setUp()
        with open(os.path.join(self.out, "labels-t02.json"), "w", encoding="utf-8") as fh:
            json.dump({"records": [{"mediaType": "tv", "tmdbId": 2559}, {"mediaType": "movie", "tmdbId": 1}]}, fh)
        self.wd.by_item = {
            BONN: {"imdbId": "tt13905034", "cast": ["Q76172"], "started": {"date": "1986-01-14", "precision": "day"},
                   "released": {"date": "2022-10-22", "precision": "day"},
                   "t": {"article": "Bonn – Alte Freunde, neue Feinde", "label": "Bonn", "original": None,
                         "aliases": []}},
            BOON: {"imdbId": "tt0090400", "cast": ["Q5362535"],
                   "t": {"article": "Boon (TV series)", "label": "Boon", "original": None, "aliases": []}}}
        self.wd.evidence = {BONN: {"claims": [2559, 215780], "articles": ["Bonn – Alte Freunde, neue Feinde"]},
                            BOON: {"claims": [2559], "articles": ["Boon (TV series)"]}}

    def record(self):
        return next(r for r in self.read(f"facts-{VERSION}.pre-merge.json")["records"]
                    if (r["mediaType"], r["tmdbId"]) == ("tv", 2559))

    def test_every_field_comes_from_the_item_chosen(self):
        """Bonn also states its own TMDB id, so Boon is the one that states this id alone."""
        for order in ([BOON, BONN], [BONN, BOON]):
            with self.subTest(order=order):
                for name in ("facts-fields.json", "facts-entities.json", "facts-source-types.json",
                             "facts-source-authors.json"):
                    if os.path.exists(os.path.join(self.out, name)):
                        os.remove(os.path.join(self.out, name))
                self.wd.claimants[("tv", 2559)] = order
                self.run_stage()
                record = self.record()
                self.assertEqual(record["titles"]["en"], "Boon")
                self.assertEqual(record["imdbId"], "tt0090400")
                self.assertEqual(record["cast"], ["Q5362535"])
                self.assertNotIn("started", record, "Bonn's dates are Bonn's")
                self.assertNotIn("released", record)
                self.assertEqual(record["wikidataItem"], BOON)
                self.assertEqual(record["wikidataCandidates"], [BONN, BOON])

    def test_an_uncontested_title_is_asked_as_before(self):
        self.wd.claimants[("tv", 2559)] = [BOON, BONN]
        self.run_stage()
        movie = next(r for r in self.read(f"facts-{VERSION}.pre-merge.json")["records"] if r["mediaType"] == "movie")
        self.assertNotIn("wikidataCandidates", movie)
        self.assertNotIn("wikidataItem", movie)
        told = {media: excluded for media, excluded in self.wd.excluded}
        self.assertFalse(told["movie"], "a batch holding no contested id is told nothing, so its query is as before")
        self.assertEqual(told["tv"], {2559: [BONN]})

    def test_a_title_nothing_singles_out_ships_no_field_from_either(self):
        self.ambiguous()
        self.run_stage()
        self.assertEqual(self.record(), {"mediaType": "tv", "tmdbId": 2559, "hasVector": True,
                                         "wikidataCandidates": [BONN, BOON]})

    def ambiguous(self):
        self.wd.claimants[("tv", 2559)] = [BOON, BONN]
        self.wd.evidence = {BONN: {"claims": [2559], "articles": []},
                            BOON: {"claims": [2559], "articles": []}}

    def test_an_ambiguous_title_is_counted_loudly_in_the_report(self):
        self.ambiguous()
        said = io.StringIO()
        with contextlib.redirect_stderr(said):
            self.run_stage()
        self.assertIn("WARNING: 1 titles ship with NO Wikidata fields", said.getvalue())
        self.assertIn("data/wikidata-item-decisions.json", said.getvalue())
        self.assertIn('"ambiguousItems": 1', said.getvalue())

    def test_a_decision_chooses_and_the_row_checkpointed_as_ambiguous_is_scraped_again(self):
        self.ambiguous()
        self.run_stage()
        self.assertNotIn("wikidataItem", self.record())
        original = facts.wikidata.load_decisions
        facts.wikidata.load_decisions = lambda path=None: {("tv", 2559): BOON}
        self.addCleanup(setattr, facts.wikidata, "load_decisions", original)
        self.run_stage()
        record = self.record()
        self.assertEqual((record["wikidataItem"], record["titles"]["en"], record["imdbId"]),
                         (BOON, "Boon", "tt0090400"))

    def test_a_checkpointed_row_that_merged_its_claimants_is_scraped_again(self):
        """A checkpoint written before the choice existed holds the merged row, and resuming would keep it."""
        self.wd.claimants[("tv", 2559)] = [BOON, BONN]
        with open(os.path.join(self.out, "facts-fields.json"), "w", encoding="utf-8") as fh:
            json.dump({"tv:2559": {"imdbId": "tt0090400", "titles": {"en": "Bonn"}}}, fh)
        self.run_stage()
        self.assertEqual(self.record()["titles"]["en"], "Boon")


class Topology(unittest.TestCase):
    def test_the_stage_owns_both_passes_and_the_merge(self):
        self.assertEqual([bind(e).name for e in facts.OUTPUTS], ["corpus_facts", "delta_facts", "facts"])
        for name in ("corpus_facts", "delta_facts", "facts"):
            self.assertEqual(pipeline.producers()[name], (facts.PRODUCER, facts.HOW, True))

    def test_the_three_files_do_not_share_a_name(self):
        names = {artifacts.CORPUS_FACTS.filename, artifacts.DELTA_FACTS.filename, artifacts.FACTS.filename}
        self.assertEqual(len(names), 3)

    def test_it_reads_the_labels_finalize_writes_and_runs_before_what_reads_it(self):
        self.assertLess(pipeline.STAGES.index("finalize"), pipeline.STAGES.index("facts"))
        for reader in (corpus, store):
            self.assertIn(artifacts.FACTS, [bind(e).artifact for e in reader.INPUTS])
            self.assertLess(pipeline.STAGES.index("facts"), pipeline.STAGES.index(reader.NAME))

    def test_the_delta_list_answers_for_itself(self):
        """Nothing in this repo derives it. Its registration names the document that says how to make it,
        which is a tracked file."""
        self.assertEqual(pipeline.producers()["delta_ids"][0], "docs/OPERATE.md")
        self.assertTrue(os.path.isfile(os.path.join(REPO, "docs", "OPERATE.md")))
        self.assertFalse(facts.SPENDS)


if __name__ == "__main__":
    unittest.main()
