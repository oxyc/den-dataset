#!/usr/bin/env python3
"""THE DAILY JOB — `./den daily`: one day of the pipeline over the out-dir the live dataset was built from,
ending at "ready to publish" (oxyc/den-dataset#27).

    ./den daily --out-dir DIR [--mode delta|export] [--since YYYY-MM-DD] [--revisit-weeks N] [--spend]

It is the stages in `STAGES` order with the choices a scheduled run has to make written down once, here,
rather than in a workflow file: which stages a missing credential skips, what it refuses to buy, and what it
reports. `.github/workflows/daily.yml` runs this and nothing else, and `den_daily_test.py` runs it over the
fixture corpus, so the job CI proves is the job that is scheduled.

**What a day is.** New titles from TMDB's delta (`worklist`, TMDB's key), enriched with every grounded
title's changed article re-read (`fetch --refresh`); the change set since the live dataset (`changes`,
against `published/dataset.meta.json`, which the caller downloads); the stages after it over that set; and
every publish gate (`publish --plan`). Nothing is signed or uploaded: the signing key is the owner's, and
`docs/OPERATE.md` says how the result is published.

**What a missing credential skips**, each named in the report so a skipped stage is never a quiet one:

  * no `TMDB_API_KEY` — no delta worklist: no new titles are discovered; the refresh still runs;
  * no `--spend` or no `TYPESAFE_API_KEY` — the classify and critique passes, and the genres & moods ask.
    A changed title then keeps its old rows, and a new one has none; the report says which;
  * no `DEN_EMBED_URL` — the embed pass. The vectors must come from the den-embed that serves the queries
    (`docs/OPERATE.md`, "The alignment rule"), so a job that cannot reach it embeds nothing rather than
    embedding somewhere else. A new title with a plot then has no vector and the plot-vector gate refuses.

**What it refuses**, before anything runs:

  * an out-dir that holds enriched batches and no live manifest. The change set would call every title new,
    and the stages would redo the whole corpus;
  * `--spend` with a plan that has no live baseline. A first generation is ~$20 of classification, and it is
    bought by hand (`docs/OPERATE.md`, 3a), never by a timer.

**The facts stage's delta ids** (`facts-delta-ids.txt`) are written here, by `docs/OPERATE.md` step 6a's
rule: the titles the live facts file carries, and the ids the list already names, less the titles the new
labels carry. An id added to the list by hand stays until it has a vector.
"""
import dataclasses
import datetime
import json
import os
import sys

from lib import cache as caching

from . import artifacts, changes, finalize, load, refresh
from .contract import Context, StageError
from . import STAGES

#: The report the job leaves beside the store, for the workflow to upload and a person to read.
REPORT = "daily-report.json"
SUMMARY = "daily-report.md"

#: How far back a delta worklist looks when no `--since` is given. The delta skips what the out-dir already
#: labels, so looking back further than a day costs little and a day the job did not run is not lost.
DAYS_BACK = 7

PAID = ("classify", "critique")


def when(now=None):
    return now or datetime.datetime.now(datetime.timezone.utc)


def keys_of(path):
    with open(path, encoding="utf-8") as handle:
        blob = json.load(handle)
    return {f"{r['mediaType']}:{r['tmdbId']}" for r in blob["records"]}


def write_delta_ids(ctx, live_version):
    """`facts-delta-ids.txt` by step 6a's rule. Returns how many ids it lists."""
    labels = keys_of(ctx.path(artifacts.VECTOR_LABELS))
    listed = set()
    path = ctx.path(artifacts.DELTA_IDS)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            listed = {token for token in handle.read().replace(",", " ").split() if token}
    if live_version:
        live = os.path.join(ctx.out_dir, artifacts.FACTS.filename.format(version=live_version))
        if not os.path.exists(live):
            raise StageError(f"daily: {live} is not here, and the delta pass is every title it carries that the "
                             f"new labels do not. Without it the facts would lose them — the rebuild that "
                             f"dropped 137 titles. This out-dir did not build the live dataset.")
        listed |= keys_of(live)
    ids = sorted(listed - labels, key=changes.order)
    caching.write_atomically(path, "".join(f"{key}\n" for key in ids).encode("utf-8"))
    return len(ids)


