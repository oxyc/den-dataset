#!/usr/bin/env python3
"""The reachability guard, and a demonstration that it refuses something.

A guard nobody has seen fail is not known to work. So the tests below build a package with a module
nothing imports and require it to be named — the same shape `scripts/v2/` has today — before asserting
the real `pipeline/` and `store/` are clean.

The two guarded packages are entered differently and so are asserted separately: `pipeline/` from the
modules in `STAGES`, `store/` from the import block of the script the store stage runs. Two assertions
rather than one loop, because what a reader needs from a failure is the remedy for THAT tree, and the
two remedies are different sentences.
"""
import os
import tempfile
import unittest

import pipeline
import store
from pipeline.contract import REPO

from . import reachable

PIPELINE = os.path.dirname(os.path.abspath(pipeline.__file__))
STORE = os.path.dirname(os.path.abspath(store.__file__))
#: The one spelling of the store's entry point: the file the store stage actually executes.
STORE_PRODUCER = pipeline.stage("store").PRODUCER
STORE_ENTRY = os.path.join(REPO, STORE_PRODUCER)


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

    def test_an_entry_script_outside_the_package_is_the_root_set(self):
        """How `store/` is entered: nothing imports it, one script does, and that script's import block
        is the list of ways in. Read off the script, a module it stops importing is stranded the same
        commit rather than kept alive by a root list nobody revisited."""
        with tempfile.TemporaryDirectory() as parent:
            dir = os.path.join(parent, "pkg")
            os.mkdir(dir)
            package(dir, __init__="", build="from . import helper\n", helper="", cards="", stranded="")
            entry = os.path.join(parent, "build_pkg.py")
            with open(entry, "w", encoding="utf-8") as fh:
                fh.write("from pkg import build\nfrom pkg.cards import display_title\n")
            self.assertEqual(reachable.roots(dir, entry), ["build", "cards"])
            self.assertEqual(reachable.unreachable(dir, reachable.roots(dir, entry)), ["stranded"])

    def test_an_entry_script_that_stops_importing_a_module_strands_it(self):
        """The teeth again, on this side. The writer dropping a section group is exactly when that
        group's module becomes dead, and exactly when nobody thinks to look for it."""
        with tempfile.TemporaryDirectory() as parent:
            dir = os.path.join(parent, "pkg")
            os.mkdir(dir)
            package(dir, __init__="", build="", vectors="from . import blob\n", blob="")
            entry = os.path.join(parent, "build_pkg.py")
            with open(entry, "w", encoding="utf-8") as fh:
                fh.write("from pkg import build\n")
            self.assertEqual(reachable.unreachable(dir, reachable.roots(dir, entry)),
                             ["blob", "vectors"])

    def test_a_relative_import_in_the_entry_script_is_not_read_as_ours(self):
        """An entry script sits in a package of its own — `scripts/v2/` — so its `from . import x` names
        ITS siblings. Counting those as roots would mark a dead module live on a name collision, which is
        the one failure mode a reachability guard cannot have."""
        with tempfile.TemporaryDirectory() as parent:
            dir = os.path.join(parent, "pkg")
            os.mkdir(dir)
            package(dir, __init__="", build="", vector_blob="")
            entry = os.path.join(parent, "build_pkg.py")
            with open(entry, "w", encoding="utf-8") as fh:
                fh.write("from . import vector_blob\nfrom pkg import build\n")
            self.assertEqual(reachable.roots(dir, entry), ["build"])


class ThisRepo(unittest.TestCase):
    def test_nothing_under_pipeline_is_unreachable(self):
        stranded = reachable.unreachable(PIPELINE, pipeline.STAGES)
        self.assertEqual(stranded, [], f"unreachable from STAGES: {stranded}. Nothing lives under "
                                       f"pipeline/ unless a stage reaches it — delete it, or import it "
                                       f"from the stage that needs it. Leaving it is how scripts/v2/ "
                                       f"came to exist.")

    def test_nothing_under_store_is_unreachable(self):
        """`store/` is entered by subprocess, not by import, so its roots are the entry script's own
        import block — read off the file `pipeline.store` executes rather than listed here."""
        stranded = reachable.unreachable(STORE, reachable.roots(STORE, STORE_ENTRY))
        self.assertEqual(stranded, [], f"unreachable from {STORE_PRODUCER}: "
                                       f"{[f'store/{name}.py' for name in stranded]}. Every file under "
                                       f"store/ is a section group the writer runs — delete it, or "
                                       f"import it from the group that needs it. Nothing in the repo "
                                       f"imports store/, so this is the only thing that would notice.")

    def test_no_test_outlives_its_subject(self):
        self.assertEqual(reachable.orphan_tests(PIPELINE), [])
        self.assertEqual(reachable.orphan_tests(STORE), [])

    def test_every_stage_in_the_order_is_a_stage(self):
        """`STAGES` is what a reader is promised describes the pipeline. A name in it that loads
        nothing, or loads a module with no contract, makes the promise false."""
        for name in pipeline.STAGES:
            self.assertEqual(pipeline.stage(name).NAME, name)


if __name__ == "__main__":
    unittest.main()
