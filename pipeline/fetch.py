#!/usr/bin/env python3
"""The FETCH pass — enrichment — behind the stage contract.

The rule lives in `taxonomy-backfill enrich`: take the next N un-enriched ids off a worklist, one TMDB
detail+keywords+credits call each, one Wikidata SPARQL per media type, then a live Wikipedia plot that
REPLACES the TMDB overview where one is found. None of that is reimplemented here. What the stage owns is
which worklist the command is handed, the credentials it runs under, and the loop.

**It is a loop, and the loop is the stage's.** One `enrich` is one batch; the corpus is ~120 of them. The
driver that ran them — `scripts/enrich-all.sh` — decided when to stop by `sed`-ing `belowFloor` out of a
JSON line and shelling out to python3 for `remaining`, off a stream it was also teeing to a log. Three
stopping rules live in there, each bought by a run that went wrong (see `drain`), and they are ported
rather than dropped. They are here, and not behind a `bash scripts/enrich-all.sh`, for one reason: a
resumed, partially-complete run is easier to reason about when ONE thing decides what "done" means and
that thing is the same thing that declares the artifacts. The driver builds its own argument list, picks
its own out-dir (`OUT_DIR:-out`) and its own worklist name, and runs `swift build` on the way past; a
stage that shelled out to it would declare INPUTS and OUTPUTS that nothing on the command line was built
from — which is the one thing `pipeline/contract.py` exists to prevent. Wrapping it would also put the
resume behind two layers: the checkpoint the command reads, and a shell loop deciding whether to call it
again, with no way for the stage to check afterwards that the batches and the checkpoint are where the
declaration says.

**The command now declares its flags and refuses what it has not declared** — an unknown flag, a value
flag with no value, a stray bare word. So the argument list below is not a convenience: it is held to the
`enrich` row of `Spec` in `Sources/taxonomy-backfill/main.swift`, and a flag this stage invents is a
refusal at the first batch rather than a default that silently stood.

**Every batch costs TMDB quota**, which is why the stage adds no retry of its own beyond the driver's and
why nothing here re-runs a batch that succeeded. `--exclude-anime` is never sent: it is opt-IN in the
command because excluding anime by default silently cost the corpus 1,498 titles, and a stage that sent it
unasked would reinstate exactly that.
"""
import json
import os
import subprocess
import sys
import time

from . import artifacts
from .contract import REPO, StageError, bind
# One spelling of where the built binary is and what to say when it is not there. It is the same binary as
# the embed pass's, so a second copy of that lookup could only ever disagree with the first.
from .embed import binary

NAME = "fetch"

#: The rule this stage runs. One binary builds several artifacts, so `dedicated` is false on what it owns.
PRODUCER = artifacts.BACKFILL
#: What an operator types to rebuild the enriched batches. Not `taxonomy-backfill enrich`: one of those is
#: one batch, and what builds this artifact is the drain.
HOW = "./den stage fetch"
#: Writes into the out-dir and nowhere else. Expensive to repeat in TMDB quota and in hours, and resumable,
#: which is a different thing from reversible.
PUBLISHES = False
#: TMDB's own API and live Wikipedia, neither of them billed. The cost is quota and hours, not money, so a
#: `den run` that drains a worklist buys nothing back from a paid provider.
SPENDS = False
COMMAND = "enrich"

#: The universe holds ONE media type per file, so it is two files and a full drain is two runs of the loop.
#: Each is bound to the one name the command spells them by: whichever media a batch is drawing from, the
#: flag is `--worklist`. The pipeline goes on seeing `universe_movie` and `universe_tv` — the name the
#: worklist stage writes them under — and the command line carries the word its reader has for whichever
#: one it was handed.
MEDIA = ("movie", "tv")
UNIVERSES = {"movie": artifacts.UNIVERSE_MOVIE.called("worklist"),
             "tv": artifacts.UNIVERSE_TV.called("worklist")}

#: A run that names a media requires only that media's file; a run that names none requires both.
INPUTS = (UNIVERSES["movie"], UNIVERSES["tv"])

#: The batches, and the checkpoint that decides what a resumed run does. A stage that declared only the
#: batches would leave the resume state owned by nobody — and an absent checkpoint is not an empty one:
#: `enrich` re-derives `nextBatch` from the directory precisely because a missing file once restarted the
#: numbering and overwrote two batches whose vote passes still named the titles they used to hold.
OUTPUTS = (artifacts.ENRICHED, artifacts.ENRICH_CHECKPOINT)

#: Ids per batch — `enrich-all.sh`'s size for a full run. The batch is the unit of resume: the checkpoint
#: is written once per batch, so a smaller one buys only more checkpoint writes and a larger one risks more
#: re-fetching after a kill. It is not a correctness knob, so it is a constant rather than an argument.
BATCH = 500

