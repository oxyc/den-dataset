#!/usr/bin/env python3
"""Refuse a module under `pipeline/`, `store/` or `lib/`, or a script, that nothing reaches.

`scripts/` and `scripts/v2/` are two generations of one pipeline side by side, 83 Python files of which
17 are reachable from CI. Nobody chose that. It happened because "delete what is not used" was a habit
rather than a check, and a habit loses to a branch someone might come back to.

So the rule has teeth here: **existence means reachability**. A file under a guarded package is either
imported, transitively, from that package's entry points, or it is deleted. Then "is this live?" is
answered by `ls` instead of by a search, which is the whole point of the layout.

The roots always include `__init__` — every submodule import loads it — and beyond that the three guarded
packages are entered differently, so they name their roots differently:

  pipeline/  the modules in `STAGES`, passed in. A stage that leaves the list stops being a root, and
             everything only it reached surfaces here on the next run: the removal and the cleanup are
             one commit rather than two years apart. The passes moved in from `scripts/` are entered
             the way scripts are — executed by the path a stage's `PRODUCER` spells, imported by a
             script still outside, run by hand — so `reached_from` widens the closure by those, and
             still not by prose.
  store/     whatever `scripts/v2/build_store.py` imports, read off that file by `roots()`. Nothing
             imports `store/` from inside the repo's own import graph — the store stage runs the writer
             in a subprocess — so a hand-kept root list here would be a second copy of the writer's
             import block, free to drift from it. Which script is the entry is not a copy either: it is
             `pipeline.store.PRODUCER`, the same string the stage executes, so the chain from `STAGES` to
             a `store/` module is unbroken and a writer the pipeline stops running strands its package.
  lib/       whatever the REACHABLE stages import from it. Read the same way and for the same reason,
             over several entry files rather than one: the chain runs `STAGES` → a stage → a `lib/`
             module, so a stage leaving the order strands the upstream client only it talked to.

It reads the imports rather than running them. An import guarded by a flag or a try is still an import,
and a module whose only path in is conditional is exactly the kind nobody can answer for.

A `*_test.py` is held to the mirror-image rule rather than to this one. Nothing imports a test — the
runner collects it by name — so reachability cannot be asked of it, and it is excluded from `modules()`
throughout. What IS asked of it is `orphan_tests()`: a test must still have the module it tests. That
pair is the whole treatment, and between them no file under a guarded package escapes a question.
"""
import ast
import json
import os
import re

#: Tests live beside the code they test rather than in a parallel tree, so they are matched by name.
TEST_SUFFIX = "_test.py"


def modules(package_dir):
    """Every module in the package, tests excluded."""
    return sorted(entry[:-3] for entry in os.listdir(package_dir)
                  if entry.endswith(".py") and not entry.endswith(TEST_SUFFIX))


def _imported(source, package, present, relative):
    """The members of `present` that `source` imports from `package`, by name.

    Both spellings reach the same place: `from . import store` and `from .contract import REPO`, and the
    absolute `import pipeline.store` a script outside the package would use.

    `relative` is false for source that lives OUTSIDE the package: there `from . import x` names that
    file's own siblings, and reading them as ours would mark a dead module live.
    """
    tree = ast.parse(source)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level and not relative:
                continue
            if node.level and not node.module:
                # `from . import a, b` — the names ARE the siblings.
                found.update(alias.name for alias in node.names)
            elif node.level:
                # `from .a import X`.
                found.add(node.module.split(".")[0])
            elif node.module and node.module.split(".")[0] == package:
                parts = node.module.split(".")
                if len(parts) > 1:
                    found.add(parts[1])
                else:
                    found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == package and len(parts) > 1:
                    found.add(parts[1])
    return found & present


def imports(package_dir, module):
    """The sibling modules `module` imports, by name."""
    with open(os.path.join(package_dir, module + ".py"), encoding="utf-8") as fh:
        source = fh.read()
    return _imported(source, os.path.basename(os.path.abspath(package_dir)), set(modules(package_dir)),
                     relative=True)


