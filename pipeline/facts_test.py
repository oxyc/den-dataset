#!/usr/bin/env python3
"""The facts stage — both scrape passes and the merge that ships.

Wikidata is stubbed at the four calls `lib/wikidata_facts.py` makes; what is under test is what the stage
does with the answers: which ids it asks about, what it checkpoints, what each pass writes and that the
merge gets both. Equivalence with the Swift `facts` was measured separately (oxyc/den-dataset#27): over
1,500 corpus titles and 500 delta ids, replayed from the cache the binary filled, the output and all three
checkpoints were byte-identical — bar one delta record whose P1476 has two values, which WDQS returns in no
fixed order and which the binary itself reads first-wins.
"""
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
        self.asked = {"facts": [], "titles": [], "names": [], "types": []}
        self.failing = set()    # (media, tmdbId) whose batch raises
        self.live = False       # answered from the cache, so nothing is paced

    def fetch_facts(self, ids, media, item, cache=None):
        self.asked["facts"].append((media, tuple(ids), item.key))
        # Late in the batch, so the properties before it have already landed on the row.
        if item.key == "cast" and any((media, i) in self.failing for i in ids):
            raise wd.WikidataError("maintenance page")
        found = {i: self.facts[(media, i)][item.key] for i in ids
                 if item.key in self.facts.get((media, i), {})}
        return found, self.live

    def titles_of(self, ids, media):
        self.asked["titles"].append((media, tuple(ids)))
        return {i: self.titles[(media, i)] for i in ids if (media, i) in self.titles}

    def entity_details(self, qids):
        self.asked["names"].append(tuple(qids))
        return {q: self.names[q] for q in qids if q in self.names}

    def instance_of(self, qids):
        self.asked["types"].append(tuple(qids))
        return {q: self.types[q] for q in qids if q in self.types}


class Staged(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.out = self.directory.name
        self.wd = Wikidata()
        for name, stub in (("fetch_facts", self.wd.fetch_facts), ("titles", self.wd.titles_of),
                           ("entity_details", self.wd.entity_details), ("instance_of", self.wd.instance_of)):
            original = getattr(facts.wd, name)
            setattr(facts.wd, name, stub)
            self.addCleanup(setattr, facts.wd, name, original)
        self.wd.facts = {
            ("movie", 1): {"imdbId": "tt1", "directors": ["Q10"], "genres": ["Q20"], "basedOn": ["Q30"],
                           "franchise": "Q40", "released": {"date": "1999", "precision": "year"}},
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
        self.assertEqual(record["franchise"], "Q40")

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
