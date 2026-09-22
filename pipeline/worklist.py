#!/usr/bin/env python3
"""The WORKLIST — the universe of titles, behind the stage contract.

The rule lives in `taxonomy-backfill worklist`: which ids exist, in which order, and which of them are
already published. None of it is reimplemented here. The stage decides which files the command is handed,
which universe it is allowed to build, and reads back what it wrote.

**The universe is three different questions, and the command answers whichever `--mode` asks.** They are
modes of one command rather than three commands, and the stage keeps them that way:

  * `export` — TMDB's daily id dump, parsed. Every id that exists: the full run's universe.
  * `discover` — `/discover` sorted `vote_count.desc`, the highest-vote titles first. The pilot seed.
  * `delta` — titles released since `--since` that clear the vote floor and are NOT in the published
    labels. The daily freshness pass `scripts/delta-run.sh` drives.

**The mode is not defaulted.** The command defaults to `discover`, which for a full run means enriching the
500 highest-vote titles and calling that the catalogue, and for a delta means re-enriching everything that
is already published. Enrichment is the step that costs money per title, so a run that did not say which
universe it is building is refused here rather than given the cheapest-looking one.

**An empty worklist is a refusal in every mode but `delta`.** `Worklist.parse` drops every line it cannot
decode, so a truncated dump, an error page saved as one, or a still-gzipped file parses to nothing, writes
`[]` and exits 0 — and `enrich` then reports `remaining: 0`, which is exactly what a finished run looks
like. (The same hole in the `/discover` path was closed in `TMDBClient.PagedList`, where a required
`results` stops an error body decoding as an empty page.) In `delta` an empty list is the answer on a quiet
day, so refusing it there would fail the daily pass for doing its job. For `export` the same silence is
checked one line finer — the ids written against the dump's own line count — because a dump that arrived
half-written parses to half a catalogue, and half a catalogue looks like a catalogue.

**A delta writes the same two filenames as a full run**, which is why `scripts/delta-run.sh` keeps its
lists in `$OUT_DIR/delta/`. Point the two outputs there with `--set` rather than moving the whole out-dir,
so the labels a delta must skip are still found beside everything else: `enrich` drains whichever list it
is handed, and forty delta rows written over a 47k-title one do not corrupt anything — they end the full
run, quietly, as a batch that reports nothing remaining.
"""
import json
import os
import subprocess

from . import artifacts
from .contract import REPO, StageError, bind

NAME = "worklist"

#: The rule this stage runs. One binary builds several artifacts, so `dedicated` is false on what it owns.
PRODUCER = artifacts.BACKFILL
HOW = "taxonomy-backfill worklist"
#: Writes two files into the out-dir. Cheap to repeat — the cost is downstream, at `enrich`.
PUBLISHES = False
COMMAND = "worklist"

#: Where `swift build -c release` leaves the binary, repo-relative. The same build `pipeline/embed.py`
#: names: one binary, one place, and each wrapper refuses in its own name so an operator learns which
#: stage stopped.
BUILT = os.path.join(".build", "release", "taxonomy-backfill")

DISCOVER, EXPORT, DELTA = "discover", "export", "delta"
#: In the order the command's own usage line lists them.
MODES = (DISCOVER, EXPORT, DELTA)

#: The vote floor the shipped catalogue was built at, and the one `scripts/delta-run.sh` passes daily.
#: Passed explicitly rather than inherited from the command's default, so the universe this stage builds
#: does not move when that default does. `enrich` re-checks it per title, so this only decides how much
#: gets looked at — a brand-new release with no votes has no plot worth classifying and would be re-billed
#: every day it stayed in the list.
VOTE_FLOOR = 50

#: media -> (its slice of the daily dump, the worklist built from it). `enrich` takes one media at a time
#: and refuses a mixed list, so the command runs once per entry here.
MEDIA = {
    "movie": (artifacts.EXPORT_MOVIE, artifacts.UNIVERSE_MOVIE),
    "tv": (artifacts.EXPORT_TV, artifacts.UNIVERSE_TV),
}

#: Both dumps are `--file` to the command — one per invocation — and `labels-t02.json` is `--known` here,
#: `--labels` to the corpus join and `--vector-labels` to the store writer. The file keeps one pipeline-wide
#: name; the word on the command line belongs to whoever is reading.
INPUTS = (
    artifacts.EXPORT_MOVIE.called("file"),
    artifacts.EXPORT_TV.called("file"),
    artifacts.VECTOR_LABELS.called("known"),
)

OUTPUTS = (artifacts.UNIVERSE_MOVIE, artifacts.UNIVERSE_TV)

#: The declaration, by artifact name, so `argv` spells no flag of its own — a declaration that has drifted
#: from the command dies at its parser rather than composing a different universe.
BOUND = {bind(entry).name: bind(entry) for entry in INPUTS}


