#!/usr/bin/env python3
"""The stage contract — the declaration everything else is derived from.

Each test here is a way the declaration could be wrong while still looking fine, which is the only kind
of wrong that survives: `STORE_INPUTS` drifting from the writer twice in one day was never a visible
mistake, it was a second list that nobody had a reason to open.
"""
import os
import tempfile
import types
import unittest

from . import artifacts
from .contract import Artifact, Context, StageError, bind, registry, validate

#: An artifact no stage here writes, so it answers for itself — the seam every unported stage sits on.
UNOWNED = Artifact(name="demo_input", filename="demo.json", producer="scripts/demo.py",
                   how="scripts/demo.py")
#: An artifact a stage writes, so it carries no producer of its own.
OWNED = Artifact(name="demo_output", filename="demo-{version}.bin")


def stub(name="demo", inputs=(), outputs=(OWNED,), run=lambda ctx: None,
         producer="scripts/demo.py", how="scripts/demo.py --all"):
    module = types.ModuleType(f"pipeline.{name}")
    module.NAME, module.INPUTS, module.OUTPUTS, module.run = name, inputs, outputs, run
    module.PRODUCER, module.HOW = producer, how
    return module


class Naming(unittest.TestCase):
    def test_an_input_name_becomes_the_flag_that_carries_it(self):
        """The declaration builds the command line, so the underscore/dash translation is the seam
        between the two. `vector_labels` reaching the writer as `--vector_labels` is an unrecognised
        argument, which is the failure this spelling exists to keep loud."""
        self.assertEqual(artifacts.VECTOR_LABELS.flag(), "--vector-labels")
        self.assertEqual(artifacts.CORPUS.flag(), "--corpus")

    def test_a_binding_changes_the_flag_and_not_the_artifacts_name(self):
        """One file, two readers, two words for it: `labels-t02.json` is `--vector-labels` to the store
        writer and `--labels` to the corpus join. The name is what `--set` and the producer registry key
        on, so it has to survive the rename — two names for one file is two entries waiting to drift."""
        bound = artifacts.VECTOR_LABELS.called("labels")
        self.assertEqual(bound.flag(), "--labels")
        self.assertEqual(bound.name, "vector_labels")
        self.assertEqual(bound.artifact, artifacts.VECTOR_LABELS)

    def test_a_bare_artifact_binds_to_its_own_name(self):
        self.assertEqual(bind(artifacts.CORPUS).arg, "corpus")
        self.assertEqual(bind(artifacts.CORPUS).flag(), "--corpus")

    def test_the_glob_is_the_filename_with_the_version_wild(self):
        """A publish dir holds `corpus-2026-09-04.jsonl.gz`; the ownership guard searches for it without
        knowing the version. One template answers both."""
        self.assertEqual(artifacts.CORPUS.glob(), "corpus-*.jsonl.gz")
        self.assertEqual(artifacts.ENTITIES.glob(), "corpus-*-entities.json.gz")


class Paths(unittest.TestCase):
    def test_the_version_lands_in_the_filename(self):
        ctx = Context(out_dir="out", dataset_version="v9")
        self.assertEqual(ctx.path(artifacts.STORE), os.path.join("out", "den-v9.store"))

    def test_an_override_wins(self):
        ctx = Context(out_dir="out", dataset_version="v9", overrides={"corpus": "/tmp/fixture.gz"})
        self.assertEqual(ctx.path(artifacts.CORPUS), "/tmp/fixture.gz")

    def test_a_missing_required_input_names_what_builds_it(self):
        """The useful half of "corpus is missing" is not the name, it is whose job it was. A refusal
        that does not carry the command sends the reader back to the docs the pipeline is replacing."""
        with tempfile.TemporaryDirectory() as out:
            ctx = Context(out_dir=out, dataset_version="v9")
            with self.assertRaises(StageError) as refused:
                ctx.require(artifacts.CORPUS)
            self.assertIn(artifacts.CORPUS.how, str(refused.exception))

    def test_a_missing_optional_input_is_not_a_refusal(self):
        with tempfile.TemporaryDirectory() as out:
            ctx = Context(out_dir=out, dataset_version="v9")
            self.assertIsNone(ctx.require(artifacts.PREMISE_VECTORS))


