#!/usr/bin/env python3
"""The doc-facts stage — the resume, and the two distinctions the file has to keep.

  * **an id Wikidata states neither fact for is written EMPTY, not skipped.** Absent and empty mean
    different things one level up, and recording the empty row is also what stops the resume re-querying
    it forever;
  * **a file that will not parse is a refusal.** Starting over is ~770 requests nobody asked for, and it
    overwrites the rows that ARE there.

Scraping is stubbed: what is under test is which ids are asked for, in which batches, and what is written.
Equivalence with the Swift `doc-facts` was measured separately (oxyc/den-dataset#27) by replaying 2,000
shipped titles out of `.cache/wiki` — the two files were byte-identical at 148,506 bytes.
"""
import json
import os
import tempfile
import unittest

import pipeline

from . import artifacts, docfacts
from .contract import Context, StageError, bind

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION = "testver"


class Staged(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.out = self.directory.name
        self.asked = []
        self.answers = {}
        self.original = docfacts.wikidata.doc_facts
        docfacts.wikidata.doc_facts = self.stub
        # The items claiming each (media, id), and what each item states.
        self.claimants, self.evidence, self.excluded = {}, {}, []
        self.originals = {name: getattr(docfacts.wikidata, name)
                          for name in ("claimants", "item_evidence", "load_decisions")}
        # The committed decisions name real ids the stubs say nothing claims; read, they refuse as stale.
        docfacts.wikidata.load_decisions = lambda path=None: {}
        docfacts.wikidata.claimants = lambda ids, media, cache=None: {
            i: self.claimants[(media, i)] for i in ids if (media, i) in self.claimants}
        docfacts.wikidata.item_evidence = lambda qids, media, cache=None: {
            q: self.evidence[q] for q in qids if q in self.evidence}

    def tearDown(self):
        docfacts.wikidata.doc_facts = self.original
        for name, original in self.originals.items():
            setattr(docfacts.wikidata, name, original)
        self.directory.cleanup()

    def stub(self, ids, media, cache=None, excluded=None):
        self.asked.append((media, list(ids)))
        self.excluded.append((media, excluded))
        return {tmdb_id: self.answers[(media, tmdb_id)]
                for tmdb_id in ids if (media, tmdb_id) in self.answers}

    def labels(self, records):
        """`genres-moods.json` holding `records`, keyed the way the genres & moods stage writes it."""
        titles = {f"{r['mediaType']}:{r['tmdbId']}": {k: r[k] for k in ("animated", "moods", "primaryGenre",
                                                                         "subgenres")} for r in records}
        with open(os.path.join(self.out, "genres-moods.json"), "w", encoding="utf-8") as fh:
            json.dump({"taxonomyVersion": "t02", "count": len(titles), "titles": titles}, fh)

    def record(self, tmdb_id, media="movie"):
        return {"tmdbId": tmdb_id, "mediaType": media, "primaryGenre": "Drama",
                "subgenres": [], "moods": [], "animated": False}

    def context(self, **kwargs):
        return Context(out_dir=self.out, dataset_version=VERSION, **kwargs)

    def facts(self):
        with open(os.path.join(self.out, "doc-facts.json"), encoding="utf-8") as fh:
            return json.load(fh)


class Writing(Staged):
    def test_an_id_with_neither_fact_is_recorded_empty_rather_than_left_out(self):
        """The embed stage reads an empty row as "no clause"; a missing key is a title the scrape never
        reached. Recording it is also what stops the resume re-querying it forever."""
        self.labels([self.record(11), self.record(12)])
        self.answers = {("movie", 11): {"directors": ["Lucas"], "genres": ["space opera"]}}
        docfacts.run(self.context())
        self.assertEqual(self.facts(), {"movie:11": {"directors": ["Lucas"], "genres": ["space opera"]},
                                        "movie:12": {"directors": [], "genres": []}})

    def test_a_contested_title_is_asked_about_one_item_and_says_which(self):
        """Series 2559 is claimed by "Boon" and by "Bonn", which also states its own id. The director and
        genres composed into its document are Boon's alone — and a row written before the choice existed,
        which merged both, is asked again."""
        self.labels([self.record(2559, "tv")])
        self.claimants[("tv", 2559)] = ["Q132860965", "Q116226000"]
        self.evidence = {"Q116226000": {"claims": [2559, 215780], "articles": []},
                         "Q132860965": {"claims": [2559], "articles": []}}
        docfacts.write(os.path.join(self.out, "doc-facts.json"), {"tv:2559": {"directors": ["Both"], "genres": []}})
        docfacts.run(self.context())
        self.assertEqual(self.excluded, [("tv", {2559: ["Q116226000"]})])
        self.assertEqual(self.facts()["tv:2559"], {"directors": [], "genres": [], "wikidataItem": "Q132860965",
                                                   "wikidataCandidates": ["Q116226000", "Q132860965"]})

    def test_the_key_is_media_qualified(self):
        """TMDB's id spaces overlap — movie 95 is Armageddon, series 95 is Buffy — and the composed
        document would otherwise take one title's director for the other."""
        self.labels([self.record(95, "movie"), self.record(95, "tv")])
        self.answers = {("movie", 95): {"directors": ["Bay"], "genres": []},
                        ("tv", 95): {"directors": [], "genres": ["supernatural drama"]}}
        docfacts.run(self.context())
        self.assertEqual(sorted(self.facts()), ["movie:95", "tv:95"])
        self.assertEqual(self.facts()["movie:95"]["directors"], ["Bay"])

    def test_the_file_is_the_swift_encoders_bytes(self):
        """A golden, not a round trip through `write`: keys sorted (so `movie:12` before `movie:3`),
        compact, `/` escaped and UTF-8 left raw. The Swift scrape wrote the file this stage resumes, and a
        second spelling of it moves every hash taken of it for a reason that is not a change in any fact."""
        path = os.path.join(self.out, "doc-facts.json")
        docfacts.write(path, {"tv:5": {"genres": ["action/adventure"], "directors": []},
                              "movie:3": {"directors": ["Agnès Varda"], "genres": []},
                              "movie:12": {"directors": ["Luc Besson"], "genres": ["science fiction"]}})
        golden = ('{"movie:12":{"directors":["Luc Besson"],"genres":["science fiction"]},'
                  '"movie:3":{"directors":["Agnès Varda"],"genres":[]},'
                  '"tv:5":{"directors":[],"genres":["action\\/adventure"]}}')
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), golden.encode("utf-8"))

    def test_a_write_that_fails_part_way_leaves_the_previous_file(self):
        """The file is rewritten after every batch, and `existing` refuses one that does not parse. A
        failure between truncating it and refilling it left a 0-byte file, and the next run refused to
        continue the very scrape it was interrupted in."""
        path = os.path.join(self.out, "doc-facts.json")
        good = {f"movie:{n}": {"directors": ["X"], "genres": []} for n in range(100)}
        docfacts.write(path, good)
        with self.assertRaises(TypeError):
            docfacts.write(path, dict(good, **{"movie:zz": {"directors": [object()], "genres": []}}))
        self.assertEqual(docfacts.existing(path), good)

    def test_two_runs_over_one_corpus_write_one_file(self):
        self.labels([self.record(n) for n in range(1, 30)])
        docfacts.run(self.context())
        with open(os.path.join(self.out, "doc-facts.json"), "rb") as fh:
            first = fh.read()
        os.remove(os.path.join(self.out, "doc-facts.json"))
        docfacts.run(self.context())
        with open(os.path.join(self.out, "doc-facts.json"), "rb") as fh:
            self.assertEqual(fh.read(), first)