def binary():
    """The built executable, or a refusal naming the build that makes it.

    `DEN_BACKFILL_BIN` points at one elsewhere. `.build/` is gitignored and per-checkout, so a worktree or
    a machine that was handed the binary rather than the toolchain does not have it under this repo.
    """
    path = os.environ.get("DEN_BACKFILL_BIN") or os.path.join(REPO, BUILT)
    if not os.path.exists(path):
        raise StageError(f"worklist: no taxonomy-backfill at {path}. Build it with: swift build -c "
                         f"release, or point DEN_BACKFILL_BIN at a built one.")
    return path


def mode(ctx):
    """The universe this run builds, as the operator named it.

    Refused when it is unset, because the three answers are three different catalogues and the one the
    command would pick unasked is the pilot's 500 titles.
    """
    if ctx.mode not in MODES:
        raise StageError(
            f"worklist: --mode is {ctx.mode or 'unset'}, and it decides which universe this builds: "
            f"{EXPORT} (every id in TMDB's daily dump — the full run), {DISCOVER} (the highest-vote "
            f"titles, the pilot seed), {DELTA} (what is new since --since and not already published). "
            f"Enrichment is billed per title, so this is not defaulted.")
    return ctx.mode


def argv(ctx, media):
    """The command line for one media, built from the declaration and the mode.

    Each mode is handed only what it reads: a dump for `export`, the window and the published labels for
    `delta`. `--known` is required rather than optional there — without it a delta re-enriches the whole
    published catalogue, which is the one cost the pass exists to avoid, and it does so while reporting
    a perfectly ordinary count.
    """
    chosen = mode(ctx)
    export, out = MEDIA[media]
    command = [binary(), COMMAND, "--mode", chosen, "--media", media]
    if chosen == EXPORT:
        command += [BOUND[export.name].flag(), ctx.require(export)]
    else:
        command += ["--vote-floor", str(VOTE_FLOOR)]
    if chosen == DELTA:
        if not ctx.since:
            raise StageError("worklist: --mode delta needs --since YYYY-MM-DD — the window it collects "
                             "titles from. There is no default window: a delta with no date is either "
                             "every title ever released or none of them.")
        known = BOUND[artifacts.VECTOR_LABELS.name]
        command += ["--since", ctx.since, known.flag(), ctx.require(known.artifact)]
    command += ["--out", ctx.path(out)]
    return command


def rows(path):
    """Ids the dump offers: its non-blank lines, one object each.

    Counted over bytes rather than parsed, because what is being checked is how many lines the command was
    given — the decision about what each one holds is the command's.
    """
    with open(path, "rb") as fh:
        return sum(1 for line in fh if line.strip())


def check_output(ctx, media):
    """What the run left for one media, read back rather than inferred from the exit code.

    Two things the exit code cannot say, both of which leave a short universe that enrich drains happily:

      * nothing was built. `[]` is what a still-gzipped file, a truncated dump or a saved error page
        parses to, and `enrich` reads an empty worklist as a finished run. A `delta` that found nothing is
        the exception — that is the answer on a quiet day.
      * the parse dropped lines. `Worklist.parse` skips every line it cannot decode as an id, silently, so
        a dump that arrived half-written becomes half a catalogue with no complaint anywhere. Today's dump
        turns each of its 1,246,659 lines into an id, so anything short of the file's own line count is a
        universe with a hole in it.
    """
    export, artifact = MEDIA[media]
    path = ctx.path(artifact)
    if not os.path.exists(path):
        raise StageError(f"worklist: the run finished and wrote no {media} worklist at {path}.")
    with open(path, encoding="utf-8") as fh:
        entries = json.load(fh)
    chosen = mode(ctx)
    if not entries and chosen != DELTA:
        raise StageError(
            f"worklist: the run wrote an empty {path} and exited 0, so there is no {media} universe to "
            f"enrich. `enrich` reads that as a finished run rather than as a failure.")
    if chosen == EXPORT:
        dump = ctx.require(export)
        offered = rows(dump)
        if len(entries) != offered:
            raise StageError(
                f"worklist: {dump} holds {offered} lines and {path} holds {len(entries)} ids. A line that "
                f"cannot be read as an id is dropped without a word, so the difference is a universe with "
                f"a hole in it, not a filter.")
    return path


def run(ctx):
    """Build the universe for both media. Returns the two worklists.

    One invocation per media, in order, each checked before the next runs: a second media that cannot be
    built is not a reason to leave the first one unread.
    """
    os.makedirs(os.path.abspath(ctx.out_dir), exist_ok=True)
    built = []
    for media in MEDIA:
        result = subprocess.run(argv(ctx, media))
        if result.returncode != 0:
            raise StageError(f"worklist: {HOW} --mode {mode(ctx)} --media {media} exited "
                             f"{result.returncode}")
        built.append(check_output(ctx, media))
    return ", ".join(built)
