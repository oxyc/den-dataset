#!/usr/bin/env python3
"""The FETCH pass — enrichment — as a stage: drain a worklist into enriched batches.

One batch is `pipeline/enrich.py`: the next N un-enriched ids, one TMDB detail+keywords+credits call each,
one Wikidata SPARQL per media type, then a live Wikipedia plot that becomes the record's `overview`. What
this stage owns is which worklist it is handed, the credentials it runs under, and the loop.

**It is a loop, and the loop is the stage's.** The corpus is ~120 batches. The driver that ran them —
`scripts/enrich-all.sh` — decided when to stop by `sed`-ing `belowFloor` out of a JSON line, off a
stream it was also teeing to a log. Its three stopping rules were each bought by a run that went wrong
(see `drain`), and they are here because a resumed, partially-complete run is easier to reason about when
ONE thing decides what "done" means and that thing is the same thing that declares the artifacts.

**Every batch costs TMDB quota**, which is why nothing here re-runs a batch that succeeded.
`--exclude-anime` is never asked for: it is opt-IN because excluding anime by default silently cost the
corpus 1,498 titles, and a stage that asked for it unprompted would reinstate exactly that.
"""
import json
import os
import subprocess
import sys
import time

from . import artifacts, enrich
from .contract import REPO, StageError, bind
from lib import enterprise, tmdb as tmdb_api

NAME = "fetch"

#: The rule this stage runs. It builds two artifacts, so `dedicated` is false on both.
PRODUCER = "pipeline/enrich.py"
#: What an operator types to rebuild the enriched batches. Not one `pipeline/enrich.py`: that is one
#: batch, and what builds this artifact is the drain.
HOW = "./den stage fetch"
#: Writes into the out-dir and nowhere else. Expensive to repeat in TMDB quota and in hours, and resumable,
#: which is a different thing from reversible.
PUBLISHES = False
#: TMDB's own API and live Wikipedia, neither of them billed. The cost is quota and hours, not money.
SPENDS = False

#: A full drain is both media, one universe file each — the worklist stage writes them apart.
MEDIA = ("movie", "tv")
UNIVERSES = {"movie": artifacts.UNIVERSE_MOVIE.called("worklist"),
             "tv": artifacts.UNIVERSE_TV.called("worklist")}

#: A run that names a media requires only that media's file; a run that names none requires both.
INPUTS = (UNIVERSES["movie"], UNIVERSES["tv"])

#: The batches, and the checkpoint that decides what a resumed run does. A stage that declared only the
#: batches would leave the resume state owned by nobody — and an absent checkpoint is not an empty one:
#: `enrich` re-derives the next batch number from the directory precisely because a missing file once
#: restarted the numbering and overwrote two batches.
OUTPUTS = (artifacts.ENRICHED, artifacts.ENRICH_CHECKPOINT)

#: Ids per batch — the size `enrich-all.sh` drained a full run at. The batch is the unit of resume: the
#: checkpoint is written once per batch, so a smaller one buys only more checkpoint writes and a larger one
#: risks more re-fetching after a kill. Not a correctness knob, so a constant rather than an argument.
BATCH = 500

#: Consecutive aborted batches before the drain stops for inspection, and consecutive batches that finish
#: having moved `remaining` not at all. Both are ceilings on a backoff that grows with the count — 30s per
#: abort, 60s per stall.
ABORTS = 6
STALLS = 6

#: The credential bootstrap, over `scripts/lib/den-env.sh`. Two INDEPENDENT steps, and the split matters:
#: `den_load_env` reads a `den.env` FILE and fails without one, while `enterprise_login` mints a 24h
#: Wikimedia bearer from environment credentials. They arrive together on a workstation and apart
#: everywhere else — GitHub Actions puts both in the environment as secrets with no file to read, so a
#: bootstrap that insisted on `den.env` would fail there before fetching anything.
#:
#: The values come back NUL-separated on a pipe this process owns; they never reach an argv, a log or a
#: file. The shell's own messages go to stderr, where an operator sees them as they happen.
LOAD_AND_LOGIN = ('. scripts/lib/den-env.sh; den_load_env >&2 || exit 1; enterprise_login; '
                  'printf "%s\\0%s\\0" "$TMDB_API_KEY" "${WIKIMEDIA_ENTERPRISE_TOKEN:-}"')
