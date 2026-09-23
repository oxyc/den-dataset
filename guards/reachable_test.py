#!/usr/bin/env python3
"""The reachability guard, and a demonstration that it refuses something.

A guard nobody has seen fail is not known to work. So the tests below build a package with a module
nothing imports and require it to be named — the same shape `scripts/v2/` has today — before asserting
the real `pipeline/` and `store/` are clean.

The three guarded packages are entered differently and so are asserted separately: `pipeline/` from the
modules in `STAGES`, `store/` from the import block of the script the store stage runs, `lib/` from the
import blocks of the stages that reach outside the machine. Three assertions rather than one loop,
because what a reader needs from a failure is the remedy for THAT tree, and the remedies are different
sentences.

`scripts/` is not a package, so it has a rule of its own — imports, paths named in code, shell scripts, CI
and a declared list of operator tools — asserted in `ScriptRefusal` and at the end of `ThisRepo`.
"""
import os
import tempfile
import unittest

import lib
import pipeline
import store
from pipeline.contract import REPO

from . import reachable

PIPELINE = os.path.dirname(os.path.abspath(pipeline.__file__))
STORE = os.path.dirname(os.path.abspath(store.__file__))
LIB = os.path.dirname(os.path.abspath(lib.__file__))
#: The one spelling of the store's entry point: the file the store stage actually executes.
STORE_PRODUCER = pipeline.stage("store").PRODUCER
STORE_ENTRY = os.path.join(REPO, STORE_PRODUCER)


CI = ".github/workflows/ci.yml"


def operator_tools_in(package_dir):
    """The operator tools that live in a package, as its module names."""
    prefix = os.path.relpath(package_dir, REPO) + "/"
    return sorted(path[len(prefix):-len(".py")] for path in reachable.operator_tools()
                  if path.startswith(prefix) and path.endswith(".py"))


def scripts_entering(tools):
    """The scripts under `scripts/` (held live by their own guard below), without the operator tools among
    them when `tools` is false."""
    scripts, _ = reachable.script_files(os.path.join(REPO, "scripts"))
    declared = reachable.operator_tools()
    return [script for script in scripts if tools or script not in declared]


def pipeline_reach(tools=True):
    """What runs reaches under `pipeline/`: the stages and what they import, and what `den`, CI, the scripts
    under `scripts/` and the operator tools import or name — a stage's `PRODUCER` included, which is how a
    pass the stage executes is entered. `tools=False` leaves every operator tool out, which is what the
    operator-tool list is checked against."""
    base = list(pipeline.STAGES) + (operator_tools_in(PIPELINE) if tools else [])
    return reachable.reached_from(REPO, PIPELINE, base, ["den", CI] + scripts_entering(tools))


def lib_reach(tools=True):
    """What runs reaches under `lib/`: what the reached pipeline modules import, and what `den`, the scripts
    under `scripts/` and the operator tools import or name."""
    stages = [f"pipeline/{name}.py" for name in sorted(pipeline_reach(tools))]
    base = operator_tools_in(LIB) if tools else []
    return reachable.reached_from(REPO, LIB, base, ["den"] + stages + scripts_entering(tools))


def script_roots():
    """Everything that runs and can name a script: `den`, CI, the stages and what they reach. The operator
    tools are not among them — they reach what they name as `extra` roots, which is what lets the list
    be held to naming only what nothing else runs."""
    return (["den", CI]
            + [f"pipeline/{name}.py" for name in sorted(pipeline_reach(tools=False))]
            + [f"store/{name}.py" for name in reachable.modules(STORE)]
            + [f"lib/{name}.py" for name in reachable.modules(LIB)])


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


