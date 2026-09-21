#!/usr/bin/env python3
"""The corpus stage — that it is a wrapper, and that the two things this port added still hold.

The join itself is tested where it lives, in `scripts/v2/test_consolidate_corpus.py`: every guard in it
was bought by a failure that happened, and none of them is reimplemented here. What is tested here is the
stage around it, which had to answer two questions the store stage did not:

  * **a repeatable input.** `--combined` and `--delta` take a SET of shards, and the eleven titles that
    vanished for a day vanished because a producer read one shard of three. So the declaration names the
    set, not a file, and the stage passes the flag once per member — checked against the script's own
    parser rather than against a copy of its argument list;
  * **an output that is the next stage's input.** `corpus` is written here and read by `store`, so the
    producer registry can say who owns it from the ORDER rather than from a `producer` written on the
    artifact. The test for that is that the artifact carries no producer of its own and the registry
    still answers.

The fixture is `scripts/v2/test_consolidate_corpus.py`'s, reused rather than rebuilt: a second definition
of what a valid pass shard looks like is a second thing to keep true.
"""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

import pipeline

from . import artifacts, corpus, store
from .contract import Context, StageError, bind

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
V2 = os.path.join(REPO, "scripts", "v2")

sys.path.insert(0, V2)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = load("consolidate_corpus", os.path.join(V2, "consolidate_corpus.py"))
fixture = load("test_consolidate_corpus", os.path.join(V2, "test_consolidate_corpus.py"))

VERSION = "testver"

#: What `write_inputs` lays down, by artifact. The names are the DECLARED ones, so a test that writes
#: these and then runs the stage with no overrides exercises the filename templates too.
FIXTURE_FILES = {
    "combined": ("combined-v1-r2.jsonl", "combined-v1-r2-token-fallback.jsonl"),
    "delta": ("delta-v1.jsonl",),
    "facts": (f"facts-{VERSION}.json",),
    "vector_labels": ("labels-t02.json",),
    "premise_labels": ("labels-premise.json",),
}

#: Two pass shards, one title that carries facts and no pass row at all, and a delta answer — the joins
#: the stage has to hand over intact.
PASS_KEYS = ("movie:1", "movie:2", "tv:9")
FACTS_KEYS = PASS_KEYS + ("movie:77",)


def sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def write_inputs(out):
    """Every declared input, under its declared filename."""
    first, second = (os.path.join(out, n) for n in FIXTURE_FILES["combined"])
    fixture.write(first, [fixture.combined(1), fixture.combined(2)])
    fixture.write(second, [fixture.combined(9, media="tv")])
    fixture.write(os.path.join(out, FIXTURE_FILES["delta"][0]),
                  [{"mediaType": "movie", "tmdbId": 1,
                    "answers": {"critique__craft": {"p": 0.7}, "made_for_children": {"choice": "no"}}}])
    fixture.facts_file(os.path.join(out, FIXTURE_FILES["facts"][0]), list(FACTS_KEYS),
                       entities={"Q42": {"en": "Ada Director"}})
    fixture.labels_file(os.path.join(out, FIXTURE_FILES["vector_labels"][0]), list(PASS_KEYS))
    fixture.labels_file(os.path.join(out, FIXTURE_FILES["premise_labels"][0]), ["movie:1"])


def context(out, **kwargs):
    return Context(out_dir=out, dataset_version=VERSION, **kwargs)


class Declaration(unittest.TestCase):
    def test_the_stage_declares_exactly_what_the_script_reads(self):
        """The pin the derived registry hangs from, in the same shape the store stage uses: the names the
        stage hands over are the script's own argument names, so an input can only be added or dropped in
        one place without something going red."""
        self.assertEqual([bind(e).arg for e in corpus.INPUTS], list(script.INPUT_ARGS))

    def test_the_outputs_are_the_corpus_and_its_entity_sidecar(self):
        """The sidecar is written by the same run and is unreadable apart from the corpus, so a stage that
        declared only the corpus would leave 3.6 MB of entity names owned by nobody."""
        self.assertEqual([bind(e).name for e in corpus.OUTPUTS], ["corpus", "entities"])

    def test_the_labels_artifact_keeps_one_name_and_reaches_the_script_under_its_own(self):
        """`labels-t02.json` is `--vector-labels` to the store writer and `--labels` to this join. The
        FILE keeps one name across the pipeline — that is what `--set` and the producer registry key on —
        and the flag is the reader's word for it."""
        bound = {bind(e).name: bind(e) for e in corpus.INPUTS}["vector_labels"]
        self.assertEqual(bound.flag(), "--labels")
        self.assertEqual(bound.artifact, artifacts.VECTOR_LABELS)
        self.assertEqual(artifacts.VECTOR_LABELS.flag(), "--vector-labels")
        self.assertIn(artifacts.VECTOR_LABELS, [bind(e).artifact for e in store.INPUTS])


