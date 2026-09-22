#!/usr/bin/env python3
"""The EMBED pass, behind the stage contract.

The rule lives in `taxonomy-backfill embed-corpus`: it composes one document per title — Wikidata's
director and genre, our own tags, the capped Wikipedia plot — and batch-embeds it through den-embed into
two append-only stores. None of that is reimplemented here, and this stage is deliberately thin, because
the composition is the one part of this pipeline where a wrong answer looks right: a document assembled
slightly differently still embeds, still ranks, and still returns ten plausible neighbours. Nothing
downstream can see that the space moved. So the stage decides which files the command is handed and what
it is allowed to compose, and runs it.

**The composition is not a set of options.** `--doc-facts` (the lean CC0 shape), `--doc-drop-director` and
`--plot-cap 3500` are what the shipped index was built with, recorded in `index/composition.json` as
`{"docShape":"lean","dropDirector":true,"plotCap":3500}`; anything else is a different vector space in the
same file. They were not written down at the time and had to be recovered by re-embedding probe titles and
comparing bytes against the shipped rows — 12/12 exact at these settings, 10/12 with the director clause
kept, 2/12 at cap 1500 — so they are constants here rather than arguments, and the run's own record is
checked against them afterwards. That check is not ceremony: `embed-corpus` parses its arguments with a
hand-rolled reader that ignores what it does not recognise, so a misspelled `--doc-drop-director` composes
a different document in silence, and on a fresh out-dir the mismatch guard has nothing to compare against.

**Resume is the property that makes a 12-hour run interruptible**, and a wrapper is exactly the kind of
thing that quietly breaks it. `embed-corpus` appends to `index/labels.jsonl` and `index/vectors.jsonl`,
skips every key already in them, and reconciles the pair first in case a kill landed between a label line
and its vector line. All of that depends on the run being pointed at the SAME out-dir and finding its
stores where it left them, so this stage creates the out-dir and touches nothing inside it.

**The canary gates this path like every other.** `embed-corpus` verifies `data/embed-canary.json` against
the service before it writes its first vector and fails closed. It resolves that file relative to the
working directory, so the stage names it outright — a stage invoked from somewhere else must still be
gated by THIS checkout's known answers, rather than dying on a path or being waved through by another
set. `DEN_EMBED_URL` picks the service, and is the operator's to set: it decides which embedding space the
corpus lands in, and the canary is what checks the answer. A run that leaves no verdict behind is refused
here rather than accepted as quiet success — see `check_outputs`.
"""
import json
import os
import subprocess

from . import artifacts
from .contract import REPO, StageError, bind

NAME = "embed"

#: The rule this stage runs. One binary builds several artifacts, so `dedicated` is false on everything it
#: owns — a staleness warning on `main.swift` fires for edits that have nothing to do with the vectors.
PRODUCER = artifacts.BACKFILL
HOW = "taxonomy-backfill embed-corpus"
#: Writes into the out-dir and nowhere else. Expensive to repeat — a full corpus is ~12 hours —
#: but repeatable, and resumable, which is a different thing from irreversible.
PUBLISHES = False
#: den-embed is self-hosted, so a re-embed costs hours and no money.
SPENDS = False
COMMAND = "embed-corpus"

#: Where `swift build -c release` leaves the binary, repo-relative.
BUILT = os.path.join(".build", "release", "taxonomy-backfill")

#: In the command's own argument order. `labels-t02.json` is `--labels` here and `--vector-labels` to the
#: store writer; the file keeps one name and each reader keeps its own word for it.
INPUTS = (
    artifacts.VECTOR_LABELS.called("labels"),
    artifacts.ENRICHED.called("enriched_dir"),
    artifacts.DOC_FACTS,
)

#: The two append-only stores, and the three records that say which space and which document shape the
#: rows in them belong to. The stores are `finalize`'s input; the records gate the next run and are what
#: `dataset.meta.json` names the space by.
OUTPUTS = (
    artifacts.EMBED_LABELS,
    artifacts.EMBED_VECTORS,
    artifacts.COMPOSITION,
    artifacts.EMBEDDER,
    artifacts.EMBEDDING_SPACE,
)

#: The document shape the shipped index was built with, as `index/composition.json` records it. The cap is
#: the one value here that is not pinned by the bytes: `cappedPlot` snaps back to the last ". ", so each
#: probe title is insensitive across an interval and intersecting them gives [3479..3534]. 3500 is the
#: round number in that window, not a measurement of its own.
SHIPPED_COMPOSITION = {"docShape": "lean", "dropDirector": True, "plotCap": 3500}

#: den-embed's per-request budget: it accepts a request while `sum(min(actual_tokens, max_tokens))` is
#: within this, and answers 413 otherwise.
TOKEN_BUDGET = 8192
#: What the serving box runs, permanently, and what the canary's answers were recorded at. A different cap
#: truncates different documents, which is a different space — the canary refuses it before this does.
MAX_TOKENS = 1024

