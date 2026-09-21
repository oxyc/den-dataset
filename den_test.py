#!/usr/bin/env python3
"""`den` — that the entry point dispatches, and that it stays the only thing you have to know.

The acceptance this covers is small and specific: the pipeline can be LISTED and INVOKED. Before it,
`docs/OPERATE.md` was the order and the commands, which is a pipeline you remember rather than one you
run.
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DEN = os.path.join(HERE, "den")
V2 = os.path.join(HERE, "scripts", "v2")

sys.path.insert(0, V2)
_spec = importlib.util.spec_from_file_location("test_build_store", os.path.join(V2, "test_build_store.py"))
fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture)

from pipeline.store_test import FIXTURE_FILES  # noqa: E402  — one spelling of the fixture's filenames


def den(*args):
    return subprocess.run([sys.executable, DEN, *args], capture_output=True, text=True, cwd=HERE)


class Listing(unittest.TestCase):
    def test_stages_answers_what_runs_in_what_order_and_what_it_touches(self):
        result = den("stages")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1. store", result.stdout)
        for line in ("corpus", "premise_labels", "scripts/v2/build_store.py"):
            self.assertIn(line, result.stdout)
        # The optional input is marked as such: "the writer needs this" and "the writer can do without
        # it" are different answers to the same question.
        self.assertIn("(optional)", result.stdout)


class Dispatch(unittest.TestCase):
    def test_a_stage_that_is_not_in_the_order_is_refused_with_the_order(self):
        result = den("stage", "classify", "--dataset-version", "test")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no stage named 'classify'", result.stderr)
        self.assertIn("store", result.stderr)

    def test_a_missing_input_is_refused_with_the_command_that_builds_it(self):
        with tempfile.TemporaryDirectory() as out:
            result = den("stage", "store", "--out-dir", out, "--dataset-version", "test")
            self.assertEqual(result.returncode, 1)
            self.assertIn("consolidate_corpus.py", result.stderr)

    def test_set_wants_a_pair(self):
        result = den("stage", "store", "--dataset-version", "test", "--set", "corpus")
        self.assertEqual(result.returncode, 1)
        self.assertIn("NAME=PATH", result.stderr)


class EndToEnd(fixture.StoreFixture, unittest.TestCase):
    def test_den_stage_store_builds_a_store(self):
        """One command, from outside the process, over real inputs."""
        with tempfile.TemporaryDirectory() as out:
            self.build(out)
            overrides = []
            for name, filename in FIXTURE_FILES.items():
                overrides += ["--set", f"{name}={os.path.join(out, filename)}"]
            result = den("stage", "store", "--out-dir", out, "--dataset-version", "den", *overrides)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(os.path.isfile(os.path.join(out, "den-den.store")),
                            f"no store in {os.listdir(out)}")
            self.assertIn("==> store", result.stderr)


if __name__ == "__main__":
    unittest.main()
