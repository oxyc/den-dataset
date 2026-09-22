#!/usr/bin/env python3
"""Refuse a module under `pipeline/` or `store/` that nothing reaches.

`scripts/` and `scripts/v2/` are two generations of one pipeline side by side, 83 Python files of which
17 are reachable from CI. Nobody chose that. It happened because "delete what is not used" was a habit
rather than a check, and a habit loses to a branch someone might come back to.

So the rule has teeth here: **existence means reachability**. A file under a guarded package is either
imported, transitively, from that package's entry points, or it is deleted. Then "is this live?" is
answered by `ls` instead of by a search, which is the whole point of the layout.

The roots always include `__init__` — every submodule import loads it — and beyond that the two guarded
packages are entered differently, so they name their roots differently:

  pipeline/  the modules in `STAGES`, passed in. A stage that leaves the list stops being a root, and
             everything only it reached surfaces here on the next run: the removal and the cleanup are
             one commit rather than two years apart.
  store/     whatever `scripts/v2/build_store.py` imports, read off that file by `roots()`. Nothing
             imports `store/` from inside the repo's own import graph — the store stage runs the writer
             in a subprocess — so a hand-kept root list here would be a second copy of the writer's
             import block, free to drift from it. Which script is the entry is not a copy either: it is
             `pipeline.store.PRODUCER`, the same string the stage executes, so the chain from `STAGES` to
             a `store/` module is unbroken and a writer the pipeline stops running strands its package.

It reads the imports rather than running them. An import guarded by a flag or a try is still an import,
and a module whose only path in is conditional is exactly the kind nobody can answer for.

A `*_test.py` is held to the mirror-image rule rather than to this one. Nothing imports a test — the
runner collects it by name — so reachability cannot be asked of it, and it is excluded from `modules()`
throughout. What IS asked of it is `orphan_tests()`: a test must still have the module it tests. That
pair is the whole treatment, and between them no file under a guarded package escapes a question.
"""
import ast
import os

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


def roots(package_dir, entry):
    """The package's modules an entry script outside it imports — the root set, read rather than kept.

    A package nothing in the repo imports is entered from one file, and that file's import block already
    says which modules are the way in. Asking it, instead of restating it here, is what keeps the root set
    from being a copy that drifts.
    """
    with open(entry, encoding="utf-8") as fh:
        source = fh.read()
    return sorted(_imported(source, os.path.basename(os.path.abspath(package_dir)),
                            set(modules(package_dir)), relative=False))


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


def orphan_tests(package_dir):
    """Tests whose subject is gone.

    A test beside a module it no longer tests outlives the deletion it should have accompanied, and reads
    from the outside exactly like coverage.
    """
    present = set(modules(package_dir))
    return sorted(entry for entry in os.listdir(package_dir)
                  if entry.endswith(TEST_SUFFIX) and entry[:-len(TEST_SUFFIX)] not in present)