class NamedRefusal(unittest.TestCase):
    def test_a_module_run_by_path_or_imported_from_outside_is_reached_and_prose_still_is_not(self):
        """A pass moved out of `scripts/` into `pipeline/` is executed by the path a stage's `PRODUCER`
        spells, and imported by a script still outside — both reach it, and what it imports is reached in
        turn. A docstring naming a module still reaches nothing."""
        with tempfile.TemporaryDirectory() as root:
            repo(root, {
                "pkg/__init__.py": "",
                "pkg/stage.py": 'PRODUCER = "pkg/pass_.py"\n',
                "pkg/pass_.py": "from pkg import helper\n",
                "pkg/helper.py": "",
                "pkg/imported.py": "",
                "pkg/told.py": "",
                "pkg/stranded.py": "",
                "tool.py": "'''Run pkg/told.py by hand.'''\nfrom pkg.imported import x\n",
            })
            reach = reachable.reached_from(root, os.path.join(root, "pkg"), ["stage"], ["tool.py"])
            self.assertEqual(sorted(set(reachable.modules(os.path.join(root, "pkg"))) - reach),
                             ["stranded", "told"])


def repo(root, files):
    """A throwaway repo. `files` maps a repo-relative path to its source."""
    for path, source in files.items():
        full = os.path.join(root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(source)
    return root


class ScriptRefusal(unittest.TestCase):
    """`scripts/` is entered by import, by a path in code, by a shell script and by CI — and never by
    prose. Each case is one of those, on a throwaway tree."""

    def test_a_script_only_prose_names_is_unreachable(self):
        """A docstring or a comment naming a file is how dead scripts survived: the prose outlived them."""
        with tempfile.TemporaryDirectory() as root:
            repo(root, {"stage.py": "'''Run scripts/told.py by hand.'''\n# or scripts/v2/commented.py\n",
                        "run.sh": "# scripts/shelled.py\necho done\n",
                        "scripts/told.py": "", "scripts/v2/commented.py": "", "scripts/shelled.py": ""})
            dead, _ = reachable.unreachable_scripts(root, ["stage.py", "run.sh"])
            self.assertEqual(dead, ["scripts/shelled.py", "scripts/told.py", "scripts/v2/commented.py"])

    def test_every_way_in_counts_and_reaches_onward(self):
        """A producer string, an import off `sys.path`, a shell script naming another, and a CI run step
        all reach a script — and what a live script reaches is live too. A compile step is not a run."""
        with tempfile.TemporaryDirectory() as root:
            repo(root, {
                "stage.py": 'PRODUCER = "scripts/v2/producer.py"\n',
                "scripts/v2/producer.py": "import helper\n",
                "scripts/v2/helper.py": 'CMD = ["bash", "scripts/loop.sh"]\n',
                "scripts/loop.sh": ". scripts/lib/env.sh\n",
                "scripts/lib/env.sh": "",
                "ci.yml": "run: python3 -m py_compile scripts/compiled.py\nrun: python3 scripts/ran.py\n",
                "scripts/compiled.py": "",
                "scripts/ran.py": "",
            })
            dead, _ = reachable.unreachable_scripts(root, ["stage.py", "ci.yml"])
            self.assertEqual(dead, ["scripts/compiled.py"])

    def test_a_test_keeps_nothing_alive_and_is_named_with_its_subject(self):
        """The loophole a test runner opens: CI runs the test, the test imports the module, so the module
        looks reached. A test proves a module works, not that anything uses it."""
        with tempfile.TemporaryDirectory() as root:
            repo(root, {"scripts/lonely.py": "", "scripts/test_lonely.py": "import lonely\n",
                        "ci.yml": "run: python3 -m unittest scripts/test_lonely.py\n"})
            self.assertEqual(reachable.unreachable_scripts(root, ["ci.yml"]),
                             (["scripts/lonely.py"], ["scripts/test_lonely.py"]))

    def test_an_operator_tool_is_live_and_so_is_what_it_imports(self):
        with tempfile.TemporaryDirectory() as root:
            repo(root, {"scripts/tool.py": "import shared\n", "scripts/shared.py": "", "scripts/stray.py": ""})
            self.assertEqual(reachable.unreachable_scripts(root, [], extra=["scripts/tool.py"]),
                             (["scripts/stray.py"], []))


class ThisRepo(unittest.TestCase):
    def test_nothing_under_pipeline_is_unreachable(self):
        stranded = sorted(set(reachable.modules(PIPELINE)) - pipeline_reach())
        self.assertEqual(stranded, [], f"unreachable from STAGES: {stranded}. Nothing lives under "
                                       f"pipeline/ unless a stage reaches it, something that runs imports "
                                       f"or names it, or guards/operator-tools.json declares it — delete "
                                       f"it, or import it from the stage that needs it. Leaving it is how "
                                       f"scripts/v2/ came to exist.")

    def test_nothing_under_store_is_unreachable(self):
        """`store/` is entered by subprocess, not by import, so its roots are the entry script's own
        import block — read off the file `pipeline.store` executes rather than listed here."""
        stranded = reachable.unreachable(STORE, reachable.roots(STORE, STORE_ENTRY))
        self.assertEqual(stranded, [], f"unreachable from {STORE_PRODUCER}: "
                                       f"{[f'store/{name}.py' for name in stranded]}. Every file under "
                                       f"store/ is a section group the writer runs — delete it, or "
                                       f"import it from the group that needs it. Nothing in the repo "
                                       f"imports store/, so this is the only thing that would notice.")

    def test_nothing_under_lib_is_unreachable(self):
        """`lib/` is entered by import, from whichever stages leave the machine — so its roots are those
        stages' own import blocks, over the stages `STAGES` actually reaches, and those of the scripts still
        under `scripts/`. A stage that drops an upstream, or leaves the order entirely, strands the client
        only it talked to in the same commit."""
        stranded = sorted(set(reachable.modules(LIB)) - lib_reach())
        self.assertEqual(stranded, [], f"unreachable from the pipeline: "
                                       f"{[f'lib/{name}.py' for name in stranded]}. `lib/` holds what a "
                                       f"stage needs from outside the machine — delete it, or import it "
                                       f"from the stage that needs it.")

    def test_nothing_under_scripts_is_unreachable(self):
        """What runs is `den`, CI, the stages `STAGES` reaches and the `store/` and `lib/` code above; what an
        operator runs by hand is `guards/operator-tools.json`. A script none of those reach is deleted."""
        dead, orphans = reachable.unreachable_scripts(REPO, script_roots(), extra=reachable.operator_tools())
        self.assertEqual(dead, [], f"unreachable under scripts/: {dead}. Nothing that runs reaches these — "
                                   f"not a stage, `den`, CI, or a script one of those runs. Delete them, "
                                   f"after recording any result they produced in the issue it answered; "
                                   f"or, if a person runs one by hand, add it to "
                                   f"guards/operator-tools.json with the reason.")
        self.assertEqual(orphans, [], f"tests of dead scripts: {orphans}. They go with their subject.")

    def test_every_operator_tool_is_a_script_nothing_else_runs(self):
        """The list is a declaration, so it is held to the tree: a stale entry would keep nothing alive and
        read as if it did, and a redundant one would survive the day the thing that runs it stops."""
        code, _ = reachable.script_files(os.path.join(REPO, "scripts"))
        run = reachable.reached_scripts(REPO, script_roots(), code)
        run |= {f"pipeline/{name}.py" for name in pipeline_reach(tools=False)}
        run |= {f"lib/{name}.py" for name in lib_reach(tools=False)}
        for path, reason in reachable.operator_tools().items():
            self.assertTrue(path in code or (path.startswith(("pipeline/", "lib/"))
                                             and os.path.isfile(os.path.join(REPO, path))),
                            f"{path} is in guards/operator-tools.json and is not a script")
            self.assertTrue(reason.strip(), f"{path} is in guards/operator-tools.json with no reason")
            self.assertNotIn(path, run, f"{path} is already reached by what runs; drop it from the list")

    def test_no_test_outlives_its_subject(self):
        self.assertEqual(reachable.orphan_tests(PIPELINE), [])
        self.assertEqual(reachable.orphan_tests(STORE), [])
        self.assertEqual(reachable.orphan_tests(LIB), [])

    def test_every_stage_in_the_order_is_a_stage(self):
        """`STAGES` is what a reader is promised describes the pipeline. A name in it that loads
        nothing, or loads a module with no contract, makes the promise false."""
        for name in pipeline.STAGES:
            self.assertEqual(pipeline.stage(name).NAME, name)


if __name__ == "__main__":
    unittest.main()