def roots(package_dir, *entries):
    """The package's modules the entry files outside it import — the root set, read rather than kept.

    A package nothing else in the repo imports is entered from one file, and that file's import block
    already says which modules are the way in. Asking it, instead of restating it here, is what keeps the
    root set from being a copy that drifts.

    Several entries because a package can be entered from more than one place: `store/` has one writer,
    while `lib/` is imported by whichever stages need to leave the machine. The union is the root set, and
    a stage that stops importing a `lib/` module strands it the same commit.
    """
    found = set()
    for entry in entries:
        with open(entry, encoding="utf-8") as fh:
            source = fh.read()
        found |= _imported(source, os.path.basename(os.path.abspath(package_dir)),
                           set(modules(package_dir)), relative=False)
    return sorted(found)


def reached(package_dir, roots):
    """The transitive import closure of the roots, plus `__init__`."""
    seen, queue = set(), ["__init__"] + [r for r in roots]
    while queue:
        module = queue.pop()
        if module in seen or not os.path.exists(os.path.join(package_dir, module + ".py")):
            continue
        seen.add(module)
        queue.extend(imports(package_dir, module))
    return seen


def unreachable(package_dir, roots):
    """The modules nothing reaches — each one a file to delete."""
    return sorted(set(modules(package_dir)) - reached(package_dir, roots))


def reached_from(repo, package_dir, base, entries=()):
    """The package's reach from the modules `base`, widened by what enters it from outside the import graph.

    A script moved into a package (oxyc/den-dataset#73) is entered the ways a script is: a stage executes
    it by the path its `PRODUCER` spells, a refusal tells the operator to run it, a file outside the package
    imports it. So on top of the import closure of `base`, a module is reached when a reached module names
    it in code (see `references`), and when one of `entries` — repo-relative files that run, outside the
    package — imports or names it. Iterated, because a module reached by name reaches what it imports.
    """
    by_path = {os.path.relpath(os.path.join(package_dir, m + ".py"), repo): m for m in modules(package_dir)}
    found = set(base)
    for entry in entries:
        path = os.path.join(repo, entry)
        if _is_python(path):
            found |= set(roots(package_dir, path))
        found |= {by_path[p] for p in references(path, by_path)}
    while True:
        closure = reached(package_dir, found)
        named = {by_path[p] for m in closure
                 for p in references(os.path.join(package_dir, m + ".py"), by_path)}
        if named <= closure:
            return closure
        found |= named


def _is_python(path):
    if path.endswith(".py"):
        return True
    with open(path, encoding="utf-8") as fh:
        return fh.readline().startswith("#!/usr/bin/env python")


def orphan_tests(package_dir):
    """Tests whose subject is gone.

    A test beside a module it no longer tests outlives the deletion it should have accompanied, and reads
    from the outside exactly like coverage.
    """
    present = set(modules(package_dir))
    return sorted(entry for entry in os.listdir(package_dir)
                  if entry.endswith(TEST_SUFFIX) and entry[:-len(TEST_SUFFIX)] not in present)


# --- scripts -------------------------------------------------------------------------------------------
#
# A script — any file under `scripts/` or `tools/`, and a shell script anywhere under the packages — is not
# in a package, so its reachability cannot be an import closure alone. A script is entered three ways —
# imported off a `sys.path` entry, executed by a path a stage or another script spells out, or typed by an
# operator — and the rule has to see all three without seeing prose:
#
#   * A Python file refers to a script by IMPORTING it (a tool's directory is on `sys.path` when it runs, so
#     its siblings import by bare name) or by a string in its CODE that names the file: a stage's `PRODUCER`, an
#     `os.path.join(REPO, "scripts", "merge-facts.py")`, a subprocess argv, an error message telling the
#     operator what to run. Docstrings and comments are not code and are skipped — a file that only prose
#     names is exactly the dead file this exists to find.
#   * A shell script refers to a script by naming it outside a comment.
#   * A workflow refers to one by running it. A `py_compile` line compiles a file; it does not run it, and
#     does not count.
#
# The roots are what actually runs: `den`, every module under `pipeline/` a stage reaches, `store/` and
# `lib/` (which the guards above hold to the same rule), the workflows, and `OPERATOR_TOOLS` — the scripts a person
# runs by hand, each with its reason. From those, reachability is transitive: a script a live script names
# is live.
#
# A test is never a root. Nothing runs a test except the runner, and a test that is the only thing reaching
# a module is proof that the module works, not that anything uses it. A test is asked the mirror question
# instead: it must not reach a dead script, because a test is what keeps a dead one looking covered.