def paid_tokens(ctx):
    """Input tokens across every paid shard in the out-dir — the provider bills input only
    (`lib/typesafe_client.py`). Taken before and after the paid stages; the difference is the day's."""
    total = 0
    for artifact in (artifacts.COMBINED, artifacts.DELTA, artifacts.GENRES_MOODS_ANSWERS):
        for shard in ctx.paths(artifact):
            with open(shard, encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        total += sum(call.get("inputTokens") or 0 for call in json.loads(line).get("calls") or ())
    return total


class Day:
    """One run: the context each stage gets, and what the report will say."""

    def __init__(self, args, environ, now):
        self.args, self.environ, self.now = args, environ, now
        out = args.out_dir
        mode = args.mode or "delta"
        since = args.since or (now.date() - datetime.timedelta(days=DAYS_BACK)).isoformat()
        overrides = {}
        if mode == "delta":
            # A delta's lists live under delta/: written over the full ones, they END the enrich run.
            overrides = {"universe_movie": os.path.join(out, "delta", "universe-movie.json"),
                         "universe_tv": os.path.join(out, "delta", "universe-tv.json")}
        self.ctx = Context(out_dir=out, overrides=overrides, stamp_meta=os.path.join(out, "dataset.meta.json"),
                           mode=mode, since=since if mode == "delta" else "", refresh=True,
                           revisit_weeks=args.revisit_weeks, spend=False)
        self.skipped, self.ran = [], []
        self.can_buy = bool(args.spend and environ.get("TYPESAFE_API_KEY"))

    def skip(self, stage, why):
        self.skipped.append({"stage": stage, "why": why})
        print(f"==> {stage}: SKIPPED — {why}", file=sys.stderr)

    def stage(self, name, **changed):
        module = load(name)
        ctx = dataclasses.replace(self.ctx, **changed)
        print(f"==> {name}", file=sys.stderr)
        made = module.run(finalize.versioned(module, ctx))
        print(f"==> {name}: {made}", file=sys.stderr)
        self.ran.append(name)
        return made


def refuse_a_first_generation_by_accident(ctx):
    enriched = ctx.path(artifacts.ENRICHED)
    if os.path.isdir(enriched) and os.listdir(enriched) and not os.path.exists(ctx.path(artifacts.PUBLISHED_META)):
        raise StageError(f"daily: {enriched} holds batches and {ctx.path(artifacts.PUBLISHED_META)} is not here, "
                         f"so every title would be new and every stage would redo the corpus. Download the "
                         f"live manifest first: {artifacts.PUBLISHED_META.how.replace('<out-dir>', ctx.out_dir)}")


def live_version(ctx):
    path = ctx.path(artifacts.PUBLISHED_META)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle).get("datasetVersion")


def run_day(day):
    """Every stage of the day, in `STAGES` order. Returns the check's verdict: `(ready, why)`."""
    ctx, env = day.ctx, day.environ
    refuse_a_first_generation_by_accident(ctx)
    for name in STAGES:
        if name == "worklist":
            if ctx.mode == "delta" and not env.get("TMDB_API_KEY"):
                day.skip(name, "no TMDB_API_KEY, so no new titles were discovered")
                continue
            day.stage(name)
        elif name == "fetch":
            universes = [ctx.path(artifacts.UNIVERSE_MOVIE), ctx.path(artifacts.UNIVERSE_TV)]
            if all(os.path.exists(path) for path in universes):
                day.stage(name)
            else:
                # Nothing to drain: the refresh alone, which is the fetch stage's own rule for it.
                day.skip(name, "no worklist to drain; every grounded title's article was still re-read")
                print(json.dumps(refresh.run(ctx), sort_keys=True), file=sys.stderr)
        elif name == "changes":
            day.stage(name)
            if day.args.spend and changes.planned(ctx) is None:
                raise StageError("daily: --spend with no live baseline would buy a first generation, the whole "
                                 "corpus; that is bought by hand (docs/OPERATE.md, 3a), not by the daily job.")
        elif name in PAID:
            if not day.can_buy:
                day.skip(name, "not given --spend with a TYPESAFE_API_KEY, so nothing was bought: a changed "
                               "title keeps its old rows and a new one has none")
                continue
            day.stage(name, spend=True)
        elif name == "genres_moods":
            if not day.can_buy:
                day.skip("genres_moods (ask)", "nothing bought; genres & moods derived from what is answered")
            day.stage(name, spend=day.can_buy)
        elif name == "embed":
            if not env.get("DEN_EMBED_URL"):
                day.skip(name, "no DEN_EMBED_URL: vectors come from the den-embed that serves queries or not "
                               "at all, so a new title with a plot has no vector and the check refuses it")
                continue
            day.stage(name)
        elif name == "facts":
            count = write_delta_ids(finalize_ctx(ctx), live_version(ctx))
            print(f"==> facts: {count} delta id(s) by docs/OPERATE.md 6a's rule", file=sys.stderr)
            day.stage(name)
        elif name == "publish":
            try:
                day.stage(name, plan=True)
            except StageError as refusal:
                return False, str(refusal)
            return True, "every gate passed"
        else:
            day.stage(name)
    return False, "the pipeline has no publish stage to check with"


