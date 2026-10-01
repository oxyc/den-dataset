#!/usr/bin/env python3
"""THE DAILY JOB — `./den daily`: one day of the pipeline since the live dataset,
ending at "ready to publish" (oxyc/den-dataset#27).

    ./den daily --out-dir DIR [--mode delta|catalogue|export] [--since YYYY-MM-DD]
                [--revisit-weeks N] [--spend]

It is the stages in `STAGES` order with the choices a scheduled run has to make written down once, here,
rather than in a workflow file: which stages a missing credential skips, what it refuses to buy, and what it
reports. `.github/workflows/daily.yml` runs this and nothing else, and `den_daily_test.py` runs it over the
fixture corpus, so the job CI proves is the job that is scheduled.

**What a day is.** New titles from TMDB's delta (`worklist`, TMDB's key), then the change set since the
live dataset (`changes`,
against `published/dataset.meta.json`, which the caller downloads); the stages after it over that set; and
every publish gate (`publish --plan`). Nothing is signed or uploaded: the signing key is the owner's, and
`docs/OPERATE.md` says how the result is published.

**Where it starts.** From the out-dir the live dataset was built from — or, on a machine that keeps nothing
between runs, from that dataset's `corpus-<ver>` release downloaded into `published/`, which it lays out as
the out-dir first (`pipeline/published.py`).

**What a missing credential skips**, each named in the report so a skipped stage is never a quiet one:

  * no `TMDB_API_KEY` — no delta worklist: no new titles are discovered;
  * no `--spend` or no `TYPESAFE_API_KEY` — the classify and critique passes, and the genres & moods and
    franchise asks.
    A changed title then keeps its old rows, and a new one has none; the report says which;
  * no `--spend` or no `GEMINI_API_KEY` — no fan picks are bought for added titles; the durable existing
    fan-picks input is still sanitized and carried into the next store;
  * no `DEN_EMBED_URL` — the embed pass. Any den-embed will do whose canary answers match
    (`docs/OPERATE.md`, "The alignment rule"); the scheduled job runs its own. A new title with a plot then
    has no vector and the plot-vector gate refuses.

**What it refuses**, before anything runs:

  * an out-dir that holds enriched batches and no live manifest. The change set would call every title new,
    and the stages would redo the whole corpus;
  * `--spend` with a plan that has no live baseline. A first generation is ~$20 of classification, and it is
    bought by hand (`docs/OPERATE.md`, "A first generation"), never by a timer.

**The facts stage's delta ids** (`facts-delta-ids.txt`) are written here, by the rule in `docs/OPERATE.md`,
"Wikidata": the titles the live facts file carries, and the ids the list already names, less the titles the new
labels carry. An id added to the list by hand stays until it has a vector.
"""
import argparse
import dataclasses
import datetime
import json
import os
import sys

from lib import cache as caching
from lib import llm as lib_llm

from . import artifacts, changes, classify, finalize, load, paid, plot_length, premise_daily, published, spend
from .contract import Context, StageError
from . import STAGES
from tools import fan_picks

#: The report the job leaves beside the store, for the workflow to upload and a person to read.
REPORT = "daily-report.json"
SUMMARY = "daily-report.md"

#: How far back a delta worklist looks when no `--since` is given. The delta skips what the out-dir already
#: labels, so looking back further than a day costs little and a day the job did not run is not lost.
DAYS_BACK = 7
PAID = ("classify", "critique")
TYPESAFE_PROJECTED_PER_TITLE = 0.0004
#: The stages whose ask is one step of several: they run every day, and buy only when the day can. What a day
#: that cannot buy leaves undone.
ASKS = {"genres_moods": "nothing bought; genres & moods derived from what is answered",
        "franchises": "nothing bought; franchises derived from what is answered, and a new title that needs "
                      "judgment has no franchise yet"}


def when(now=None):
    return now or datetime.datetime.now(datetime.timezone.utc)


def keys_of(path):
    with open(path, encoding="utf-8") as handle:
        blob = json.load(handle)
    return {f"{r['mediaType']}:{r['tmdbId']}" for r in blob["records"]}