LOGIN_ONLY = ('. scripts/lib/den-env.sh; enterprise_login; '
              'printf "%s\\0%s\\0" "$TMDB_API_KEY" "${WIKIMEDIA_ENTERPRISE_TOKEN:-}"')


def media_types(ctx):
    """The media this run drains, in order — both, unless one is named. A step that has to be remembered
    twice is a step that gets half-done."""
    if not ctx.media:
        return MEDIA
    if ctx.media not in MEDIA:
        raise StageError(f"fetch: --media {ctx.media!r} is not one of {', '.join(MEDIA)}.")
    return (ctx.media,)


def credentials(environ=None):
    """`(tmdb_key, enterprise_bearer_or_None)`, asked about SEPARATELY.

    `TMDB_API_KEY` says nothing about the Wikimedia ones. Keying the whole bootstrap on it meant an operator
    who exported it by hand skipped `enterprise_login` too, and silently fetched every plot from the free
    action API — a run that differs from the box's in what it RECORDS, since the Enterprise path reports no
    revision id and no resolved article and so cannot see a redirect.

    So `den.env` is read only when the TMDB key is missing, and the Enterprise login runs whenever no bearer
    is already held. Asked per batch, as the driver did, because a bearer lasts a day and a drain can
    outlast one.
    """
    environ = os.environ if environ is None else environ
    key, bearer = environ.get("TMDB_API_KEY"), environ.get("WIKIMEDIA_ENTERPRISE_TOKEN")
    if key and bearer:
        return key, bearer
    script = LOGIN_ONLY if key else LOAD_AND_LOGIN
    minted = subprocess.run(["bash", "-c", script], cwd=REPO, env=dict(environ), stdout=subprocess.PIPE)
    if minted.returncode != 0:
        raise StageError("fetch: no TMDB_API_KEY in the environment and none loaded from den.env — see the "
                         "message above. Waiting will not create one, so this is not retried.")
    loaded, token = (minted.stdout.decode("utf-8").split("\0") + ["", ""])[:2]
    return key or loaded, token or None


def pause(seconds):
    """The backoff between a failed or stalled batch and the next one."""
    time.sleep(seconds)


#: The batch report's counts: which source served each grounded plot, and the Enterprise requests sent.
SERVED = ("plotsFromEnterprise", "plotsFromActionApi", "enterpriseRequests")


def announce_prose_source(media, served):
    """Say which Wikipedia SERVED a drain's plots, counted from its batch reports, and what the account's
    on-demand count was at the start and end — read from the server, which sees every machine on it.

    Not which was asked: this once announced a held bearer as the source before anything was read, and a
    bearer that was then throttled or expired fell back title by title with nothing saying so. It is not a
    performance detail: the Enterprise path records no revision id and no resolved article, so two runs of
    the same command against the same worklist can differ in what they know about their own rows.
    """
    line = (f"==> {NAME}: {media} plots served by Wikimedia Enterprise: {served['plotsFromEnterprise']}, "
            f"by the free action API: {served['plotsFromActionApi']}; Enterprise requests sent: "
            f"{served['enterpriseRequests']}")
    first, latest = enterprise.gate.refresh()
    if first is not None:
        line += (f"; the account's on-demand count {first[0]:,} → {latest[0]:,} of {latest[1]:,} "
                 f"(get-user, all machines)")
    elif enterprise.gate.unknown:
        line += f"; the account's on-demand count is unknown ({enterprise.gate.unknown})"
    if served.get("enterpriseOff"):
        line += f" — Enterprise was switched off during the run ({served['enterpriseOff']})"
    print(line, file=sys.stderr)


def batch(ctx, media):
    """One batch of `media`'s worklist, under freshly asked credentials. Returns its report."""
    key, token = credentials()
    return enrich.run(ctx.require(bind(UNIVERSES[media]).artifact), ctx.out_dir,
                      vote_floor=enrich.VOTE_FLOOR if ctx.vote_floor is None else ctx.vote_floor,
                      limit=BATCH, client=tmdb_api.TMDB(key), token=token)


