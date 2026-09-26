#!/usr/bin/env python3
"""Build edges.json, dataflow.html and readme-section.md for the den-dataset pipeline.

    python3 gen.py

The stage graph is read off the pipeline's own declarations (`declared.py`, imported once per ref tree
under refs/). The rest — sources, caches, side passes, releases, consumers — is `traced.py`, whose every
edge names a line and a substring that line must contain; a citation that no longer matches stops the
build. The Mermaid in both outputs is generated from edges.json, never typed.

`refs/<name>` (see `REFS`) are checkouts of each ref, `refs/merged` the tree `git merge-tree` gives for all
three; they are not committed. den-atlas, den-edge and den are read from checkouts next to den-dataset.
"""
import html
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import traced  # noqa: E402

REFS = {"origin/main": "refs/main", "origin/port-enrich": "refs/enrich",
        "origin/port-tranche-3": "refs/t3", "merged": "refs/merged"}
#: The consumer repos, checked out next to den-dataset.
SIBLINGS = {name: os.path.normpath(os.path.join(HERE, "..", "..", "..", name))
            for name in ("den-atlas", "den-edge", "den")}

_files = {}


def lines_of(ref, path):
    key = (ref, path)
    if key not in _files:
        full = os.path.join(HERE, REFS[ref], path)
        try:
            with open(full, encoding="utf-8") as fh:
                _files[key] = fh.read().split("\n")
        except OSError:
            _files[key] = None
    return _files[key]


def cite(path, line, must=None):
    """`<ref>:<path>:<line>` for a line of the merged tree, naming the ref that actually carries it."""
    merged = lines_of("merged", path)
    if merged is None or line > len(merged):
        raise SystemExit(f"citation {path}:{line} is not in the merged tree")
    text = merged[line - 1]
    if must is not None and must not in text:
        raise SystemExit(f"citation {path}:{line} does not contain {must!r}: {text.strip()!r}")
    for ref in ("origin/main", "origin/port-tranche-3", "origin/port-enrich"):
        if lines_of(ref, path) == merged:
            return f"{ref}:{path}:{line}"
    best = None
    for ref in ("origin/port-tranche-3", "origin/port-enrich", "origin/main"):
        body = lines_of(ref, path) or []
        for n, other in enumerate(body, 1):
            if other == text and (best is None or abs(n - line) < abs(best[1] - line)):
                best = (ref, n)
        if best:
            return f"{best[0]}:{path}:{best[1]}"
    raise SystemExit(f"{path}:{line} exists only in the merge result")


_heads = {}


def cite_sibling(spec, line, must):
    repo, path = spec.split("@", 1)
    root = SIBLINGS[repo]
    if repo not in _heads:
        _heads[repo] = subprocess.run(["git", "-C", root, "rev-parse", "--short", "HEAD"],
                                      capture_output=True, text=True, check=True).stdout.strip()
    body = subprocess.run(["git", "-C", root, "show", f"HEAD:{path}"], capture_output=True, text=True,
                          check=True).stdout.split("\n")
    if must not in body[line - 1]:
        raise SystemExit(f"citation {spec}:{line} does not contain {must!r}")
    return f"{repo}@{_heads[repo]}:{path}:{line}"