def write_delta_ids(ctx, live_version):
    """`facts-delta-ids.txt` by the delta-pass rule. Returns how many ids it lists."""
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
        # The weekly run is the full vote-floor diff: an old title can cross the floor without entering a
        # release-date delta. An explicit mode remains an operator override.
        mode = args.mode or ("catalogue" if args.revisit_weeks else "delta")
        since = args.since or (now.date() - datetime.timedelta(days=DAYS_BACK)).isoformat()
        overrides = {}
        if mode in ("delta", "catalogue"):
            # Incremental lists must not overwrite a full run's universe. Catalogue is a full TMDB survey,
            # but its output is only the unpublished difference, so it has the same constraint as delta.
            directory = mode
            overrides = {"universe_movie": os.path.join(out, directory, "universe-movie.json"),
                         "universe_tv": os.path.join(out, directory, "universe-tv.json")}
        self.ctx = Context(out_dir=out, overrides=overrides, stamp_meta=os.path.join(out, "dataset.meta.json"),
                           mode=mode, since=since if mode == "delta" else "", refresh=False,
                           revisit_weeks=args.revisit_weeks, spend=False)
        self.skipped, self.ran = [], []
        enabled = lambda setting: bool(args.spend and setting is not False)
        self.can_buy = bool(enabled(getattr(args, "spend_typesafe", None)) and environ.get("TYPESAFE_API_KEY"))
        self.can_buy_fan_picks = bool(enabled(getattr(args, "spend_fan_picks", None)) and
                                      environ.get("GEMINI_API_KEY"))
        self.can_buy_premise = bool(enabled(getattr(args, "spend_premise", False)) and
                                    premise_daily.can_buy(environ) and environ.get("DEN_EMBED_URL"))
        self.fan_picks = None
        self.premise = None
        self.premise_corrections = None
        self.classifiable_changes = None
        prior = spend.month_to_date(getattr(args, "published_reports_dir", None), now.date())
        self.ledger = spend.Ledger(prior, getattr(args, "max_spend_usd_month", 10.0))
        self.typesafe_before = None
        self.paid = paid.Ledger(getattr(args, "paid_state", None))
        self.restored = 0

    def persist(self):
        """Take what was bought so far into the paid-answers ledger and write it, so a run that stops after
        this point is not paid for again by the next."""
        self.paid.keep(self.ctx)
        self.paid.save()

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


def seed_if_empty(ctx):
    """Lay the live dataset's bundle out as the out-dir when the out-dir holds nothing yet
    (`pipeline/published.py`) — the run on a machine that keeps nothing between runs."""
    enriched = ctx.path(artifacts.ENRICHED)
    bundle = os.path.join(os.path.dirname(ctx.path(artifacts.PUBLISHED_META)), published.RECORD)
    if (os.path.isdir(enriched) and os.listdir(enriched)) or not os.path.exists(bundle):
        return None
    seeded = published.seed(ctx.out_dir)
    print(f"==> seeded from the live dataset: {json.dumps(seeded, sort_keys=True)}", file=sys.stderr)
    return seeded


def update_fan_picks(day):
    """Merge fan picks for the plan's added titles before the store reads its input."""
    ctx = finalize_ctx(day.ctx)
    plan = changes.planned(day.ctx) or {}
    added = plan.get("added") or []
    asked = added if day.can_buy_fan_picks else []
    if added and not day.can_buy_fan_picks:
        why = ("not given --spend with GEMINI_API_KEY, so no new-title fan picks were bought; "
               "the existing fan-picks input is still carried forward")
        day.skip("fan_picks", why)
    existing = ctx.path(artifacts.FAN_PICKS)
    live = day.ctx.path(artifacts.PUBLISHED_META)
    if not os.path.exists(existing) and os.path.exists(live):
        try:
            with open(live, encoding="utf-8") as handle:
                store_inputs = json.load(handle).get("storeInputs") or []
        except (OSError, ValueError) as error:
            raise StageError(f"fan_picks: could not read the live manifest: {error}") from error
        if not isinstance(store_inputs, list):
            raise StageError("fan_picks: the live manifest's storeInputs is not a list")
        if any(entry.get("arg") == "fan_picks" for entry in store_inputs if isinstance(entry, dict)):
            raise StageError(f"fan_picks: the live store was built with fan picks but {existing} is missing; "
                             "refusing to build a store that drops its fan_picks sections. Republish the live "
                             "generation's corpus bundle with fan-picks.json before enabling the daily job")
    if not asked and not os.path.exists(existing):
        if not added:
            day.skip("fan_picks", "no titles were added and there is no existing fan-picks input to carry")
        return None
    day.fan_picks_kept_before = set(day.paid.answers("fan_picks"))
    try:
        result = fan_picks.daily_update(
            corpus_path=ctx.path(artifacts.CORPUS), articles_path=ctx.path(artifacts.ARTICLES),
            franchises_path=ctx.path(artifacts.FRANCHISES), existing_path=existing,
            out=existing, keys=asked, max_spend=day.args.fan_picks_max_spend_usd,
            backfill=fan_picks.load_backfill(), kept=day.paid.answers("fan_picks") if day.paid.path else None,
            persist=day.paid.save)
    except (OSError, ValueError, RuntimeError) as error:
        raise StageError(f"fan_picks: {error}") from error
    day.ran.append("fan_picks")
    print(f"==> fan_picks: {json.dumps(result, sort_keys=True)}", file=sys.stderr)
    return result


