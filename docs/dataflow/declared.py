#!/usr/bin/env python3
"""Dump one checkout's stage declarations as JSON, by importing its `pipeline` package.

    python3 declared.py <tree-dir>

Run in a child process per tree, so each import sees only that tree's `pipeline/`, `lib/` and `scripts/`.
Line numbers come from the module's AST: element i of an `INPUTS`/`OUTPUTS` tuple literal is the i-th
declared entry, so the citation points at the line that names the artifact.
"""
import ast
import json
import os
import sys

tree = os.path.abspath(sys.argv[1])
sys.path.insert(0, tree)

import pipeline  # noqa: E402
from pipeline import artifacts as A  # noqa: E402
from pipeline.contract import bind  # noqa: E402


def tuple_lines(path, name):
    """[lineno of each element] of the module-level `name = (...)` tuple, or [] when it is not a literal."""
    with open(path, encoding="utf-8") as fh:
        mod = ast.parse(fh.read())
    for node in mod.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            if isinstance(node.value, ast.Tuple):
                return [elt.lineno for elt in node.value.elts], node.lineno
            return [], node.lineno
    return [], None


def assign_line(path, name):
    return tuple_lines(path, name)[1]


def artifact_lines(path):
    """{artifact name: lineno of its `X = Artifact(` assignment} in artifacts.py."""
    with open(path, encoding="utf-8") as fh:
        mod = ast.parse(fh.read())
    out = {}
    for node in mod.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            for kw in node.value.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    out[kw.value.value] = node.lineno
    return out


stages = []
for module in pipeline.stages():
    path = module.__file__
    rel = os.path.relpath(path, tree)
    entry = {"name": module.NAME, "file": rel, "producer": module.PRODUCER, "how": module.HOW,
             "spends": module.SPENDS, "publishes": module.PUBLISHES,
             "spends_line": assign_line(path, "SPENDS"), "publishes_line": assign_line(path, "PUBLISHES")}
    for field in ("INPUTS", "OUTPUTS"):
        lines, head = tuple_lines(path, field)
        rows = []
        for i, raw in enumerate(getattr(module, field)):
            b = bind(raw)
            rows.append({"artifact": b.artifact.name, "flag": b.flag(),
                         "line": lines[i] if i < len(lines) else head})
        entry[field.lower()] = rows
    stages.append(entry)

apath = os.path.join(tree, "pipeline", "artifacts.py")
alines = artifact_lines(apath)
catalogue = []
for art in A.CATALOGUE:
    catalogue.append({"name": art.name, "filename": art.filename, "producer": art.producer, "how": art.how,
                      "manifest_key": art.manifest_key, "required": art.required, "shards": art.shards,
                      "remote": art.remote, "line": alines.get(art.name)})

json.dump({"order": list(pipeline.STAGES), "order_line": assign_line(os.path.join(tree, "pipeline", "__init__.py"), "STAGES"),
           "stages": stages, "catalogue": catalogue, "registry": {k: list(v) for k, v in pipeline.producers().items()}},
          sys.stdout, indent=1)
