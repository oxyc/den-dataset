#!/usr/bin/env python3
"""The store stage — that it is a wrapper and not a second writer.

Two things are worth testing about a stage whose whole job is to hand `scripts/v2/build_store.py` the
right files:

  * that the declaration and the writer's argument list still name the same inputs. This is the pin the
    derived producer registry hangs from — `check-producers.py` reads `INPUTS`, so if `INPUTS` may drift
    from the writer then the drift has only moved;
  * that the stage's bytes are the hand-typed command's bytes. `docs/OPERATE.md` is what people run
    today, and a wrapper that is nearly the same command is worse than no wrapper.

The fixture is `scripts/v2/test_build_store.py`'s, reused rather than rebuilt: the inputs the writer
accepts are precise — real DENVEC02 blobs, an entity carrying an alias, a genre map — and a second
fixture beside it would be a second definition of what a valid corpus is.
"""
import hashlib
import importlib.util
import os
import sys
import tempfile
import unittest

from . import artifacts, store
from .contract import Context, StageError

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
V2 = os.path.join(REPO, "scripts", "v2")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sys.path.insert(0, V2)
writer = load("build_store", os.path.join(V2, "build_store.py"))
fixture = load("test_build_store", os.path.join(V2, "test_build_store.py"))

#: What `StoreFixture.build` writes, by artifact. Named here rather than guessed, so a rename in that
#: fixture fails as a missing file with a path in the message.
FIXTURE_FILES = {
    "corpus": "corpus.jsonl.gz",
    "entities": "entities.json",
    "facts": "facts.json",
    "vectors": "plot.bin",
    "vector_labels": "plot-labels.json",
    "premise_vectors": "premise.bin",
    "premise_labels": "premise-labels.json",
}


def sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class Declaration(unittest.TestCase):
    def test_the_stage_declares_exactly_what_the_writer_reads(self):
        """`build_store.INPUT_ARGS` is anchored to its own parser by `test_build_store.py`, and this
        anchors the declaration to it. Between them, an input can only be added or dropped in one place
        without something going red — which is what the hand-kept `STORE_INPUTS` could not say.
        """
        self.assertEqual([a.name for a in store.INPUTS], list(writer.INPUT_ARGS))

    def test_the_output_is_the_store(self):
        self.assertEqual([a.name for a in store.OUTPUTS], ["store"])
        self.assertEqual(artifacts.STORE.manifest_key, "storeFile")


class CommandLine(unittest.TestCase):
    def paths(self, out, skip=()):
        for name, filename in FIXTURE_FILES.items():
            if name not in skip:
                path = os.path.join(out, filename)
                open(path, "a").close()
        return Context(out_dir=out, dataset_version="test",
                       overrides={n: os.path.join(out, f) for n, f in FIXTURE_FILES.items()})

    def test_the_writers_own_parser_accepts_what_the_declaration_builds(self):
        """The declaration is not checked against a copy of the argument list — it is run through the
        parser that will receive it. A flag the writer does not know is a failure here rather than at
        the end of a seven-hour pass."""
        with tempfile.TemporaryDirectory() as out:
            command = store.argv(self.paths(out))
            parsed = writer.build_parser().parse_args(command[2:])
            for artifact in store.INPUTS:
                self.assertEqual(getattr(parsed, artifact.name),
                                 os.path.join(out, FIXTURE_FILES[artifact.name]))
            self.assertEqual(parsed.out, os.path.join(out, "den-test.store"))

    def test_an_absent_optional_input_leaves_its_flag_off(self):
        """The writer makes `--premise-vectors` optional; passing it as an empty path would be a
        different thing entirely."""
        with tempfile.TemporaryDirectory() as out:
            command = store.argv(self.paths(out, skip=("premise_vectors",)))
            self.assertNotIn("--premise-vectors", command)
            self.assertIsNone(writer.build_parser().parse_args(command[2:]).premise_vectors)

    def test_an_absent_required_input_stops_the_stage(self):
        with tempfile.TemporaryDirectory() as out:
            with self.assertRaises(StageError) as refused:
                store.argv(self.paths(out, skip=("corpus",)))
            self.assertIn("pipeline/consolidate_corpus.py", str(refused.exception))

    def test_the_manifest_is_only_stamped_when_one_is_named(self):
        with tempfile.TemporaryDirectory() as out:
            self.assertNotIn("--stamp-meta", store.argv(self.paths(out)))
            ctx = self.paths(out)
            stamped = Context(out_dir=ctx.out_dir, dataset_version=ctx.dataset_version,
                              overrides=ctx.overrides, stamp_meta=os.path.join(out, "meta.json"))
            self.assertIn("--stamp-meta", store.argv(stamped))


class Equivalence(fixture.StoreFixture, unittest.TestCase):
    def test_the_stage_writes_the_bytes_the_hand_typed_command_writes(self):
        """`den stage store` against `python3 scripts/v2/build_store.py …`, on one set of inputs.

        Byte-identical is the right bar and not an overstrict one: the writer promises deterministic
        output, and that promise is what makes the store's content hash — the thing the publisher and
        den-atlas both trust — mean anything. A wrapper that changed one byte would be a second writer.
        """
        with tempfile.TemporaryDirectory() as out:
            self.build(out)
            reference = os.path.join(out, "test.store")
            self.assertTrue(os.path.isfile(reference))
            for name, filename in FIXTURE_FILES.items():
                path = os.path.join(out, filename)
                self.assertTrue(os.path.isfile(path),
                                f"the shared fixture no longer writes {filename} for {name}")

            ctx = Context(out_dir=out, dataset_version="test",
                          overrides=dict({n: os.path.join(out, f) for n, f in FIXTURE_FILES.items()},
                                         store=os.path.join(out, "stage.store")))
            self.assertEqual(sha256(store.run(ctx)), sha256(reference))


if __name__ == "__main__":
    unittest.main()