def planned_title_count(plan):
    keys = set(plan.get("added") or []) | set((plan.get("changed") or {}).keys())
    revisit = plan.get("revisit") or []
    if isinstance(revisit, dict):
        keys |= set(revisit)
    elif isinstance(revisit, list):
        keys |= set(revisit)
    return len(keys)


def reserve_known_spend(day):
    """Reserve every step whose request count is known at the change-plan boundary."""
    plan = changes.planned(day.ctx) or {}
    if day.can_buy:
        day.ledger.reserve("typesafe", planned_title_count(plan) * TYPESAFE_PROJECTED_PER_TITLE,
                           getattr(day.args, "typesafe_max_spend_usd", 1.0))
        day.typesafe_before = paid_tokens(day.ctx)
    if day.can_buy_fan_picks:
        projected = len(plan.get("added") or []) * fan_picks.PILOT_COST * fan_picks.MARGIN
        day.ledger.reserve("fanPicks", projected, day.args.fan_picks_max_spend_usd)


def update_premise(day):
    """Build the exact new/regained worklist, reserve it, generate it, and merge its strings."""
    cfg = premise_daily.config()
    if not day.can_buy_premise:
        why = (f"not given --spend with the premise switch, the {cfg['provider']} key and DEN_EMBED_URL; "
               "new titles keep no premise tags until a spending run")
        day.skip("premise_tags", why)
        return None
    if day.classifiable_changes is False:
        day.skip("premise_tags", "no newly admitted or regained title has an article to classify")
        return None
    tags_path = premise_daily.ensure_tags(day.ctx)
    premise_cap = getattr(day.args, "premise_max_spend_usd", 1.0)
    ceiling = max(1, int(premise_cap / lib_llm.price(cfg["model"])[1]))
    work, manifest = premise_daily.prepare(day.ctx, tags_path, ceiling)
    projected = premise_daily.projected(manifest, cfg)
    day.ledger.reserve("premiseTags", projected, premise_cap)
    if not manifest["titles"]:
        result = {"provider": cfg["provider"], "model": cfg["model"], "titles": 0, "generated": 0,
                  "resumed": 0, "byModel": {}, "untagged": [], "refused": [], "short": [],
                  "inputTokens": 0, "outputTokens": 0,
                  "reasoningTokens": 0, "costUSD": 0.0, "projectedSpendUSD": 0.0, "spendCapUSD": premise_cap}
    else:
        try:
            result = premise_daily.generate(os.path.join(work, "gen"), premise_cap, cfg,
                                            kept=day.paid.answers("premise_tags") if day.paid.path else None,
                                            persist=day.paid.save)
            premise_daily.merge(day.ctx, os.path.join(work, "gen"), result, day.now)
        except premise_daily.GenerationError as error:
            day.ledger.actual("premiseTags", error.cost_usd)
            raise StageError(f"premise_tags: {error}") from error
        except (OSError, ValueError, RuntimeError) as error:
            raise StageError(f"premise_tags: {error}") from error
    day.ledger.actual("premiseTags", result["costUSD"])
    day.premise = result
    day.ran.append("premise_tags")
    print(f"==> premise_tags: {json.dumps(result, sort_keys=True)}", file=sys.stderr)
    return result