class CommandLine(unittest.TestCase):
    def test_the_scripts_own_parser_accepts_what_the_declaration_builds(self):
        """Run through the parser that will receive it, not against a copy of the argument list."""
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            parsed = script.build_parser().parse_args(corpus.argv(context(out))[2:])
            # Sorted, so one out-dir gives one command line. The join refuses a key that appears in two
            # shards, so the order it reads them in changes nothing it writes.
            self.assertEqual(parsed.combined,
                             sorted(os.path.join(out, n) for n in FIXTURE_FILES["combined"]))
            self.assertEqual(parsed.delta, [os.path.join(out, FIXTURE_FILES["delta"][0])])
            self.assertEqual(parsed.labels, os.path.join(out, "labels-t02.json"))
            self.assertEqual(parsed.premise_labels, os.path.join(out, "labels-premise.json"))
            self.assertEqual(parsed.out, os.path.join(out, f"corpus-{VERSION}.jsonl.gz"))

    def test_every_shard_of_a_set_is_handed_over(self):
        """The eleven-title bug, as a property of the declaration: the stage cannot pass one shard of a
        set, because it does not know the set as a file."""
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            command = corpus.argv(context(out))
            self.assertEqual(command.count("--combined"), 2)

    def test_a_shard_set_with_no_members_stops_the_stage(self):
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            for name in FIXTURE_FILES["combined"]:
                os.remove(os.path.join(out, name))
            with self.assertRaises(StageError) as refused:
                corpus.argv(context(out))
            self.assertIn("run_combined.py", str(refused.exception))

    def test_a_missing_required_input_stops_the_stage(self):
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            os.remove(os.path.join(out, FIXTURE_FILES["facts"][0]))
            with self.assertRaises(StageError) as refused:
                corpus.argv(context(out))
            self.assertIn("taxonomy-backfill facts", str(refused.exception))

    def test_the_expected_count_is_only_passed_when_one_is_named(self):
        """`--expect` is the guard that refuses a short run. Passing it unasked would make every run
        assert a count nobody declared."""
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            self.assertNotIn("--expect", corpus.argv(context(out)))
            command = corpus.argv(context(out, expect=4))
            self.assertEqual(command[command.index("--expect") + 1], "4")


class Topology(unittest.TestCase):
    def test_the_corpus_is_owned_by_the_stage_that_outputs_it(self):
        """The registry used to read a `producer` written on the artifact — a field that could name one
        script while the stage ran another. Now the stage that declares the artifact in `OUTPUTS` is the
        answer, and the artifact carries no producer to disagree with."""
        self.assertEqual(artifacts.CORPUS.producer, "")
        self.assertEqual(artifacts.ENTITIES.producer, "")
        registered = pipeline.producers()
        self.assertEqual(registered["corpus"], (corpus.PRODUCER, corpus.HOW, True))
        self.assertEqual(registered["entities"], (corpus.PRODUCER, corpus.HOW, True))

    def test_the_stage_runs_the_script_it_is_registered_as(self):
        """One spelling. A stage subprocessing a file other than the one the registry names is a store
        rebuilt by a rule nothing checked."""
        self.assertEqual(corpus.SCRIPT, os.path.join(REPO, corpus.PRODUCER))
        self.assertTrue(os.path.isfile(corpus.SCRIPT))

    def test_an_input_no_stage_produces_still_names_its_own_producer(self):
        """The seam. `facts` comes from a stage that is not ported, so it answers for itself until that
        stage lands — and the registry has to carry both kinds at once."""
        self.assertEqual(pipeline.producers()["facts"], (artifacts.FACTS.producer, artifacts.FACTS.how,
                                                         False))

    def test_the_corpus_is_built_before_the_store_reads_it(self):
        self.assertLess(pipeline.STAGES.index("corpus"), pipeline.STAGES.index("store"))
        self.assertIn(artifacts.CORPUS, [bind(e).artifact for e in store.INPUTS])


