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
from .contract import Artifact, Context, StageError, registry, validate


def stub(name="demo", inputs=(), outputs=(artifacts.STORE,), run=lambda ctx: None):
    module = types.ModuleType(f"pipeline.{name}")
    module.NAME, module.INPUTS, module.OUTPUTS, module.run = name, inputs, outputs, run
    return module


class Naming(unittest.TestCase):
    def test_an_input_name_becomes_the_flag_that_carries_it(self):
        """The declaration builds the command line, so the underscore/dash translation is the seam
        between the two. `vector_labels` reaching the writer as `--vector_labels` is an unrecognised
        argument, which is the failure this spelling exists to keep loud."""
        self.assertEqual(artifacts.VECTOR_LABELS.flag(), "--vector-labels")
        self.assertEqual(artifacts.CORPUS.flag(), "--corpus")

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


class Validation(unittest.TestCase):
    def test_a_module_missing_part_of_the_contract_is_refused(self):
        for missing in ("INPUTS", "OUTPUTS", "run"):
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
    def test_the_registry_carries_every_declared_artifact(self):
        derived = registry(artifacts.CATALOGUE)
        self.assertEqual(set(derived), {a.name for a in artifacts.CATALOGUE})
        self.assertEqual(derived["corpus"],
                         (artifacts.CORPUS.producer, artifacts.CORPUS.how, artifacts.CORPUS.dedicated))

    def test_one_name_with_two_producers_is_refused_rather_than_resolved(self):
        """Two stages declaring the same artifact differently is how a registry starts disagreeing with
        itself. Picking one silently would make the derived registry as trustworthy as the hand-kept one
        it replaces."""
        twin = Artifact(name="corpus", filename="corpus-{version}.jsonl.gz",
                        producer="scripts/v2/something_else.py", how="scripts/v2/something_else.py")
        with self.assertRaises(StageError):
            registry((artifacts.CORPUS, twin))

    def test_the_same_artifact_declared_twice_is_fine(self):
        """Two stages reading one file is ordinary. Only a DISAGREEMENT is a problem."""
        self.assertEqual(set(registry((artifacts.CORPUS, artifacts.CORPUS))), {"corpus"})


if __name__ == "__main__":
    unittest.main()