def prepare(day):
    """The out-dir a day starts from: seeded from the live dataset when empty, and what earlier runs paid for
    laid back from the ledger (`pipeline/paid.py`) after the seed, so the seed cannot overwrite it."""
    ctx = day.ctx
    day.seeded = seed_if_empty(ctx)
    refuse_a_first_generation_by_accident(ctx)
    day.restored = day.paid.restore(ctx)
    if day.restored:
        print(f"==> paid answers: {day.restored} shard(s) an earlier run bought, laid back", file=sys.stderr)


def run_day(day):
    """Every stage of the day, in `STAGES` order, over a `prepare`d out-dir. Returns the check's verdict:
    `(ready, why)`."""
    ctx, env = day.ctx, day.environ
    for name in STAGES:
        if name == "worklist":
            if ctx.mode in ("delta", "catalogue") and not env.get("TMDB_API_KEY"):
                day.skip(name, "no TMDB_API_KEY, so no new titles were discovered")
                continue
            day.stage(name)
        elif name == "fetch":
            universes = [ctx.path(artifacts.UNIVERSE_MOVIE), ctx.path(artifacts.UNIVERSE_TV)]
            if all(os.path.exists(path) for path in universes):
                day.stage(name)
            else:
                day.skip(name, "no worklist to drain; existing Wikipedia sources are operator-only")
        elif name == "changes":
            day.stage(name)
            if day.args.spend and changes.planned(ctx) is None:
                raise StageError("daily: --spend with no live baseline would buy a first generation, the whole "
                                 "corpus; that is bought by hand (docs/OPERATE.md, \"A first generation\"), not by "
                                 "the daily job.")
            reserve_known_spend(day)
        elif name in PAID:
            if not day.can_buy:
                day.skip(name, "not given --spend with a TYPESAFE_API_KEY, so nothing was bought: a changed "
                               "title keeps its old rows and a new one has none")
                continue
            if name == "classify":
                day.classifiable_changes = classify.changed_articles(ctx, skip_answered=False) is not None
            if day.classifiable_changes is False:
                day.skip(name, "no newly admitted or regained title has an article to classify")
                continue
            day.stage(name, spend=True)
            day.persist()
        elif name in ASKS:
            if not day.can_buy:
                day.skip(f"{name} (ask)", ASKS[name])
            elif day.classifiable_changes is False:
                day.skip(f"{name} (ask)", "no newly admitted or regained title has a classified article")
            day.stage(name, spend=day.can_buy and day.classifiable_changes is not False)
            day.persist()
            if name == "genres_moods":
                update_premise(day)
            if name == "franchises":
                if day.can_buy and day.typesafe_before is not None:
                    used = paid_tokens(day.ctx) - day.typesafe_before
                    day.ledger.actual("typesafe", used * (0.042 / 1_000_000))
        elif name == "embed":
            if not env.get("DEN_EMBED_URL"):
                day.skip(name, "no DEN_EMBED_URL, so nothing was embedded: a new title with a plot has no "
                               "vector and the check refuses it")
                continue
            day.stage(name)
        elif name == "facts":
            count = write_delta_ids(finalize_ctx(ctx), live_version(ctx))
            print(f"==> facts: {count} delta id(s) by the delta-pass rule (docs/OPERATE.md, Wikidata)", file=sys.stderr)
            day.stage(name)
        elif name == "finalize":
            day.stage(name)
            if day.premise and day.premise["titles"]:
                day.premise["vectors"] = premise_daily.extend_vectors(day.ctx, env["DEN_EMBED_URL"])
            day.premise_corrections = correct_premise(day)
        elif name == "store":
            day.fan_picks = update_fan_picks(day)
            if day.fan_picks:
                day.ledger.actual("fanPicks", day.fan_picks.get("costUSD", 0.0))
            day.stage(name)
            premise_daily.stamp_metadata(day.ctx, day.premise)
        elif name == "publish":
            try:
                day.stage(name, plan=True)
            except StageError as refusal:
                return False, str(refusal)
            return True, "every gate passed"
        else:
            day.stage(name)
    return False, "the pipeline has no publish stage to check with"


