#!/usr/bin/env python3
"""The reachability guard, and a demonstration that it refuses something.

A guard nobody has seen fail is not known to work. So the tests below build a package with a module
nothing imports and require it to be named — the same shape `scripts/v2/` has today — before asserting
the real `pipeline/` is clean.
"""
import os
import tempfile
import unittest

import pipeline

from . import reachable

PIPELINE = os.path.dirname(os.path.abspath(pipeline.__file__))


def package(dir, **modules):
    """A throwaway package. `modules` maps module name to its source."""
    for name, source in modules.items():
        with open(os.path.join(dir, name + ".py"), "w", encoding="utf-8") as fh:
            fh.write(source)
    return dir


class Refusal(unittest.TestCase):
    def test_a_module_nothing_imports_is_named(self):
        with tempfile.TemporaryDirectory() as dir:
            package(dir, __init__="", store="from . import helper\n", helper="", stranded="")
            self.assertEqual(reachable.unreachable(dir, ["store"]), ["stranded"])

    def test_a_module_reached_only_through_another_stage_survives_its_own_removal(self):
        """Transitive, not one hop: `store` imports `contract` imports nothing, and a helper two steps
        down is as live as the stage that reaches it."""
        with tempfile.TemporaryDirectory() as dir:
            package(dir, __init__="", store="from .contract import Artifact\n",
                    contract="from . import io_\n", io_="")
            self.assertEqual(reachable.unreachable(dir, ["store"]), [])

    def test_dropping_a_stage_from_the_order_strands_what_only_it_reached(self):
        """The rule's teeth. A stage leaving `STAGES` is exactly when its private helpers become dead,
        and exactly when nobody thinks to look for them."""
        with tempfile.TemporaryDirectory() as dir:
            package(dir, __init__="", store="from . import shared\n", classify="from . import mine\n",
                    shared="", mine="")
            self.assertEqual(reachable.unreachable(dir, ["store", "classify"]), [])
            # The stage module itself and the helper only it reached, together — the deletion the
            # removal implies, listed rather than remembered.
            self.assertEqual(reachable.unreachable(dir, ["store"]), ["classify", "mine"])

    def test_every_spelling_of_an_import_counts(self):
        """`from . import x`, `from .x import y` and `import pkg.x` all reach `x`. A guard that knows
        one spelling deletes a live module, which is how a guard gets turned off."""
        for source in ("from . import helper\n", "from .helper import thing\n",
                       "import {package}.helper\n", "from {package}.helper import thing\n",
                       "from {package} import helper\n"):
            with tempfile.TemporaryDirectory() as parent:
                dir = os.path.join(parent, "pkg")
                os.mkdir(dir)
                package(dir, __init__="", store=source.format(package="pkg"), helper="")
                self.assertEqual(reachable.unreachable(dir, ["store"]), [],
                                 f"{source.strip()!r} was not read as an import")

    def test_a_test_beside_a_module_that_is_gone_is_named(self):
        with tempfile.TemporaryDirectory() as dir:
            package(dir, __init__="", store="", store_test="", gone_test="")
            self.assertEqual(reachable.orphan_tests(dir), ["gone_test.py"])


class ThisRepo(unittest.TestCase):
    def test_nothing_under_pipeline_is_unreachable(self):
        stranded = reachable.unreachable(PIPELINE, pipeline.STAGES)
        self.assertEqual(stranded, [], f"unreachable from STAGES: {stranded}. Nothing lives under "
                                       f"pipeline/ unless a stage reaches it — delete it, or import it "
                                       f"from the stage that needs it. Leaving it is how scripts/v2/ "
                                       f"came to exist.")

    def test_no_test_outlives_its_subject(self):
        self.assertEqual(reachable.orphan_tests(PIPELINE), [])

    def test_every_stage_in_the_order_is_a_stage(self):
        """`STAGES` is what a reader is promised describes the pipeline. A name in it that loads
        nothing, or loads a module with no contract, makes the promise false."""
        for name in pipeline.STAGES:
            self.assertEqual(pipeline.stage(name).NAME, name)


if __name__ == "__main__":
    unittest.main()