def drain(ctx, media):
    """Run batches until the worklist has nothing left. Returns the number of batches that ran.

    Three stopping rules, all `enrich-all.sh`'s, all bought by a run:

      * **an aborted batch is retried, not fatal.** A transient Wikidata outage that outlived the retries
        used to kill a twelve-hour drain. Only an abort is retried: a refusal — an unreadable checkpoint, a
        batch file that would be overwritten, no TMDB key — says the same thing on every attempt, and the
        Swift driver spent six backoffs finding that out.
      * **a batch that finishes having moved nothing is a stall.** Ids that fail transiently are
        deliberately not checkpointed, so during an upstream outage every id defers and `remaining` does
        not move. Counting only aborts, the loop spun with no sleep, re-issuing the whole batch as fast as
        the upstream could refuse it.
      * **a batch where every title was below the vote floor is not an outage.** Below-floor ids are not
        checkpointed either ("a vote count only climbs"), so a worklist of them can never drain at that
        floor. That reported "upstream is refusing" for two and a half hours while Wikipedia was answering
        perfectly well, so it says which it is.
    """
    aborts = stalls = batches = 0
    previous = None
    served = dict.fromkeys(SERVED, 0)
    while True:
        try:
            report = batch(ctx, media)
        except enrich.Aborted as error:
            aborts += 1
            if aborts >= ABORTS:
                raise StageError(f"fetch: {ABORTS} consecutive {media} batches aborted (last: {error}) — "
                                 f"stopping for inspection. The checkpoint holds everything the successful "
                                 f"batches processed, so this resumes.")
            print(f"fetch: {media} batch aborted ({error}); retry #{aborts} after backoff", file=sys.stderr)
            pause(aborts * 30)
            continue
        aborts = 0
        batches += 1
        print(f"{media}: {json.dumps(report, sort_keys=True)}", flush=True)
        for name in SERVED:
            served[name] += report.get(name, 0)
        served["enterpriseOff"] = report.get("enterpriseOff") or served.get("enterpriseOff")
        remaining = report.get("remaining")
        if remaining == 0:
            announce_prose_source(media, served)
            return batches
        if remaining == previous:
            if report.get("belowFloor", 0) > 0 and report.get("count", 0) == 0:
                # Only when NOTHING was deferred: a batch of both is still waiting on an upstream, and
                # reporting it as all below the floor named the wrong cause.
                if not report.get("deferred", 0):
                    raise StageError(
                        f"fetch: every title in this {media} batch is below the vote floor, so the worklist "
                        f"cannot drain at it — below-floor ids are not checkpointed, because a vote count "
                        f"only climbs. Re-run with --vote-floor 0 to include the low-vote tail, or filter "
                        f"the worklist. This is not an upstream failure.")
            stalls += 1
            if stalls >= STALLS:
                below, deferred = report.get("belowFloor", 0), report.get("deferred", 0)
                cause = ("the ids are being attempted and deferred, which is an upstream refusing rather than "
                         "a worklist that is done")
                if below:
                    cause = (f"{deferred} of them deferred by an upstream refusing, and {below} below the vote "
                             f"floor, which never drain at it (--vote-floor 0, or filter the worklist)")
                raise StageError(f"fetch: {STALLS} {media} batches in a row finished with {remaining} "
                                 f"still pending — {cause}. Stopping.")
            pause(stalls * 60)
        else:
            stalls = 0
        previous = remaining


def check_outputs(ctx):
    """The batches and the checkpoint, where the declaration says they are.

    Both are derived from `--out-dir`, so an override that points one elsewhere does not move it — it
    splits the batches from the state that says which ids they cover, and the next run then re-enriches
    everything the missing checkpoint no longer accounts for.
    """
    for entry in (bind(e) for e in OUTPUTS):
        path = ctx.path(entry.artifact)
        if not os.path.exists(path):
            raise StageError(f"fetch: the run finished and wrote no {entry.name} at {path}. An empty "
                             f"worklist enriches nothing and leaves neither, and both are derived from "
                             f"--out-dir, so an override that names one somewhere else splits the batches "
                             f"from their checkpoint.")


def run(ctx):
    """Drain the worklists into the enriched batches. Returns the batch directory's path.

    Resumable: a re-run against the same out-dir takes only ids the checkpoint does not hold, and one that
    finds everything enriched runs a single batch, fetches nothing and exits clean.
    """
    os.makedirs(os.path.abspath(ctx.out_dir), exist_ok=True)
    ran = {media: drain(ctx, media) for media in media_types(ctx)}
    check_outputs(ctx)
    batches = ", ".join(f"{count} {media} batch(es)" for media, count in ran.items())
    return f"{ctx.path(artifacts.ENRICHED)} ({batches})"