#: Documents per request. `embed-corpus`'s own default is 15 and does not fit: 15 and even 16 pass ONLY
#: while `min()` clips every document to exactly `max_tokens`, so the sum lands on the budget and the
#: service's test is `>`. At MAX_TOKENS=1024 the clip stops binding, a full request exceeds the budget, and
#: den-embed answers 413 — which the transport treats as definitive, so the run dies on its first flush
#: having written nothing at all. 8192/1024 is 8; 7 leaves the margin.
CHUNK = 7


def binary():
    """The built executable, or a refusal naming the build that makes it.

    `DEN_BACKFILL_BIN` points at one elsewhere. `.build/` is gitignored and per-checkout, so a worktree or
    a machine that was handed the binary rather than the toolchain does not have it under this repo.
    """
    path = os.environ.get("DEN_BACKFILL_BIN") or os.path.join(REPO, BUILT)
    if not os.path.exists(path):
        raise StageError(f"embed: no taxonomy-backfill at {path}. Build it with: swift build -c release, "
                         f"or point DEN_BACKFILL_BIN at a built one.")
    return path


def argv(ctx):
    """The command line, built from the declaration and the pinned composition.

    Every input is required. `--doc-facts` in particular: without it the command composes the FULL document
    shape — title, year and cast — which is not what anything ships, so an absent file would not degrade
    the run, it would silently change what the vectors mean.
    """
    command = [binary(), COMMAND]
    for entry in (bind(e) for e in INPUTS):
        command += [entry.flag(), ctx.require(entry.artifact)]
    command += ["--out-dir", ctx.out_dir,
                "--doc-drop-director",
                "--plot-cap", str(SHIPPED_COMPOSITION["plotCap"]),
                "--chunk", str(CHUNK)]
    # Both are per-run and neither has a safe default to pass unasked: a pause nobody chose costs hours,
    # and a limit nobody chose stops a run early and reports it as done.
    if ctx.pause_ms:
        command += ["--pause-ms", str(ctx.pause_ms)]
    if ctx.limit is not None:
        command += ["--limit", str(ctx.limit)]
    return command


def environment():
    """The run's environment, with the canary named outright.

    `embed-corpus` looks for `data/embed-canary.json` relative to the working directory, which is right for
    a command typed at the repo root and wrong for a stage invoked from anywhere else. An environment that
    already names one is left alone — that is how the box runs it against a mounted copy.
    """
    env = dict(os.environ)
    env.setdefault("DEN_EMBED_CANARY", os.path.join(REPO, "data", "embed-canary.json"))
    return env


def check_outputs(ctx):
    """Every record the run should have left, where the declaration says it is.

    The verified space gets its own refusal because its absence is not a misplaced file, it is a run
    nothing gated: a binary built before the canary landed embeds the whole corpus happily and records no
    space at all. The command cannot notice that about itself — the code that would complain is the code
    that is missing — so the stage is where a run with no verdict stops.
    """
    for entry in (bind(e) for e in OUTPUTS):
        path = ctx.path(entry.artifact)
        if os.path.exists(path):
            continue
        if entry.artifact is artifacts.EMBEDDING_SPACE:
            raise StageError(
                f"embed: the run wrote no {path}, so nothing says which embedding space these vectors are "
                f"in — the known-answer canary did not run. A taxonomy-backfill built before that gate "
                f"landed behaves exactly like this. Rebuild it with: swift build -c release.")
        raise StageError(
            f"embed: the run finished and wrote no {entry.name} at {path}. The command derives all of "
            f"these from --out-dir, so an override that points one somewhere else leaves the store and "
            f"its identity records split between two directories.")


def check_composition(ctx):
    """Hold the run's own record against the composition this stage is allowed to produce.

    The record is written from the flags the command actually parsed, so it is the only thing that can say
    what was composed rather than what was meant. On a store with rows the command refuses a change itself;
    this is for the fresh out-dir, where there is nothing to disagree with and a dropped flag becomes the
    recorded truth.
    """
    path = ctx.path(artifacts.COMPOSITION)
    with open(path, encoding="utf-8") as fh:
        recorded = json.load(fh)
    got = {key: recorded.get(key) for key in SHIPPED_COMPOSITION}
    if got != SHIPPED_COMPOSITION:
        raise StageError(
            f"embed: the run recorded {got} in {path}, and this stage composes {SHIPPED_COMPOSITION}. "
            f"Those are two document shapes in one vector space, which no similarity score can separate "
            f"afterwards. The rows this run wrote are in the recorded shape — start a fresh --out-dir.")


def run(ctx):
    """Embed the corpus into the append-only stores. Returns the vectors store's path.

    Resumable: re-running against the same out-dir continues where the last one stopped, and a run that
    finds everything embedded writes nothing and exits clean.
    """
    os.makedirs(os.path.abspath(ctx.out_dir), exist_ok=True)
    result = subprocess.run(argv(ctx), env=environment())
    if result.returncode != 0:
        raise StageError(f"embed: {HOW} exited {result.returncode}")
    check_outputs(ctx)
    check_composition(ctx)
    return ctx.path(artifacts.EMBED_VECTORS)