def finalize_ctx(ctx):
    """`ctx` under the version `finalize` derived — the facts stage's files are named by it."""
    return dataclasses.replace(ctx, dataset_version=finalize.manifest_version(ctx))


def report(day, ready, why, tokens):
    """`daily-report.json` and `.md` in the out-dir: what moved, what ran and did not, and what it cost."""
    ctx = day.ctx
    plan_path = os.path.join(ctx.path(artifacts.CHANGES), "plan.json")
    plan = {}
    if os.path.exists(plan_path):
        with open(plan_path, encoding="utf-8") as handle:
            plan = json.load(handle)
    meta = {}
    if os.path.exists(ctx.stamp_meta):
        with open(ctx.stamp_meta, encoding="utf-8") as handle:
            meta = json.load(handle)
    rate = 0.042 / 1_000_000  # lib/typesafe_client.TypeSafe.RATE_PER_INPUT_TOKEN, not imported: it is a client
    out = {
        "date": day.now.date().isoformat(), "ready": ready, "verdict": why,
        "baseline": plan.get("baseline"), "datasetVersion": meta.get("datasetVersion"),
        "store": {key: meta.get(key) for key in ("storeFile", "storeSha256", "storeBytes", "storeRecords")},
        "counts": plan.get("counts"), "added": plan.get("added", []), "changed": plan.get("changed", {}),
        "withdrawn": plan.get("withdrawn", {}), "revisit": plan.get("revisit"),
        "ran": day.ran, "skipped": day.skipped,
        "spend": {"inputTokens": tokens, "usd": round(tokens * rate, 4)},
    }
    caching.write_atomically(os.path.join(ctx.out_dir, REPORT),
                             (json.dumps(out, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))
    lines = [f"# Daily run, {out['date']}", "",
             f"**{'Ready to publish' if ready else 'Not ready'}** — {why.splitlines()[0] if why else ''}", "",
             f"- live dataset: {(out['baseline'] or {}).get('datasetVersion') or 'none (a first generation)'}",
             f"- built: {out['datasetVersion']} ({out['store'].get('storeRecords')} rows)",
             f"- spend: ${out['spend']['usd']:.2f} ({tokens:,} input tokens)", ""]
    counts = out["counts"] or {}
    lines += [f"| added | changed | withdrawn | revised | revisited |", "|---|---|---|---|---|",
              f"| {counts.get('added', 0)} | {counts.get('changed', 0)} | {counts.get('withdrawn', 0)} | "
              f"{counts.get('revised', 0)} | {counts.get('revisit', 0)} |", ""]
    for title, keys in (("Added", out["added"]), ("Changed", [f"{k} ({', '.join(v)})" for k, v in out["changed"].items()]),
                        ("Withdrawn", list(out["withdrawn"]))):
        if keys:
            lines += [f"**{title}** ({len(keys)}): " + ", ".join(keys[:50]) + (" …" if len(keys) > 50 else ""), ""]
    if day.skipped:
        lines += ["**Skipped**", ""] + [f"- `{s['stage']}` — {s['why']}" for s in day.skipped] + [""]
    caching.write_atomically(os.path.join(ctx.out_dir, SUMMARY), "\n".join(lines).encode("utf-8"))
    return out


def run(args, environ=None, now=None):
    """`./den daily`. Exit 0 ready to publish; 1 a stage refused or the check did."""
    environ = os.environ if environ is None else environ
    day = Day(args, environ, when(now))
    os.makedirs(os.path.abspath(args.out_dir), exist_ok=True)
    before = paid_tokens(day.ctx)
    try:
        ready, why = run_day(day)
    except StageError as refusal:
        ready, why = False, str(refusal)
    out = report(day, ready, why, paid_tokens(day.ctx) - before)
    print(f"daily: {'ready to publish' if ready else 'NOT ready'} — {why}", file=sys.stderr)
    print(json.dumps({k: out[k] for k in ("ready", "datasetVersion", "counts", "spend")}, sort_keys=True))
    return 0 if ready else 1


def register(commands):
    """`den daily`'s arguments."""
    sub = commands.add_parser("daily", help="one day of the pipeline, ending at 'ready to publish'")
    sub.add_argument("--out-dir", default="out")
    sub.add_argument("--mode", choices=("delta", "export"),
                     help="the worklist: TMDB's delta since --since (default), or the daily export dump")
    sub.add_argument("--since", help=f"the delta's window, YYYY-MM-DD (default: {DAYS_BACK} days ago)")
    sub.add_argument("--revisit-weeks", type=int, metavar="N",
                     help="also revisit this week's slice of an N-week cycle (the weekly run)")
    sub.add_argument("--spend", action="store_true",
                     help="buy the classify, critique and genres & moods answers for what moved "
                          "(needs TYPESAFE_API_KEY and a live baseline)")
    return sub