def declarations(ref):
    out = subprocess.run([sys.executable, os.path.join(HERE, "declared.py"), os.path.join(HERE, REFS[ref])],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


# ---- the declared graph, per ref ----------------------------------------------------------------
decl = {ref: declarations(ref) for ref in REFS}


def declared_edges(d):
    out = set()
    for st in d["stages"]:
        for row in st["inputs"]:
            out.add((row["artifact"], st["name"]))
        for row in st["outputs"]:
            out.add((st["name"], row["artifact"]))
    return out


per_ref = {ref: declared_edges(d) for ref, d in decl.items()}
M = decl["merged"]

STAGE_PHASE = {"worklist": "p1", "fetch": "p1", "articles": "p2", "classify": "p2", "docfacts": "p4",
               "embed": "p4", "finalize": "p4", "facts": "p5", "corpus": "p5", "store": "p5", "publish": "p6"}
STAGE_GROUP = {"worklist": "g_fetch", "fetch": "g_fetch", "articles": "g_classify", "classify": "g_classify",
               "docfacts": "g_embed", "embed": "g_embed", "finalize": "g_embed", "facts": "g_facts",
               "corpus": "g_store", "store": "g_store", "publish": "g_publish"}
PRODUCER_NODE = {"scripts/build-worklist.py": "sc_build_worklist", "scripts/v2/run_delta.py": "sc_run_delta",
                 "docs/OPERATE.md": "op_hand", "scripts/build-premise-tags.py": "sc_build_premise_tags",
                 "scripts/v2/embed_tags.py": "sc_embed_tags"}

nodes = {}
for i, (label, kind, group, phase, lic) in traced.N.items():
    nodes[i] = {"id": i, "label": label, "kind": kind, "group": group, "phase": phase, "licence": lic}

writer = {}
for st in M["stages"]:
    for row in st["outputs"]:
        writer[row["artifact"]] = st["name"]

def sid(name):
    """A stage's node id. Prefixed: `articles`, `facts`, `corpus` and `store` are also artifact names."""
    return "st_" + name


for st in M["stages"]:
    n = st["name"]
    nodes[sid(n)] = {"id": sid(n), "label": f"stage: {n}", "kind": "stage", "group": STAGE_GROUP[n],
                "phase": STAGE_PHASE[n], "licence": "", "source": cite(st["file"], st["publishes_line"], "PUBLISHES")}
catalogue = {a["name"]: a for a in M["catalogue"]}
for name, a in catalogue.items():
    owner = writer.get(name)
    if owner:
        phase, group = STAGE_PHASE[owner], STAGE_GROUP[owner]
    else:
        reader = next(st["name"] for st in M["stages"] for r in st["inputs"] if r["artifact"] == name)
        phase, group = STAGE_PHASE[reader], STAGE_GROUP[reader]
    if name in ("premise_labels", "premise_vectors"):
        phase, group = "p3", "g_classify"
    if name == "delta":
        phase, group = "p2", "g_classify"
    # finalize creates the manifest, but the store writer and the publisher rewrite it: it belongs with
    # the store it describes, or the overview draws the store feeding back into the embed phase.
    if name == "manifest":
        group = "g_store"
    filename = a["filename"].replace("{version}", "&lt;ver&gt;")
    nodes[name] = {"id": name, "label": f"{traced.PLAIN[name]}<br/>{filename}",
                   "kind": "release" if a["remote"] else "artifact", "group": "g_release" if a["remote"] else group,
                   "phase": phase, "licence": traced.DECLARED_LICENCE.get(name, ""),
                   "source": cite("pipeline/artifacts.py", a["line"], a["name"].upper() + " = Artifact(")}

edges = []


def add(frm, to, label, source, inferred=False, origin="traced", **extra):
    for end in (frm, to):
        if end not in nodes:
            raise SystemExit(f"edge {frm} -> {to}: no node {end}")
    edges.append({"id": f"e{len(edges) + 1}", "from": frm, "to": to, "label": label, "source": source,
                  "inferred": inferred, "origin": origin, **extra})


def in_refs(pair):
    return [ref for ref in ("origin/main", "origin/port-enrich", "origin/port-tranche-3") if pair in per_ref[ref]]


#: Stages that run before `finalize` and read `labels-t02.json`: what they read is the file the PREVIOUS
#: run's finalize wrote (pipeline/__init__.py:34-35). Drawn from a separate node so the chart does not show
#: a loop that no single run takes.
READS_PREVIOUS = {("vector_labels", "worklist"), ("vector_labels", "docfacts"), ("vector_labels", "embed")}

for st in M["stages"]:
    for row in st["inputs"]:
        pair = (row["artifact"], st["name"])
        if pair in READS_PREVIOUS:
            add("vector_labels_prev", sid(st["name"]), f"reads {row['flag']} (previous generation)",
                cite(st["file"], row["line"]), origin="declared INPUTS", declared_in=in_refs(pair),
                declared_artifact=row["artifact"], previous_generation=True)
            continue
        add(row["artifact"], sid(st["name"]), f"reads {row['flag']}", cite(st["file"], row["line"]),
            origin="declared INPUTS", declared_in=in_refs(pair))
    for row in st["outputs"]:
        pair = (st["name"], row["artifact"])
        add(sid(st["name"]), row["artifact"], "writes", cite(st["file"], row["line"]),
            origin="declared OUTPUTS", declared_in=in_refs(pair))
    if st["spends"]:
        add("gate_spend", sid(st["name"]), "den run skips it unless --spend",
            cite(st["file"], st["spends_line"], "SPENDS = True"), origin="declared SPENDS")
    if st["publishes"]:
        add("gate_publish", sid(st["name"]), "den run skips it unless --publish",
            cite(st["file"], st["publishes_line"], "PUBLISHES = True"), origin="declared PUBLISHES")
for name, a in catalogue.items():
    if a["producer"]:
        add(PRODUCER_NODE[a["producer"]], name, "registered producer",
            cite("pipeline/artifacts.py", a["line"], a["name"].upper() + " = Artifact("),
            origin="catalogue producer")

def resolve(end):
    """A traced endpoint. A bare stage name that no artifact shares is that stage; the four shared names
    (articles, facts, corpus, store) are written `st_<name>` in traced.py when they mean the stage."""
    stage_names = {st["name"] for st in M["stages"]}
    return sid(end) if end in stage_names and end not in catalogue else end


for frm, to, label, where, line, must, inferred, *extra in traced.EDGES:
    src = cite_sibling(where, line, must) if "@" in where else cite(where, line, must)
    add(resolve(frm), resolve(to), label, src, inferred, **(extra[0] if extra else {}))

licence_cites = [{"node": n, "claim": c, "source": cite(p, l, m)} for n, c, p, l, m in traced.LICENCE_CITES]

# The declared edges each branch has that the merged tree does not, and the reverse.
branch_diff = {}
for ref in ("origin/main", "origin/port-enrich", "origin/port-tranche-3"):
    branch_diff[ref] = {"only_here": sorted(map(list, per_ref[ref] - per_ref["merged"])),
                        "missing_here": sorted(map(list, per_ref["merged"] - per_ref[ref])),
                        "stages": decl[ref]["order"],
                        "producers": {k: v[0] for k, v in decl[ref]["registry"].items()}}

PHASES = {"p1": "Universe and enrichment", "p2": "Articles, Jev answers and genre & mood labels", "p3": "Premise tags",
          "p4": "Embedding", "p5": "Facts, corpus and store", "p6": "Publish, releases and consumers"}
GROUPS = {"g_tmdb": "TMDB<br/>ID dumps, API, .cache/tmdb", "g_wiki": "Wikipedia + Wikidata<br/>.cache/wiki",
          "g_models": "Jev, Claude subagents<br/>den-embed", "g_fetch": "worklist → fetch",
          "g_classify": "articles → classify<br/>genre & mood labels, critique pass, premise tags", "g_embed": "docfacts → embed → finalize",
          "g_facts": "facts", "g_store": "corpus → store", "g_publish": "publish + guards",
          "g_release": "GitHub releases", "g_consumers": "den-atlas, den-edge<br/>TV app", "g_imdb": "IMDb ratings"}
GROUP_KIND = {"g_tmdb": "source", "g_wiki": "source", "g_models": "source", "g_imdb": "source",
              "g_release": "release", "g_consumers": "consumer", "g_publish": "stage"}

doc = {"generated_by": "docs/dataflow/gen.py", "state": "origin/main + origin/port-enrich + "
       "origin/port-tranche-3, merged with `git merge-tree` (pipeline/ merges cleanly; 6 files conflict)",
       "nodes": list(nodes.values()), "edges": edges, "licence_evidence": licence_cites,
       "branch_declarations": branch_diff, "phases": PHASES, "groups": GROUPS}
with open(os.path.join(HERE, "edges.json"), "w", encoding="utf-8") as fh:
    json.dump(doc, fh, indent=1, ensure_ascii=False)

# ---- Mermaid, from edges.json ------------------------------------------------------------------
SHAPE = {"source": ('{{"', '"}}'), "stage": ('["', '"]'), "artifact": ('("', '")'), "cache": ('[("', '")]'),
         "release": ('[/"', '"\\]'), "consumer": ('(["', '"])'), "gate": ('{"', '"}'), "script": ('[["', '"]]'),
         "file": ('[/"', '"/]')}
CLASSDEF = """  classDef source fill:#e7dcf3,stroke:#6b4d91,color:#1b1426
  classDef stage fill:#cfe5e6,stroke:#2c6b70,color:#0f2224,stroke-width:2px
  classDef artifact fill:#f3f5f2,stroke:#7c8a82,color:#1b2420
  classDef cache fill:#f6e7c8,stroke:#9a6f1f,color:#2b2008
  classDef release fill:#d7ebd3,stroke:#3f7a38,color:#12240f,stroke-width:2px
  classDef consumer fill:#d8e2f3,stroke:#3d5f95,color:#101b2c
  classDef gate fill:#f6d6d2,stroke:#a3423a,color:#2e0e0b
  classDef script fill:#eceae4,stroke:#6f6a5c,color:#221f18,stroke-dasharray:4 3
  classDef file fill:#fbf6e9,stroke:#8b7b52,color:#241e0e
  classDef ghost fill:none,stroke:#9aa59f,color:#6a756f,stroke-dasharray:2 3"""


def esc(text):
    return (text.replace('"', "#quot;").replace("&lt;", "#lt;").replace("&gt;", "#gt;")
            .replace("&amp;", "&").replace("|", "#124;"))


def node_line(n, ghost=False):
    a, b = SHAPE[n["kind"]]
    cls = "ghost" if ghost else n["kind"]
    return f"  {n['id']}{a}{esc(n['label'])}{b}:::{cls}"


def edge_line(e, short=False):
    label = esc(e["label"] if not short else e["label"][:48])
    if e["from"].startswith("gate_"):
        arrow = f"-.-o|\"{label}\"|"
    elif e["inferred"]:
        arrow = f"-.->|\"{label}\"|"
    else:
        arrow = f"-->|\"{label}\"|"
    return f"  {e['from']} {arrow} {e['to']}"


def phase_chart(p):
    home = {i for i, n in nodes.items() if n["phase"] == p}
    mine = [e for e in edges if e["from"] in home or e["to"] in home]
    ghosts = {end for e in mine for end in (e["from"], e["to"])} - home
    out = ["flowchart LR", CLASSDEF]
    out += [node_line(nodes[i]) for i in sorted(home)]
    out += [node_line(nodes[i], ghost=True) for i in sorted(ghosts)]
    out += [edge_line(e) for e in mine]
    return "\n".join(out), len(home), len(ghosts), len(mine)


def overview_chart():
    crossing = {}
    for e in edges:
        g1, g2 = nodes[e["from"]]["group"], nodes[e["to"]]["group"]
        # Gates guard a node rather than carry data, so they stay in the detail diagrams.
        # Writes into a cache are the other half of a read already drawn; drawn too, every phase points
        # back at its source and the overview stops showing a direction.
        if (g1 == g2 or e["from"].startswith("gate_") or e["label"] == "registered producer"
                or nodes[e["to"]]["kind"] == "cache"):
            continue
        side = e["from"] if nodes[e["from"]]["kind"] in ("artifact", "cache", "release") else e["to"]
        label = nodes[side]["label"]
        name = ((label.rpartition("<br/>")[0] or label).replace("<br/>", " ")
                if nodes[side]["kind"] in ("artifact", "cache", "release") else "")
        crossing.setdefault((g1, g2), []).append(name)
    out = ["flowchart LR", CLASSDEF]
    for g, label in GROUPS.items():
        kind = GROUP_KIND.get(g, "stage")
        a, b = SHAPE[kind]
        out.append(f"  {g}{a}{esc(label)}{b}:::{kind}")
    for (g1, g2), names in sorted(crossing.items()):
        uniq = [n for i, n in enumerate(names) if n and n not in names[:i]]
        text = uniq[0] + (f" +{len(uniq) - 1}" if len(uniq) > 1 else "") if uniq else ""
        out.append(f"  {g1} -->|\"{esc(text)}\"| {g2}" if text else f"  {g1} --> {g2}")
    return "\n".join(out), len(GROUPS), len(crossing)


overview, ov_nodes, ov_edges = overview_chart()
charts = {p: phase_chart(p) for p in PHASES}


def disp(i):
    """How a node is named in the table: a stage by its stage name, a script by its path, anything else
    by what it is (the first line of its label)."""
    n = nodes[i]
    parts = n["label"].replace("&amp;", "&").split("<br/>")
    if n["kind"] == "stage":
        return n["label"]
    if n["kind"] == "script":
        return next(p for p in parts if "/" in p or p.endswith((".py", ".sh")))
    return parts[0]


def producers_of(i):
    return sorted({disp(e["from"]) for e in edges if e["to"] == i and e["label"] != "registered producer"
                   and not e["from"].startswith("gate_")})


def readers_of(i):
    return sorted({disp(e["to"]) for e in edges if e["from"] == i})


def lic_text(code):
    return " + ".join(traced.LICENCE.get(part, part) for part in code.split("+")) if code else ""


table_rows = []
for i, (holds, status, gate) in traced.TABLE.items():
    n = nodes[i]
    # The filename is the label's LAST line; a plain name may itself wrap over several.
    what, sep, filename = n["label"].replace("&amp;", "&").rpartition("<br/>")
    if not sep:
        what, filename = filename, ""
    table_rows.append({"what": what.replace("<br/>", " "), "file": filename.replace("<br/>", " "), "holds": holds,
                       "producer": ", ".join(producers_of(i)) or "(none in code)",
                       "readers": ", ".join(readers_of(i)) or "(nothing)", "status": status,
                       "licence": lic_text(n["licence"]), "gate": gate})

# ---- HTML ------------------------------------------------------------------------------------------
LEGEND = [("source", "External source or model"), ("stage", "Stage in STAGES"),
          ("script", "Side pass outside STAGES"), ("artifact", "Artifact (file in the out-dir)"),
          ("cache", "Response cache"), ("file", "Committed file or hand input"),
          ("release", "GitHub release"), ("consumer", "Consumer"), ("gate", "Gate or guard")]
SWATCH = {"source": "#e7dcf3", "stage": "#cfe5e6", "script": "#eceae4", "artifact": "#f3f5f2", "cache": "#f6e7c8",
          "file": "#fbf6e9", "release": "#d7ebd3", "consumer": "#d8e2f3", "gate": "#f6d6d2"}


def h(text):
    return html.escape(text, quote=True)


total_inferred = sum(1 for e in edges if e["inferred"])
counts = {"nodes": len(nodes), "edges": len(edges), "inferred": total_inferred,
          "declared": sum(1 for e in edges if e["origin"].startswith("declared"))}

sections = []
for p, title in PHASES.items():
    chart, home, ghosts, n_edges = charts[p]
    sections.append(f"""
<section class="phase" id="{p}">
  <h2>{h(title)}</h2>
  <p class="meta">{home} nodes here, {ghosts} from neighbouring phases (dashed outline), {n_edges} edges</p>
  <div class="chart"><pre class="mermaid">
{h(chart)}
</pre></div>
</section>""")

def cell(text):
    """Escaped text, with `backticked` internal names set as code."""
    parts = h(text.replace("&lt;", "<").replace("&gt;", ">")).split("`")
    return "".join(f"<code>{p}</code>" if i % 2 else p for i, p in enumerate(parts))


def artifact_cell(r):
    return f"{cell(r['what'])}<br><code>{cell(r['file'])}</code>" if r["file"] else cell(r["what"])


rows_html = "\n".join(
    "<tr><td>" + artifact_cell(r) + "</td>" +
    "".join(f"<td{' class=\"mono\"' if k in ('producer', 'readers') else ''}>{cell(r[k])}</td>"
            for k in ("holds", "producer", "readers", "status", "licence", "gate")) + "</tr>"
    for r in table_rows)
legend_html = "\n".join(f'<li><span class="sw sw-{k}" style="--sw:{SWATCH[k]}"></span>{h(t)}</li>' for k, t in LEGEND)

page = f"""<title>den-dataset Data Flow</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Sans+Condensed:wght@600&display=swap">
<style>
:root {{
  --ground: #f4f6f4; --panel: #ffffff; --ink: #1b2420; --muted: #5a6660; --rule: #d3dad5;
  --accent: #2c6b70; --accent-soft: #e1eeee; --warn: #9a3a31;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --ground: #111513; --panel: #171c1a; --ink: #e2e8e4; --muted: #9aa8a1; --rule: #2c3531;
    --accent: #7fc0c4; --accent-soft: #1d2d2e; --warn: #e79a8f;
  }}
}}
:root[data-theme="dark"] {{
  --ground: #111513; --panel: #171c1a; --ink: #e2e8e4; --muted: #9aa8a1; --rule: #2c3531;
  --accent: #7fc0c4; --accent-soft: #1d2d2e; --warn: #e79a8f;
}}
body {{ background: var(--ground); color: var(--ink); font: 15px/1.55 "IBM Plex Sans", system-ui, sans-serif;
  padding-inline: clamp(16px, 4vw, 48px); padding-block: 32px 64px; }}
.wrap {{ max-width: 1180px; margin-inline: auto; display: grid; gap: 40px; }}
h1, h2 {{ font-family: "IBM Plex Sans Condensed", "IBM Plex Sans", sans-serif; font-weight: 600;
  text-wrap: balance; margin: 0; letter-spacing: -0.01em; }}
h1 {{ font-size: 2rem; }}
h2 {{ font-size: 1.35rem; }}
p {{ margin: 0; max-width: 68ch; }}
code, .mono {{ font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 0.86em; }}
header {{ display: grid; gap: 12px; }}
.eyebrow {{ font: 500 0.75rem "IBM Plex Mono", monospace; letter-spacing: 0.08em; text-transform: uppercase;
  color: var(--accent); }}
.stats {{ display: flex; flex-wrap: wrap; gap: 8px 24px; font-variant-numeric: tabular-nums; color: var(--muted); }}
.stats b {{ color: var(--ink); font-weight: 600; }}
.top {{ display: grid; grid-template-columns: minmax(0, 1fr) 240px; gap: 24px; align-items: start; }}
@media (max-width: 820px) {{ .top {{ grid-template-columns: minmax(0, 1fr); }} }}
.legend {{ list-style: none; margin: 0; padding: 16px; display: grid; gap: 8px; background: var(--panel);
  border: 1px solid var(--rule); border-radius: 6px; font-size: 0.87rem; }}
.legend h3 {{ margin: 0 0 4px; font-size: 0.75rem; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); }}
.legend-list {{ list-style: none; margin: 0; padding: 0; display: grid; gap: 8px; }}
.legend li {{ display: flex; gap: 10px; align-items: center; }}
.sw {{ inline-size: 22px; block-size: 14px; border-radius: 3px; background: var(--sw); border: 1px solid #0003; flex: none; }}
.legend .lines {{ border-top: 1px solid var(--rule); padding-top: 8px; color: var(--muted); display: grid; gap: 4px; }}
.chart {{ overflow-x: auto; background: var(--panel); border: 1px solid var(--rule); border-radius: 6px; padding: 16px; }}
.chart pre {{ margin: 0; min-width: 900px; }}
.phase {{ display: grid; gap: 10px; }}
.meta {{ color: var(--muted); font-size: 0.87rem; }}
nav {{ display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: 0.9rem; }}
a {{ color: var(--accent); }}
a:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
.tablewrap {{ overflow-x: auto; border: 1px solid var(--rule); border-radius: 6px; background: var(--panel); }}
table {{ border-collapse: collapse; min-width: 1100px; font-size: 0.85rem; }}
th, td {{ text-align: left; vertical-align: top; padding: 8px 12px; border-bottom: 1px solid var(--rule); }}
th {{ font-size: 0.72rem; letter-spacing: 0.07em; text-transform: uppercase; color: var(--muted);
  background: var(--accent-soft); position: sticky; top: 0; }}
td.mono {{ white-space: normal; word-break: break-word; }}
.note {{ border-left: 3px solid var(--warn); padding: 4px 0 4px 14px; color: var(--ink); max-width: 76ch; }}
</style>

<div class="wrap">
<header>
  <div class="eyebrow">oxyc/den-dataset · pipeline data flow</div>
  <h1>den-dataset Data Flow</h1>
  <p>The pipeline as it stands once <code>port-enrich</code> (#51) and <code>port-tranche-3</code> land on
  <code>main</code>: enrich, finalize, facts, embed and recluster run in Python. Swift files remain in the
  tree: <code>Package.swift</code>, <code>main.swift</code>, and <code>Taxonomy.swift</code>, which the classify
  pass reads as data. The stage-to-artifact edges are read from each stage's
  <code>INPUTS</code>/<code>OUTPUTS</code>; everything else was traced by hand to a line. Every edge and its
  citation is in <code>edges.json</code>.</p>
  <p>"Genre &amp; mood labels" means the per-title primary genre, subgenres, moods and themes, stored in
  <code>labels-t02.json</code>.</p>
  <div class="stats"><span><b>{counts['nodes']}</b> nodes</span><span><b>{counts['edges']}</b> edges</span>
  <span><b>{counts['declared']}</b> from the declarations</span><span><b>{counts['inferred']}</b> inferred (dotted)</span></div>
  <nav>{"".join(f'<a href="#{p}">{h(t)}</a>' for p, t in PHASES.items())}<a href="#artifacts">Artifact table</a></nav>
</header>

<section class="top" id="overview">
  <div class="phase">
    <h2>Overview</h2>
    <p class="meta">Every node in the detail diagrams, collapsed into its phase. An arrow is drawn wherever an edge
    crosses between two groups, labelled with the artifacts it carries.</p>
    <div class="chart"><pre class="mermaid">
{h(overview)}
</pre></div>
  </div>
  <aside class="legend">
    <h3>Legend</h3>
    <ul class="legend-list">
    {legend_html}
    </ul>
    <div class="lines"><span>solid arrow: done by the cited code</span><span>dotted arrow: done by hand or
    by an agent; the cited line documents it</span>
    <span>circle end: a gate that can refuse</span><span>dashed outline: node from another phase</span>
    <span>"previous generation": the file the last run left, read before this run rewrites it</span></div>
  </aside>
</section>

<p class="note">The release carries two files: <code>den-5b1c3213b6a1.store</code> and <code>dataset.meta.json</code>
(checked on data-latest, 2026-09-22). The corpus release <code>corpus-&lt;ver&gt;</code> is uploaded by hand: no code
in the repo creates it and no publish guard checks it. Two other hand-made releases carried TMDB titles and years
and were deleted on 2026-09-22 (<code>articles-2026-09-19</code>, <code>raw-2026-09-20</code>). Their source files
still carry those fields.</p>

{"".join(sections)}

<section class="phase" id="artifacts">
  <h2>Artifact table</h2>
  <p class="meta">Producers and readers are computed from the edges. "Registered producer" edges from
  <code>pipeline/artifacts.py</code> are left out, because two of them name scripts that do not write the file.</p>
  <div class="tablewrap"><table>
    <thead><tr><th>Artifact</th><th>What it holds</th><th>Written by</th><th>Read by</th><th>Status</th><th>Licence class</th><th>Guard</th></tr></thead>
    <tbody>
{rows_html}
    </tbody>
  </table></div>
</section>
</div>
"""
with open(os.path.join(HERE, "dataflow.html"), "w", encoding="utf-8") as fh:
    fh.write(page)

# ---- README section (GitHub Markdown, no HTML) ------------------------------------------------------
#: The widest phase diagram GitHub's renderer keeps legible, in nodes (home + neighbouring).
README_MAX_NODES = 25
readme_phases = [p for p in PHASES if charts[p][1] + charts[p][2] <= README_MAX_NODES]


def md_cell(text):
    """Text for a table cell. `<ver>`/`<date>` stay escaped so GitHub does not drop them as HTML tags."""
    text = text.replace("<", "&lt;").replace(">", "&gt;").replace("&amp;lt;", "&lt;").replace("&amp;gt;", "&gt;")
    return text.replace("|", "\\|")


def md_file(text):
    """A filename, in backticks, where `<ver>` is literal and needs no escaping."""
    return f"`{text.replace('&lt;', '<').replace('&gt;', '>')}`" if text else ""


dropped = [PHASES[p] for p in PHASES if p not in readme_phases]
md = ["## How data flows", "",
      "Generated from the stage declarations and `edges.json`; regenerate it rather than editing it. A solid",
      "arrow is done by the cited code. A dotted arrow is done by hand or by an agent, and the cited line only",
      "documents it. A circle end is a gate that can refuse. \"Genre & mood labels\" are the per-title primary",
      "genre, subgenres, moods and themes (`labels-t02.json`). \"Previous generation\" marks a file the last",
      "run left behind, read before this run rewrites it.", "", "```mermaid", overview, "```", ""]
for p in readme_phases:
    md += [f"### {PHASES[p]}", "", "```mermaid", charts[p][0], "```", ""]
if dropped:
    md += [f"The other {len(dropped)} phase diagrams ({', '.join(dropped)}) are too wide for GitHub's renderer. "
           "They are in `dataflow.html`, which `gen.py` writes alongside this section.", ""]
md += ["### Artifacts", "", "| artifact | file | holds | written by | read by | status | licence | guard |",
       "|---|---|---|---|---|---|---|---|"]
for r in table_rows:
    md.append("| " + " | ".join([md_cell(r["what"]), md_file(r["file"])] +
                                [md_cell(r[k]) for k in ("holds", "producer", "readers", "status", "licence", "gate")]) + " |")
with open(os.path.join(HERE, "readme-section.md"), "w", encoding="utf-8") as fh:
    fh.write("\n".join(md) + "\n")

print(json.dumps({"nodes": counts["nodes"], "edges": counts["edges"], "inferred": counts["inferred"],
                  "declared": counts["declared"], "overview": [ov_nodes, ov_edges],
                  "phases": {p: charts[p][1:] for p in PHASES}, "readme_phases": readme_phases}, indent=1))