class Batching(Staged):
    def test_ids_are_asked_for_in_batches_of_the_pinned_size(self):
        self.labels([self.record(n) for n in range(1, 251)])
        docfacts.run(self.context())
        self.assertEqual([len(ids) for _media, ids in self.asked], [100, 100, 50])

    def test_a_batch_holds_one_media(self):
        """Two id spaces, two TMDB-id properties — `P4947` for a film and `P4983` for a series. A mixed
        batch asks one property about ids from both and silently returns half an answer."""
        self.labels([self.record(n) for n in range(1, 5)] +
                    [self.record(n, "tv") for n in range(1, 5)])
        docfacts.run(self.context())
        self.assertEqual([media for media, _ids in self.asked], ["movie", "tv"])

    def test_the_batch_size_is_pinned_rather_than_taken_from_a_flag(self):
        """The membership of a batch is part of the SPARQL cache key, so a run that grouped the ids
        differently re-asks Wikidata for every title already on disk under a name the cache never saw."""
        self.assertEqual(docfacts.BATCH, 100)


class Resume(Staged):
    def test_an_id_already_in_the_file_is_not_re_queried(self):
        """A 38.5k-title scrape WILL be interrupted, and re-running from zero is how a polite scrape turns
        into an impolite one."""
        self.labels([self.record(11), self.record(12)])
        with open(os.path.join(self.out, "doc-facts.json"), "w", encoding="utf-8") as fh:
            json.dump({"movie:11": {"directors": ["Lucas"], "genres": []}}, fh)
        docfacts.run(self.context())
        self.assertEqual(self.asked, [("movie", [12])])
        self.assertEqual(self.facts()["movie:11"]["directors"], ["Lucas"])

    def test_a_file_that_will_not_parse_is_a_refusal_rather_than_a_fresh_start(self):
        self.labels([self.record(11)])
        with open(os.path.join(self.out, "doc-facts.json"), "w", encoding="utf-8") as fh:
            fh.write('{"movie:11": {"directo')
        with self.assertRaises(StageError) as refused:
            docfacts.run(self.context())
        self.assertIn("Refusing to start over", str(refused.exception))
        self.assertEqual(self.asked, [])

    def test_a_corpus_already_covered_asks_nothing_and_still_reports(self):
        self.labels([self.record(11)])
        with open(os.path.join(self.out, "doc-facts.json"), "w", encoding="utf-8") as fh:
            json.dump({"movie:11": {"directors": [], "genres": []}}, fh)
        docfacts.run(self.context())
        self.assertEqual(self.asked, [])