#: The scripts an operator runs by hand, each with its reason. A committed list rather than "a doc names
#: it": prose outlives what it describes, and this list is held to the files — an entry for a file that is
#: gone, or that something that runs already reaches, is refused.
OPERATOR_TOOLS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "operator-tools.json")

_SCRIPT_EXTENSIONS = (".py", ".sh")

#: The trees entered by path rather than by import, where every file is a script: `tools/` holds standalone
#: tools with dependencies of their own, which is why they are not in a package.
SCRIPT_TREES = ("scripts", "tools")
#: The packages, where only a shell script is a script — a module there is held to its package's rule above.
SCRIPT_PACKAGES = ("pipeline", "store", "lib")


def _is_test(name):
    return name.startswith("test_") or name.endswith(TEST_SUFFIX) or name.endswith(".test.sh")


def script_files(repo):
    """Every script in the repo, repo-relative, split into (code, tests)."""
    code, tests = [], []
    for tree in SCRIPT_TREES + SCRIPT_PACKAGES:
        extensions = _SCRIPT_EXTENSIONS if tree in SCRIPT_TREES else (".sh",)
        for dirpath, dirnames, filenames in os.walk(os.path.join(repo, tree)):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                if name.endswith(extensions):
                    path = os.path.relpath(os.path.join(dirpath, name), repo)
                    (tests if _is_test(name) else code).append(path)
    return sorted(code), sorted(tests)


def _docstrings(tree):
    """The ids of the string nodes that are docstrings, which are prose and name nothing."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                found.add(id(body[0].value))
    return found


def _mentions(text, basename):
    return re.search(rf"(?<![\w.-]){re.escape(basename)}(?![\w-])", text) is not None


def references(path, scripts):
    """The members of `scripts` (repo-relative paths) the file at `path` refers to in code."""
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    by_name = {}
    for script in scripts:
        by_name.setdefault(os.path.basename(script), []).append(script)
    found = set()
    if path.endswith(".py") or source.startswith("#!/usr/bin/env python"):
        tree = ast.parse(source)
        skip = _docstrings(tree)
        strings = [node.value for node in ast.walk(tree)
                   if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip]
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                imported.add(node.module.split(".")[0])
        for basename, paths in by_name.items():
            stem, ext = os.path.splitext(basename)
            if (ext == ".py" and stem in imported) or any(_mentions(s, basename) for s in strings):
                found.update(paths)
    else:
        lines = source.splitlines()
        if path.endswith((".yml", ".yaml")):
            # A compile is not a run.
            lines = [line for line in lines if "py_compile" not in line]
        code = "\n".join(re.sub(r"(^|\s)#.*", "", line) for line in lines)
        for basename, paths in by_name.items():
            if _mentions(code, basename):
                found.update(paths)
    return found


def operator_tools():
    """`{repo-relative path: reason}` — the scripts run by hand."""
    with open(OPERATOR_TOOLS, encoding="utf-8") as fh:
        return json.load(fh)


def reached_scripts(repo, roots, scripts):
    """The scripts reachable from `roots` (files, absolute or repo-relative), transitively."""
    seen, queue = set(), list(roots)
    while queue:
        source = queue.pop()
        for script in references(os.path.join(repo, source), scripts):
            if script not in seen:
                seen.add(script)
                queue.append(script)
    return seen


def unreachable_scripts(repo, roots, extra=()):
    """The scripts nothing that runs reaches, and the tests that reach nothing live.

    `extra` are scripts reachable by declaration — the operator tools — and are roots themselves.
    """
    code, tests = script_files(repo)
    live = reached_scripts(repo, list(roots) + list(extra), code) | set(extra)
    dead = set(code) - live
    orphans = sorted(test for test in tests if references(os.path.join(repo, test), code) & dead)
    return sorted(dead), orphans
