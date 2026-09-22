#!/usr/bin/env python3
"""The facts stage — that it is a wrapper, and that the merge finally has an owner.

The merge rule is `scripts/merge-facts.py` and is not re-tested here. What is tested is the stage around
it, which had to answer one question no other stage does: its output has the same NAME as one of its
inputs. Both passes of `taxonomy-backfill facts` write `facts-<version>.json`, and so does the merge, so
the declaration is the only thing keeping the three apart — and a declaration that let them collide would
produce a facts file merged with itself, which looks exactly like a correct one.

The equivalence oracle is the real merge on a real pair of inputs, built here rather than taken from
`out-repass/`: the merge is pure local file joining, so the fixture exercises the same code path the
38,669-plus-8,949 pair does, and a test that needed a 43 MB artifact on disk would be a test that only
runs on one machine.
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

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION = "testver"

#: What `write_inputs` lays down, by artifact. The DECLARED names, so a test that writes these and runs
#: the stage with no overrides exercises the filename templates too — including the one that separates the
#: corpus pass from the file the merge writes over it.
FIXTURE_FILES = {
    "corpus_facts": f"facts-{VERSION}.pre-merge.json",
    "delta_facts": "facts-unversioned.json",
}


def pass_file(path, keys, has_vector, entities=None, genre_map=None):
    """One `taxonomy-backfill facts` output: `hasVector` stamped on every record of the pass."""
    records = [{"mediaType": media, "tmdbId": tmdb, "hasVector": has_vector,
                "countries": ["US"], "imdbId": f"tt{tmdb:07d}"} for media, tmdb in keys]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"schema": 1, "datasetVersion": "whichever", "genreMap": genre_map or {},
                   "entities": entities or {}, "records": records}, fh)


#: One title in both passes, so the collision rule is exercised: ("movie", 1) is scraped by the corpus
#: pass with a vector and by the delta pass without one.
CORPUS_KEYS = (("movie", 1), ("movie", 2), ("tv", 9))
DELTA_KEYS = (("movie", 1), ("movie", 77))


def write_inputs(out):
    """Both passes, under their declared filenames."""
    pass_file(os.path.join(out, FIXTURE_FILES["corpus_facts"]), CORPUS_KEYS, True,
              entities={"Q42": {"en": "Ada Director"}}, genre_map={"Q1": {"movie": 18}})
    pass_file(os.path.join(out, FIXTURE_FILES["delta_facts"]), DELTA_KEYS, False,
              entities={"Q7": {"en": "Bo Writer"}}, genre_map={"Q2": {"tv": 99}})


def context(out, **kwargs):
    return Context(out_dir=out, dataset_version=VERSION, **kwargs)


class Declaration(unittest.TestCase):
    def test_the_stage_reads_the_two_passes_and_writes_the_file_that_ships(self):
        self.assertEqual([bind(e).name for e in facts.INPUTS], ["corpus_facts", "delta_facts"])
        self.assertEqual([bind(e).name for e in facts.OUTPUTS], ["facts"])

    def test_the_corpus_pass_comes_first_because_first_wins(self):
        """The merge resolves a collision in favour of its FIRST argument — the pass that carries the
        vector and the fuller scrape. Swapped, every overlapping title would ship as vectorless, and
        /recommend drops a vectorless record out of the ANN path rather than reporting anything."""
        self.assertEqual(bind(facts.INPUTS[0]).artifact, artifacts.CORPUS_FACTS)

    def test_the_three_facts_files_do_not_share_a_filename(self):
        """Both passes and the merge all write `facts-<version>.json` if nothing separates them, and a
        facts file merged with its own output is indistinguishable from a correct one."""
        names = {artifacts.CORPUS_FACTS.filename, artifacts.DELTA_FACTS.filename,
                 artifacts.FACTS.filename}
        self.assertEqual(len(names), 3)

    def test_the_merge_buys_nothing(self):
        self.assertFalse(facts.SPENDS)


class CommandLine(unittest.TestCase):
    def test_the_command_is_the_one_the_scripts_docstring_writes(self):
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            command = facts.argv(context(out))
            self.assertEqual(command[1], facts.SCRIPT)
            self.assertEqual(command[2:5], [os.path.join(out, FIXTURE_FILES["corpus_facts"]),
                                            os.path.join(out, FIXTURE_FILES["delta_facts"]),
                                            os.path.join(out, f"facts-{VERSION}.json")])
            self.assertEqual(command[5:], ["--version", VERSION])

    def test_the_version_is_stated_rather_than_inherited(self):
        """Left off, the merged file takes its `datasetVersion` from the corpus pass — which is the
        previous generation whenever the merge is what a new one is built on. `facts-5b1c3213b6a1.json`
        in `out-repass` carries `c85c707b0b18` for exactly that reason."""
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            facts.run(context(out))
            with open(os.path.join(out, f"facts-{VERSION}.json"), encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["datasetVersion"], VERSION)

    def test_a_missing_pass_stops_the_stage_and_names_the_move(self):
        """The corpus pass is the one that has to be renamed, so its refusal has to say so: an operator
        who ran the scrape and skipped the move has the file, under the name the merge is about to write
        over."""
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            os.remove(os.path.join(out, FIXTURE_FILES["corpus_facts"]))
            with self.assertRaises(StageError) as refused:
                facts.argv(context(out))
            self.assertIn("pre-merge", str(refused.exception))

    def test_a_missing_delta_pass_stops_the_stage(self):
        """The 137-record failure, as a refusal. A merge with the delta pass absent writes a facts file
        that parses, validates and is short by every title only that pass covers."""
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            os.remove(os.path.join(out, FIXTURE_FILES["delta_facts"]))
            with self.assertRaises(StageError) as refused:
                facts.argv(context(out))
            self.assertIn("taxonomy-backfill facts", str(refused.exception))


class Topology(unittest.TestCase):
    def test_the_facts_file_is_owned_by_the_stage_that_outputs_it(self):
        """It named `taxonomy-backfill facts` until now — the scrape, which is half of what makes it. The
        registry answers with the merge, because the merge is what the pipeline runs to write this file."""
        self.assertEqual(artifacts.FACTS.producer, "")
        self.assertEqual(pipeline.producers()["facts"], (facts.PRODUCER, facts.HOW, True))

    def test_the_stage_runs_the_script_it_is_registered_as(self):
        self.assertEqual(facts.SCRIPT, os.path.join(REPO, facts.PRODUCER))
        self.assertTrue(os.path.isfile(facts.SCRIPT))

    def test_the_merge_runs_before_everything_that_reads_the_merged_file(self):
        for reader in (corpus, store):
            self.assertIn(artifacts.FACTS, [bind(e).artifact for e in reader.INPUTS])
            self.assertLess(pipeline.STAGES.index("facts"), pipeline.STAGES.index(reader.NAME))

    def test_both_passes_still_name_the_scrape_that_writes_them(self):
        """The seam: the facts scrape is not a stage, so its two outputs answer for themselves."""
        registered = pipeline.producers()
        self.assertEqual(registered["corpus_facts"][0], artifacts.BACKFILL)
        self.assertEqual(registered["delta_facts"][0], artifacts.BACKFILL)


class Equivalence(unittest.TestCase):
    def hand_typed(self, out, target):
        """The command as `merge-facts.py`'s own docstring writes it."""
        done = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "merge-facts.py"),
                               os.path.join(out, FIXTURE_FILES["corpus_facts"]),
                               os.path.join(out, FIXTURE_FILES["delta_facts"]),
                               target, "--version", VERSION], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_stage_writes_the_bytes_the_hand_typed_command_writes(self):
        """`den stage facts` against the command in the script's docstring, on one pair of passes.

        Byte-identical is available here and so it is the bar: the merge is a local join of two JSON files
        with no clock, no compression and no randomness in it, so anything that moved a byte would be a
        second implementation rather than a wrapper.
        """
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            reference = os.path.join(out, "reference.json")
            self.hand_typed(out, reference)

            made = facts.run(context(out))
            self.assertEqual(made, os.path.join(out, f"facts-{VERSION}.json"))
            with open(reference, "rb") as fh:
                expected = fh.read()
            with open(made, "rb") as fh:
                self.assertEqual(fh.read(), expected)

    def test_a_merge_the_script_refuses_is_a_refusal_here(self):
        """A wrapper that swallowed the script's own guards would be worse than no wrapper. The schema
        check is the cheapest of them to trip."""
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            path = os.path.join(out, FIXTURE_FILES["delta_facts"])
            with open(path, encoding="utf-8") as fh:
                blob = json.load(fh)
            blob["schema"] = 2
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(blob, fh)
            with self.assertRaises(StageError):
                facts.run(context(out))


class Records(unittest.TestCase):
    def test_every_record_of_both_passes_survives_and_the_corpus_pass_wins(self):
        """Not a second test of the merge — a check that the wrapper hands both passes over, so the file
        it produces holds the union. A stage that dropped the delta flag would still write a facts file,
        and it would be quietly short by exactly the titles nothing else covers."""
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            with open(facts.run(context(out)), encoding="utf-8") as fh:
                merged = json.load(fh)
            rows = {(r["mediaType"], r["tmdbId"]): r for r in merged["records"]}
            self.assertEqual(sorted(rows), sorted(set(CORPUS_KEYS) | set(DELTA_KEYS)))
            self.assertTrue(rows[("movie", 1)]["hasVector"], "the overlapping title lost its vector")
            self.assertFalse(rows[("movie", 77)]["hasVector"], "a delta-only title gained one")
            self.assertEqual(sorted(merged["entities"]), ["Q42", "Q7"])
            self.assertEqual(sorted(merged["genreMap"]), ["Q1", "Q2"])


if __name__ == "__main__":
    unittest.main()