class Equivalence(unittest.TestCase):
    def hand_typed(self, out, target):
        """The command as `consolidate_corpus.py`'s own docstring writes it."""
        command = [sys.executable, os.path.join(V2, "consolidate_corpus.py")]
        for name in FIXTURE_FILES["combined"]:
            command += ["--combined", os.path.join(out, name)]
        command += ["--delta", os.path.join(out, FIXTURE_FILES["delta"][0]),
                    "--facts", os.path.join(out, FIXTURE_FILES["facts"][0]),
                    "--labels", os.path.join(out, FIXTURE_FILES["vector_labels"][0]),
                    "--premise-labels", os.path.join(out, FIXTURE_FILES["premise_labels"][0]),
                    "--expect", "4", "--out", target]
        done = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_stage_writes_the_bytes_the_hand_typed_command_writes(self):
        """`den stage corpus` against the command in the script's docstring, on one set of inputs.

        Byte-identical is the right bar: the writer sets `mtime=0` precisely so an unchanged corpus is
        an unchanged file, and a publish can skip re-uploading 40 MB. A wrapper that moved one byte would
        be a second writer.
        """
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            reference = os.path.join(out, "reference.jsonl.gz")
            self.hand_typed(out, reference)
            reference_entities = os.path.join(out, "reference-entities.json.gz")
            self.assertTrue(os.path.isfile(reference_entities))

            made = corpus.run(context(out, expect=4))
            self.assertEqual(made, os.path.join(out, f"corpus-{VERSION}.jsonl.gz"))
            self.assertEqual(sha256(made), sha256(reference))
            self.assertEqual(sha256(os.path.join(out, f"corpus-{VERSION}-entities.json.gz")),
                             sha256(reference_entities))

    def test_the_sidecar_lands_where_the_declaration_says_it_will(self):
        """The script derives the sidecar's path from `--out` rather than taking a flag for it, so the
        declaration and the derivation could disagree without anything noticing — and the sidecar would
        be written somewhere no stage and no publish guard looks."""
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            ctx = context(out, overrides={"corpus": os.path.join(out, "somewhere-else.jsonl.gz")})
            with self.assertRaises(StageError) as refused:
                corpus.run(ctx)
            self.assertIn("entities", str(refused.exception))

    def test_a_join_the_script_refuses_is_a_refusal_here(self):
        """The guards are the reason this file is trusted; a wrapper that swallowed one would be worse
        than no wrapper. `--expect` disagreeing with the row count is the cheapest of them to trip."""
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            with self.assertRaises(StageError):
                corpus.run(context(out, expect=999))


class Rows(unittest.TestCase):
    def test_the_stage_carries_the_joins_through_untouched(self):
        """Not a second test of the join — a check that the wrapper hands over every artifact, so the
        row it produces holds facts, labels, premise labels and the delta pass at once. A stage that
        dropped a flag would still write a corpus, and it would be quietly thinner."""
        with tempfile.TemporaryDirectory() as out:
            write_inputs(out)
            rows = {r["key"]: r for r in fixture.read(corpus.run(context(out, expect=4)))}
            self.assertEqual(sorted(rows), ["movie:1", "movie:2", "movie:77", "tv:9"])
            self.assertEqual(rows["movie:1"]["labels"]["primaryGenre"], "Crime")
            self.assertEqual(rows["movie:1"]["premiseLabels"]["primaryGenre"], "Crime")
            self.assertEqual(rows["movie:1"]["critique"]["craft"], {"p": 0.7})
            self.assertEqual(rows["movie:77"]["facts"]["countries"], ["US"], "the facts-only title")
            with open(os.path.join(out, f"corpus-{VERSION}-entities.json.gz"), "rb") as fh:
                import gzip
                self.assertEqual(json.loads(gzip.decompress(fh.read()))["Q42"]["en"], "Ada Director")


if __name__ == "__main__":
    unittest.main()