#: Consecutive non-zero exits before the drain stops for inspection, and consecutive batches that exit 0
#: having moved `remaining` not at all. Both are `enrich-all.sh`'s, and both are ceilings on a backoff that
#: grows with the count — 30s per abort, 60s per stall.
ABORTS = 6
STALLS = 6

#: The credential bootstrap, as `scripts/lib/den-env.sh` defines it. Two INDEPENDENT steps, and the split
#: matters: `den_load_env` reads a `den.env` FILE and fails without one, while `enterprise_login` mints a
#: 24h Wikimedia bearer from environment credentials. Run per batch, like the driver ran it, because a
#: token lasts a day and a drain can outlast one.
#:
#: They are separate constants because the two credentials arrive together on a workstation and apart
#: everywhere else — GitHub Actions puts both in the environment as secrets with no file to read, so a
#: bootstrap that insisted on `den.env` would exit 1 there before fetching anything.
LOAD_AND_LOGIN = '. scripts/lib/den-env.sh; den_load_env || exit 1; enterprise_login; exec "$@"'
LOGIN_ONLY = '. scripts/lib/den-env.sh; enterprise_login; exec "$@"'


def media_types(ctx):
    """The media this run drains, in order.

    Both, unless one is named. A full corpus is both and always was — `docs/OPERATE.md` says "movie 150;
    then tv 150" — so a stage that made the operator say which would be a step that can be half-done
    without anything noticing.
    """
    if not ctx.media:
        return MEDIA
    if ctx.media not in MEDIA:
        raise StageError(f"fetch: --media {ctx.media!r} is not one of {', '.join(MEDIA)}. A worklist holds "
                         f"one media type, and the command refuses a batch that mixes them.")
    return (ctx.media,)


def argv(ctx, media):
    """One batch's command line, in the order `scripts/enrich-all.sh` writes it.

    The worklist is required: a drain pointed at a worklist that is not there would checkpoint nothing and
    report `remaining` 0, which reads exactly like a finished run.
    """
    entry = bind(UNIVERSES[media])
    command = [binary(), COMMAND,
               entry.flag(), ctx.require(entry.artifact),
               "--limit", str(BATCH),
               "--out-dir", ctx.out_dir]
    # Only when it is named. The command's own default is 50 and the below-floor refusal in `drain` is what
    # sends an operator here; passing a floor nobody chose would either re-admit the low-vote tail or hide
    # that the worklist cannot drain at the default.
    if ctx.vote_floor is not None:
        command += ["--vote-floor", str(ctx.vote_floor)]
    return command


def bootstrap(command):
    """`command`, run with the credentials the shell library mints.

    The two credentials are asked about SEPARATELY, because `TMDB_API_KEY` says nothing about the Wikimedia
    ones. Keying the whole bootstrap on the TMDB key meant an operator who exported it by hand skipped
    `enterprise_login` as well, and silently fetched every plot from the free action API — a run that
    differs from the box's in what it records, not just in speed, since the Enterprise path reports no
    revision id and no resolved article (`WikipediaSource.PlotFetch`) and is therefore the one that CANNOT
    see a redirect. Nothing downstream could tell the two runs apart.

    So: `den.env` is read only when the TMDB key is missing, and the Enterprise login runs whenever no
    bearer is already held — it mints one from environment credentials where they exist, and says so on
    stderr where they do not.
    """
    if not os.environ.get("TMDB_API_KEY"):
        return ["bash", "-c", LOAD_AND_LOGIN, NAME, *command]
    if not os.environ.get("WIKIMEDIA_ENTERPRISE_TOKEN"):
        return ["bash", "-c", LOGIN_ONLY, NAME, *command]
    return command


def pause(seconds):
    """The backoff between a failed or stalled batch and the next one."""
    time.sleep(seconds)


def summary(stdout):
    """The batch report `enrich` prints — its last line of stdout, as JSON.

    Refused rather than defaulted when it will not parse. The driver fell back to a literal `"?"` here,
    which is neither 0 nor the previous value, so an unreadable report read as progress: the loop could
    not tell a finished drain from a batch whose output it had lost.
    """
    lines = [line for line in stdout.splitlines() if line.strip()]
    try:
        return json.loads(lines[-1])
    except (IndexError, ValueError) as unreadable:
        raise StageError(f"fetch: the batch printed no report this loop could read ({unreadable}). Its last "
                         f"line decides whether anything remains, so a run cannot continue without it.")