def correct_premise(day):
    """Carry committed premise-tag corrections into the run's copy, with their vectors re-embedded.

    Tags and vectors move together or not at all: without an embedder the corrections wait for a run that
    has one, rather than shipping new strings beside vectors embedded from the old ones.
    """
    keys = premise_daily.committed_corrections(day.ctx)
    if not keys:
        return None
    url = day.environ.get("DEN_EMBED_URL")
    if not url:
        day.skip("premise_corrections", f"{len(keys)} committed premise correction(s) wait for DEN_EMBED_URL, "
                                        "which re-embeds them; the published rows stand until then")
        return {"corrected": 0, "pending": len(keys)}
    result = premise_daily.apply_corrections(day.ctx, url, keys)
    day.ran.append("premise_corrections")
    print(f"==> premise_corrections: {result['corrected']} corrected, {result['reembedded']} re-embedded",
          file=sys.stderr)
    return result


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
    if "typesafe" in day.ledger.steps:
        day.ledger.actual("typesafe", tokens * rate)
    fan_step = day.ledger.steps.get("fanPicks")
    if fan_step and fan_step.get("actualUSD") is None:
        # The update stopped before it reported: what it paid for is what it kept, in the ledger or, for a
        # run with none, in the checkpoint beside the input.
        kept = day.paid.answers("fan_picks")
        if not day.paid.path:
            checkpoint = fan_picks.daily_checkpoint_path(finalize_ctx(ctx).path(artifacts.FAN_PICKS))
            try:
                kept = fan_picks.load_daily_checkpoint(checkpoint)["responses"] if os.path.exists(checkpoint) else {}
            except (OSError, ValueError, RuntimeError):
                kept = {}
        before = getattr(day, "fan_picks_kept_before", set()) if day.paid.path else set()
        amount = sum((row.get("answer") or row.get("error") or {}).get("costUSD", 0.0)
                     for key, row in kept.items() if key not in before and isinstance(row, dict))
        day.ledger.actual("fanPicks", amount)
    boundary = None
    if plot_length.PRE_TRANSFORM_BOUNDARY in why:
        baseline = plan.get("baseline") or {}
        boundary = {
            "kind": plot_length.PRE_TRANSFORM_BOUNDARY,
            "blockingStage": "plot_length",
            "baselineDatasetVersion": baseline.get("datasetVersion") or live_version(ctx),
            "requiredMigration": ("one combined full rebuild that creates and bundles "
                                  "index/plot-length-transform-v1.json and vectors-bge-m3.raw.bin"),
            "afterMigration": "rerun the no-spend smoke before enabling DEN_DAILY_ENABLED",
        }
    out = {
        "date": day.now.date().isoformat(), "ready": ready, "verdict": why,
        "baseline": plan.get("baseline"), "datasetVersion": meta.get("datasetVersion"),
        "store": {key: meta.get(key) for key in ("storeFile", "storeSha256", "storeBytes", "storeRecords")},
        "counts": plan.get("counts"), "added": plan.get("added", []), "changed": plan.get("changed", {}),
        "withdrawn": plan.get("withdrawn", {}), "revisit": plan.get("revisit"),
        "seeded": getattr(day, "seeded", None), "ran": day.ran, "skipped": day.skipped,
        "migrationBoundary": boundary,
        "fanPicks": day.fan_picks, "premiseTags": day.premise, "premiseCorrections": day.premise_corrections,
        "spend": {**day.ledger.report(), "typesafeInputTokens": tokens,
                  "typesafeUSD": round(tokens * rate, 6)},
    }
    caching.write_atomically(os.path.join(ctx.out_dir, REPORT),
                             (json.dumps(out, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))
    lines = [f"# Daily run, {out['date']}", "",
             f"**{'Ready to publish' if ready else 'Not ready'}** — {why.splitlines()[0] if why else ''}", "",
             f"- live dataset: {(out['baseline'] or {}).get('datasetVersion') or 'none (a first generation)'}",
             f"- built: {out['datasetVersion']} ({out['store'].get('storeRecords')} rows)",
             f"- spend today: ${out['spend']['totalUSD']:.4f}; month to date: "
             f"${out['spend']['monthToDateUSD']:.4f} / ${out['spend']['monthlyCapUSD']:.2f}", ""]
    if out["fanPicks"]:
        lines += [f"- fan picks: {out['fanPicks']['asked']} asked, {out['fanPicks']['answered']} answered, "
                  f"{out['fanPicks']['emptyAnswers']} empty, "
                  f"{out['fanPicks'].get('parseErrors', 0)} malformed, "
                  f"{len(out['fanPicks'].get('notInCorpus') or [])} plan-only/not in corpus; "
                  f"${out['fanPicks']['costUSD']:.4f}", ""]
    if out["premiseTags"]:
        premise = out["premiseTags"]
        lines += [f"- premise tags: {premise['titles']} title(s), {premise['generated']} tagged "
                  f"({', '.join(f'{m} {n}' for m, n in premise['byModel'].items()) or 'none'}), "
                  f"{len(premise['untagged'])} left for a later run; ${premise['costUSD']:.4f}", ""]
    if out["premiseCorrections"]:
        fixes = out["premiseCorrections"]
        lines += [f"- premise corrections: {fixes['corrected']} row(s) corrected, "
                  f"{fixes.get('reembedded', 0)} vector(s) re-embedded"
                  + (f", {fixes['pending']} waiting for an embedder" if fixes.get("pending") else ""), ""]
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
    if boundary:
        lines += ["**Migration boundary**", "", f"- kind: `{boundary['kind']}`",
                  f"- required: {boundary['requiredMigration']}",
                  f"- after: {boundary['afterMigration']}", ""]
    caching.write_atomically(os.path.join(ctx.out_dir, SUMMARY), "\n".join(lines).encode("utf-8"))
    return out


def run(args, environ=None, now=None):
    """`./den daily`. Exit 0 ready to publish; 1 a stage refused or the check did."""
    environ = os.environ if environ is None else environ
    day = Day(args, environ, when(now))
    os.makedirs(os.path.abspath(args.out_dir), exist_ok=True)
    before = paid_tokens(day.ctx)
    try:
        prepare(day)
        # What an earlier run bought and the ledger laid back is not today's spend.
        before = paid_tokens(day.ctx)
        ready, why = run_day(day)
    except StageError as refusal:
        ready, why = False, str(refusal)
    finally:
        day.persist()
    out = report(day, ready, why, paid_tokens(day.ctx) - before)
    print(f"daily: {'ready to publish' if ready else 'NOT ready'} — {why}", file=sys.stderr)
    print(json.dumps({k: out[k] for k in ("ready", "datasetVersion", "counts", "spend")}, sort_keys=True))
    return 0 if ready else 1


def register(commands):
    """`den daily`'s arguments."""
    sub = commands.add_parser("daily", help="one day of the pipeline, ending at 'ready to publish'")
    sub.add_argument("--out-dir", default="out")
    sub.add_argument("--mode", choices=("delta", "catalogue", "export"),
                     help="the worklist: TMDB's release delta (daily), full vote-floor diff (weekly), or "
                          "the daily export dump")
    sub.add_argument("--since", help=f"the delta's window, YYYY-MM-DD (default: {DAYS_BACK} days ago)")
    sub.add_argument("--revisit-weeks", type=int, metavar="N",
                     help="also revisit this week's slice of an N-week cycle (the weekly run)")
    sub.add_argument("--spend", action="store_true",
                     help="buy the classify, critique, genres & moods, and new-title fan-picks answers "
                          "(needs the corresponding provider keys and a live baseline)")
    sub.add_argument("--spend-typesafe", action=argparse.BooleanOptionalAction, default=None)
    sub.add_argument("--spend-fan-picks", action=argparse.BooleanOptionalAction, default=None)
    sub.add_argument("--spend-premise", action=argparse.BooleanOptionalAction, default=False)
    sub.add_argument("--typesafe-max-spend-usd", type=float, default=1.0, metavar="USD")
    sub.add_argument("--fan-picks-max-spend-usd", type=float, default=fan_picks.DAILY_SPEND_CAP, metavar="USD",
                     help=f"projected per-run cap for new-title Gemini calls (default: "
                          f"${fan_picks.DAILY_SPEND_CAP:.2f}); the whole set must fit before any call")
    sub.add_argument("--premise-max-spend-usd", type=float, default=1.0, metavar="USD")
    sub.add_argument("--max-spend-usd-month", type=float, default=10.0, metavar="USD")
    sub.add_argument("--published-reports-dir",
                     help="downloaded public daily-report JSON assets used for the monthly spend total")
    sub.add_argument("--paid-state", metavar="FILE",
                     help="the paid-answers ledger (pipeline/paid.py): read before the day, written as it buys; "
                          "without it a run resumes only within its own out-dir")
    return sub