class Refusal(Staged):
    def test_a_missing_genres_moods_file_is_refused_with_what_builds_it(self):
        with self.assertRaises(StageError) as refused:
            docfacts.run(self.context())
        self.assertIn("./den stage genres_moods", str(refused.exception))

    def test_a_genres_moods_file_with_no_titles_is_refused(self):
        self.labels([])
        with self.assertRaises(StageError) as refused:
            docfacts.run(self.context())
        self.assertIn("holds no genres & moods titles", str(refused.exception))
        self.assertIn("./den stage genres_moods", str(refused.exception))


class Topology(unittest.TestCase):
    def test_the_facts_are_owned_by_the_stage_that_writes_them(self):
        self.assertEqual(artifacts.DOC_FACTS.producer, "")
        self.assertEqual(pipeline.producers()["doc_facts"],
                         (docfacts.PRODUCER, docfacts.HOW, True))
        self.assertTrue(os.path.isfile(os.path.join(REPO, docfacts.PRODUCER)))

    def test_it_runs_before_the_pass_that_composes_with_it(self):
        """They are two clauses of the embedding document. A run without them composes the FULL shape
        instead of the CC0 one — a different vector space, with nothing in the output saying so."""
        order = list(pipeline.STAGES)
        self.assertLess(order.index("docfacts"), order.index("embed"))
        self.assertIn("doc_facts", [bind(e).name for e in pipeline.stage("embed").INPUTS])

    def test_the_titles_are_this_runs_genres_and_moods(self):
        """Not `labels-t02.json`, which `finalize` writes after this stage: reading it made a fresh out-dir
        unable to start."""
        self.assertEqual([bind(e).artifact for e in docfacts.INPUTS], [artifacts.GENRES_MOODS])
        order = list(pipeline.STAGES)
        self.assertLess(order.index("genres_moods"), order.index("docfacts"))

    def test_the_cheaper_path_is_still_there(self):
        """`scripts/v2/derive_doc_facts.py` builds the same file out of the facts sidecar, validated
        against a partial scrape at 100.00% on directors and 99.98% on genres. This stage is what runs
        when there is no sidecar to derive from, and the two must not drift."""
        derive = os.path.join(REPO, "scripts", "v2", "derive_doc_facts.py")
        self.assertTrue(os.path.isfile(derive))
        with open(derive, encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("from lib.wikidata import stripped_genre", source,
                      "the derivation kept its own copy of the genre rule, which is how it drifted before")


if __name__ == "__main__":
    unittest.main()