def announce_prose_source():
    """Say which Wikipedia a drain is about to read, before it reads any of it.

    `enterprise_login` announces its own outcome on stderr, but only on the runs that call it — a run
    holding a bearer already says nothing at all, and the choice is invisible afterwards. It is not a
    performance detail: the Enterprise path returns structured contents with no page title, so it records
    no revision id and no resolved article, and a corpus enriched through it cannot report which of its
    plots arrived via a redirect. Two runs of the same command against the same worklist can differ in what
    they know about their own rows, so the log says which one this is.
    """
    held = os.environ.get("WIKIMEDIA_ENTERPRISE_TOKEN")
    source = ("Wikimedia Enterprise (bearer already held; no revision ids, no redirect detection)"
              if held else "resolved per batch by enterprise_login — see its line below")
    print(f"==> {NAME}: plot prose from {source}", file=sys.stderr)


def drain(ctx, media):
    """Run batches until the worklist has nothing left. Returns the number of batches that ran.

    Three stopping rules, all `enrich-all.sh`'s, all bought by a run:

      * **an aborted batch is retried, not fatal.** A transient Wikidata outage that outlived the
        command's own retries used to kill a twelve-hour drain.
      * **a batch that exits 0 having moved nothing is a stall.** Ids that fail transiently are
        deliberately not checkpointed, so during an upstream outage every id defers and `remaining` does
        not move. Counting only non-zero exits, the loop spun with no sleep at all — re-minting a token and
        re-issuing the whole batch as fast as the upstream could refuse it.
      * **a batch where every title was below the vote floor is not an outage.** Below-floor ids are not
        checkpointed either ("a vote count only climbs"), so a worklist of them can never drain at that
        floor. That reported "upstream is refusing" for two and a half hours while Wikipedia was answering
        perfectly well, so it says which it is.
    """
    aborts = stalls = batches = 0
    previous = None
    while True:
        # stdout is captured because the last line of it is the batch report; stderr is not, so the
        # Enterprise login's messages and the command's re-covered-key warnings arrive while they mean
        # something rather than at the end of a twelve-hour run.
        result = subprocess.run(bootstrap(argv(ctx, media)), cwd=REPO, stdout=subprocess.PIPE, text=True)
        if result.returncode != 0:
            aborts += 1
            if aborts >= ABORTS:
                raise StageError(f"fetch: {ABORTS} consecutive {media} batches aborted (last exit "
                                 f"{result.returncode}) — stopping for inspection. The checkpoint holds "
                                 f"everything the successful batches processed, so this resumes.")
            pause(aborts * 30)
            continue
        aborts = 0
        batches += 1
        report = summary(result.stdout)
        print(f"{media}: {json.dumps(report, sort_keys=True)}", flush=True)
        remaining = report.get("remaining")
        if remaining == 0:
            return batches
        if remaining == previous:
            if report.get("belowFloor", 0) > 0 and report.get("count", 0) == 0:
                raise StageError(
                    f"fetch: every title in this {media} batch is below the vote floor, so the worklist "
                    f"cannot drain at it — below-floor ids are not checkpointed, because a vote count only "
                    f"climbs. Re-run with --vote-floor 0 to include the low-vote tail, or filter the "
                    f"worklist. This is not an upstream failure.")
            stalls += 1
            if stalls >= STALLS:
                raise StageError(f"fetch: {STALLS} {media} batches in a row exited clean with {remaining} "
                                 f"still pending — the ids are being attempted and deferred, which is an "
                                 f"upstream refusing rather than a worklist that is done. Stopping.")
            pause(stalls * 60)
        else:
            stalls = 0
        previous = remaining


def check_outputs(ctx):
    """The batches and the checkpoint, where the declaration says they are.

    Both are derived by the command from `--out-dir`, so an override that points one elsewhere does not
    move it — it splits the batches from the state that says which ids they cover, and the next run then
    re-enriches everything the missing checkpoint no longer accounts for.
    """
    for entry in (bind(e) for e in OUTPUTS):
        path = ctx.path(entry.artifact)
        if not os.path.exists(path):
            raise StageError(f"fetch: the run finished and wrote no {entry.name} at {path}. An empty "
                             f"worklist enriches nothing and leaves neither, and the command derives both "
                             f"from --out-dir, so an override that names one somewhere else splits the "
                             f"batches from their checkpoint.")


def run(ctx):
    """Drain the worklists into the enriched batches. Returns the batch directory's path.

    Resumable: a re-run against the same out-dir takes only ids the checkpoint does not hold, and one that
    finds everything enriched runs a single batch, fetches nothing and exits clean.
    """
    os.makedirs(os.path.abspath(ctx.out_dir), exist_ok=True)
    announce_prose_source()
    ran = {media: drain(ctx, media) for media in media_types(ctx)}
    check_outputs(ctx)
    batches = ", ".join(f"{count} {media} batch(es)" for media, count in ran.items())
    return f"{ctx.path(artifacts.ENRICHED)} ({batches})"