class Shards(unittest.TestCase):
    """A set of files behind one flag. The declaration names the SET, so no stage can hand over one
    shard of three — which is how eleven titles left a derived blob for a day."""

    def write(self, out, *names):
        for name in names:
            open(os.path.join(out, name), "a").close()

    def test_the_whole_set_is_resolved_from_the_declared_glob(self):
        with tempfile.TemporaryDirectory() as out:
            self.write(out, "combined-v1-r2.jsonl", "combined-v1-r2-token-fallback.jsonl",
                       "combined-v1-smoke.jsonl")
            ctx = Context(out_dir=out, dataset_version="v9")
            self.assertEqual(ctx.paths(artifacts.COMBINED),
                             tuple(sorted(os.path.join(out, n) for n in
                                          ("combined-v1-r2.jsonl",
                                           "combined-v1-r2-token-fallback.jsonl"))),
                             "the glob names one pass's shards, not every pass in the out-dir")

    def test_a_set_pointed_at_by_hand_keeps_the_order_it_was_given(self):
        ctx = Context(out_dir="out", dataset_version="v9",
                      overrides={"combined": ["b.jsonl", "a.jsonl"]})
        self.assertEqual(ctx.paths(artifacts.COMBINED), ("b.jsonl", "a.jsonl"))

    def test_asking_a_shard_set_for_one_path_is_refused(self):
        """A stage that treated the set as a file would pass one shard and write a quieter corpus, which
        is the failure mode with no symptom."""
        ctx = Context(out_dir="out", dataset_version="v9")
        with self.assertRaises(StageError):
            ctx.path(artifacts.COMBINED)

    def test_an_empty_set_names_what_writes_it(self):
        with tempfile.TemporaryDirectory() as out:
            ctx = Context(out_dir=out, dataset_version="v9")
            with self.assertRaises(StageError) as refused:
                ctx.require_all(artifacts.COMBINED)
            self.assertIn(artifacts.COMBINED.how, str(refused.exception))


class Validation(unittest.TestCase):
    def test_a_module_missing_part_of_the_contract_is_refused(self):
        for missing in ("INPUTS", "OUTPUTS", "run", "PRODUCER", "HOW"):
            module = stub()
            delattr(module, missing)
            with self.assertRaises(StageError, msg=f"a stage with no {missing} was accepted"):
                validate(module, "demo")

    def test_a_stage_that_writes_nothing_is_refused(self):
        """A stage with no outputs cannot be an input to anything, so nothing downstream can declare it
        — and a pipeline of stages that name no artifacts is a hand-maintained order again."""
        with self.assertRaises(StageError):
            validate(stub(outputs=()), "demo")

    def test_a_declaration_that_is_not_an_artifact_is_refused(self):
        """A bare path string in `INPUTS` reads fine and carries no producer, which would put a
        store input back outside the registry — the exact hole the derivation closes."""
        with self.assertRaises(StageError):
            validate(stub(inputs=("out/corpus.jsonl.gz",)), "demo")

    def test_a_module_that_answers_to_another_name_is_refused(self):
        with self.assertRaises(StageError):
            validate(stub(name="other"), "demo")


class Derivation(unittest.TestCase):
    """The producer registry, read off the ORDER rather than off a field on the artifact."""

    def test_an_artifact_is_owned_by_the_stage_that_writes_it(self):
        """The topological half. The rule is the one the stage runs, named beside the code that runs it,
        so the registry cannot answer with a script the run does not use."""
        derived = registry([stub(outputs=(OWNED,))])
        self.assertEqual(derived["demo_output"], ("scripts/demo.py", "scripts/demo.py --all", True))

    def test_an_artifact_no_stage_writes_answers_for_itself(self):
        """The seam. Until its stage is ported, the entry carries its own producer — and the registry
        has to hold both kinds at once or the port has to be all-or-nothing."""
        derived = registry([stub(inputs=(UNOWNED,))])
        self.assertEqual(derived["demo_input"], ("scripts/demo.py", "scripts/demo.py", True))

    def test_an_artifact_that_names_a_producer_and_is_also_written_here_is_refused(self):
        """Two answers to "what builds this". Leaving both would let the guard name one script while the
        stage ran another, which is the drift the derivation exists to end — so it is refused rather
        than resolved by precedence nobody would remember."""
        twin = Artifact(name="demo_output", filename="demo-{version}.bin",
                        producer="scripts/somewhere_else.py", how="scripts/somewhere_else.py")
        with self.assertRaises(StageError) as refused:
            registry([stub(outputs=(twin,))])
        self.assertIn("names its own producer", str(refused.exception))

    def test_two_stages_writing_one_artifact_are_refused(self):
        with self.assertRaises(StageError) as refused:
            registry([stub(name="a", outputs=(OWNED,)), stub(name="b", outputs=(OWNED,))])
        self.assertIn("written by two stages", str(refused.exception))

    def test_an_input_nothing_writes_and_that_names_nobody_is_refused(self):
        """An artifact with no owner does not get rebuilt when its inputs change. That is facets.bin,
        999 titles behind the corpus with every guard passing."""
        orphan = Artifact(name="orphan", filename="orphan.json")
        with self.assertRaises(StageError) as refused:
            registry([stub(inputs=(orphan,))])
        self.assertIn("no stage writes it", str(refused.exception))

    def test_a_binding_registers_under_the_artifacts_own_name(self):
        """The rename is the reader's. A registry keyed on the flag would hold `labels-t02.json` twice,
        once per stage that reads it."""
        derived = registry([stub(inputs=(UNOWNED.called("other_word"),))])
        self.assertEqual(sorted(derived), ["demo_input", "demo_output"])

    def test_the_real_pipeline_answers_for_every_artifact_it_names(self):
        import pipeline
        derived = pipeline.producers()
        self.assertEqual(set(derived), {a.name for a in pipeline.declared()})
        for name, (producer, how, _) in derived.items():
            self.assertTrue(producer and how, f"{name} is registered with nothing to run")


if __name__ == "__main__":
    unittest.main()
