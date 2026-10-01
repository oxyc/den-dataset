"""The daily job's two steps that ask a text model through `lib/llm.py` — fan picks and premise tags — on the
cadence `data/models.json` gives each (oxyc/den-dataset#187 item 3).

**Daily** (`"cadence": "daily"`, online): the day's new titles are asked that day, as they always were.

**Weekly** (`"cadence": "weekly", "day": "mon"`, Batch, half price): on its day the step submits one Batch job
for every title that still waits for it, and any later run collects the job. Due is worked out from the date
and the last submit the paid-answers ledger records (`pipeline/paid.py`), not from which cron fired, so a
failed Monday is caught up on Tuesday. A job that expired or failed is finished online and never resubmitted,
and what a job refused goes to the step's fallback, both through `lib/llm.collect`. A title waiting is
published without that section, which atlas already reads as "not yet": no fan-pick row, and a premise score
at the pool floor.

Which titles wait:
  * fan picks — every title of the published corpus or the change set's added that has no anchor;
  * premise tags — every title a kept classify shard holds that has no tags, less those the worklist settled
    as untaggable (not a screen work, not narrative, no story-premise section, or an article changed since).

Their articles are dumped again each day they wait (`changes/waiting.txt`): a prompt needs the lead or the
premise sections, and no out-dir keeps an article between runs.
"""
import dataclasses
import datetime
import gzip
import json
import os
import sys

from lib import cache as caching
from lib import llm as lib_llm

from . import artifacts, changes, finalize, premise_daily
from .contract import StageError
from tools import fan_picks

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
REPORT_KEYS = {"fan_picks": "fanPicks", "premise_tags": "premiseTags"}


def finalize_ctx(ctx):
    """`ctx` under the version `finalize` derived — the facts stage's files are named by it."""
    return dataclasses.replace(ctx, dataset_version=finalize.manifest_version(ctx))


def weekly(cfg):
    return cfg.get("cadence") == "weekly"


def due(cfg, today, last):
    """Whether a step is due on `today`: always when daily; when weekly, once its day has come since `last`."""
    if not weekly(cfg):
        return True
    latest = today - datetime.timedelta(days=(today.weekday() - DAYS.index(cfg["day"])) % 7)
    return last is None or last < latest.isoformat()


def state(day, step):
    return day.paid.data["steps"].setdefault(step, {})


def jobs(day, step):
    return [job for job in day.paid.data["batches"] if job["step"] == step]


def job_keys(job):
    return [key for keys in job["chunks"].values() for key in keys]


def pending_keys(day, step):
    return {key for job in jobs(day, step) for key in job_keys(job)}


def status(day, step, cfg):
    """What the report says about a step, filled in as it runs."""
    return day.models.setdefault(step, {
        "cadence": cfg.get("cadence", "daily"), "day": cfg.get("day"), "mode": cfg["mode"],
        "provider": cfg["provider"], "model": cfg["model"], "due": None, "waiting": 0, "pending": [],
        "collected": 0, "expired": [], "submitted": None, "costUSD": 0.0})


def age(job, now):
    submitted = datetime.datetime.fromisoformat(job["submittedAt"])
    return round((now - submitted).total_seconds() / 3600, 1)


def keys_of_corpus(path):
    if not os.path.exists(path):
        return set()
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return {json.loads(line)["key"] for line in fh if line.strip()}


# --- which titles wait -------------------------------------------------------------------------------------

def write_waiting(day):
    """After the change set: each weekly step's waiting titles, and `changes/waiting.txt` — those and the ones
    in a job not collected yet — for the articles stage to dump."""
    ctx, plan = day.ctx, changes.planned(day.ctx)
    day.waiting = {}
    if plan is None:
        return day.waiting
    fan_cfg, premise_cfg = fan_picks.CFG, premise_daily.config()
    if weekly(fan_cfg) and day.can_buy_fan_picks and os.path.exists(ctx.path(artifacts.FAN_PICKS)):
        with open(ctx.path(artifacts.FAN_PICKS), encoding="utf-8") as fh:
            anchored = set(json.load(fh).get("anchors") or {})
        titles = keys_of_corpus(ctx.path(artifacts.PUBLISHED_CORPUS)) | set(plan.get("added") or [])
        day.waiting["fan_picks"] = sorted(titles - anchored - set(plan.get("withdrawn") or {}))
    if weekly(premise_cfg) and day.can_buy_premise:
        with open(premise_daily.ensure_tags(ctx), encoding="utf-8") as fh:
            tagged = set(json.load(fh).get("tags") or {})
        classified = set(changes.listed(ctx, "new"))
        for name, shard in day.paid.data["shards"].items():
            if name.startswith("combined-"):
                classified |= {f"{row['mediaType']}:{row['tmdbId']}"
                               for row in map(json.loads, filter(str.strip, shard["rows"].splitlines()))}
        settled = set(state(day, "premise_tags").get("settled") or [])
        day.waiting["premise_tags"] = sorted(classified - tagged - settled)
    listed = set().union(*day.waiting.values(), *(pending_keys(day, step) for step in REPORT_KEYS))
    caching.write_atomically(os.path.join(ctx.path(artifacts.CHANGES), "waiting.txt"),
                             "".join(f"{key}\n" for key in sorted(listed)).encode("utf-8"))
    return day.waiting


# --- fan picks ---------------------------------------------------------------------------------------------

def fan_picks_step(day):
    """Fan picks before the store reads its input: daily online, or the weekly Batch."""
    cfg = fan_picks.CFG
    report = status(day, "fan_picks", cfg)
    if not weekly(cfg):
        report["due"] = True
        return update_fan_picks(day)
    ctx = finalize_ctx(day.ctx)
    existing = refuse_a_lost_input(day, ctx)
    if not os.path.exists(existing):
        day.skip("fan_picks", "there is no existing fan-picks input to carry")
        return None
    kept = day.paid.answers("fan_picks")
    asked = []
    if not day.paid.path:
        day.skip("fan_picks", "a weekly Batch step needs the paid-answers ledger (--paid-state) to find its job "
                              "again; the existing input is carried")
    elif not day.can_buy_fan_picks:
        day.skip("fan_picks", "not given --spend with the fan-picks provider's key; the existing input is carried")
    else:
        rows = fan_picks.corpus_rows(ctx.path(artifacts.CORPUS))
        waiting = [key for key in day.waiting.get("fan_picks", []) if key in rows]
        pending = pending_keys(day, "fan_picks")
        titles = fan_picks.titles_for(sorted(set(waiting) | {key for key in pending if key in rows}), rows,
                                      ctx.path(artifacts.ARTICLES))
        task, budget = fan_picks.Task(cfg), lib_llm.Budget(day.args.fan_picks_max_spend_usd)

        def record(key, row):
            if "value" in row:
                answer = {**row["value"], "key": key, "costUSD": 0.0, "mode": row["by"]["mode"],
                          "provider": row["by"]["provider"], "model": row["by"]["model"]}
                kept[key] = {"requestSha256": fan_picks.fingerprint(titles[key]), "accepted": True,
                             "answer": answer, "error": None}
            elif row.get("refused"):
                kept[key] = {"requestSha256": fan_picks.fingerprint(titles[key]), "accepted": True,
                             "answer": None, "error": {"key": key, "usage": {}, "text": "", "refused": True,
                                                       "costUSD": 0.0}}
            day.paid.save()
        spent = collect(day, "fan_picks", cfg, task, titles, budget, record)
        report["due"] = due(cfg, day.now.date(), state(day, "fan_picks").get("lastSubmit"))
        ready = [key for key in titles if fan_picks.reusable(kept.get(key), fan_picks.fingerprint(titles[key]))]
        ask = [titles[key] for key in waiting if key not in ready and key not in pending_keys(day, "fan_picks")]
        report["waiting"] = len(ask)
        if report["due"]:
            projected = len(ask) * fan_picks.PILOT_COST * fan_picks.MARGIN * lib_llm.BATCH_FACTOR
            day.ledger.reserve("fanPicks", projected, day.args.fan_picks_max_spend_usd)
            submit(day, "fan_picks", cfg, task, ask)
        day.ledger.actual("fanPicks", spent)
        report["costUSD"] = round(spent, 6)
        asked = ready
    try:
        result = fan_picks.daily_update(
            corpus_path=ctx.path(artifacts.CORPUS), articles_path=ctx.path(artifacts.ARTICLES),
            franchises_path=ctx.path(artifacts.FRANCHISES), existing_path=existing, out=existing, keys=asked,
            max_spend=day.args.fan_picks_max_spend_usd, backfill=fan_picks.load_backfill(), kept=kept,
            persist=day.paid.save)
    except (OSError, ValueError, RuntimeError) as error:
        raise StageError(f"fan_picks: {error}") from error
    day.ran.append("fan_picks")
    print(f"==> fan_picks: {json.dumps(result, sort_keys=True)}", file=sys.stderr)
    return result


def refuse_a_lost_input(day, ctx):
    """The durable fan-picks input's path, refusing a run whose live store had fan picks and whose input is
    gone: a store built without it would drop all three sections."""
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
    return existing


def update_fan_picks(day):
    """The daily cadence: merge fan picks for the plan's added titles, asked online, before the store reads its
    input."""
    ctx = finalize_ctx(day.ctx)
    plan = changes.planned(day.ctx) or {}
    added = plan.get("added") or []
    asked = added if day.can_buy_fan_picks else []
    if added and not day.can_buy_fan_picks:
        why = ("not given --spend with GEMINI_API_KEY, so no new-title fan picks were bought; "
               "the existing fan-picks input is still carried forward")
        day.skip("fan_picks", why)
    existing = refuse_a_lost_input(day, ctx)
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


# --- premise tags ------------------------------------------------------------------------------------------

def premise_step(day):
    """Premise tags after the genres & moods: daily online, or the weekly Batch."""
    cfg = premise_daily.config()
    report = status(day, "premise_tags", cfg)
    if not weekly(cfg):
        report["due"] = True
        return update_premise(day)
    if not day.paid.path:
        day.skip("premise_tags", "a weekly Batch step needs the paid-answers ledger (--paid-state) to find its "
                                 "job again; titles wait for a run with one")
        return None
    if not day.can_buy_premise:
        day.skip("premise_tags", f"not given --spend with the premise switch, the {cfg['provider']} key and "
                                 "DEN_EMBED_URL; titles wait for a spending run")
        return None
    st, pending = state(day, "premise_tags"), pending_keys(day, "premise_tags")
    waiting = set(day.waiting.get("premise_tags", []))
    report["due"] = due(cfg, day.now.date(), st.get("lastSubmit"))
    if not pending and not (report["due"] and waiting):
        report["waiting"] = len(waiting)
        return None
    cap = getattr(day.args, "premise_max_spend_usd", 1.0)
    tags_path = premise_daily.ensure_tags(day.ctx)
    work = os.path.join(day.ctx.out_dir, "premise-increment", "weekly")
    plan_path = os.path.join(work, "plan.json")
    os.makedirs(work, exist_ok=True)
    with open(plan_path, "w", encoding="utf-8") as fh:
        json.dump({"baseline": changes.planned(day.ctx)["baseline"], "added": sorted(waiting | pending),
                   "changed": {}}, fh)
    ceiling = max(1, int(cap / lib_llm.price(cfg["model"])[1]))
    work, manifest = premise_daily.prepare(day.ctx, tags_path, ceiling, plan=plan_path, work=work)
    rows = premise_daily.rows_of(os.path.join(work, "gen"))
    with open(day.ctx.path(artifacts.ARTICLES), encoding="utf-8") as fh:
        fetched = {f"{a['mediaType']}:{a['tmdbId']}" for a in map(json.loads, fh)}
    # A waiting title the worklist turned away with its article in hand is not one it will ever take: not a
    # screen work, not narrative, no story-premise section, or an article edited since it was classified.
    st["settled"] = sorted(set(st.get("settled") or []) | ((waiting & fetched) - set(rows) - pending))
    task, kept = premise_daily.Task(cfg), day.paid.answers("premise_tags")
    answered = {key: {"tags": kept[key]["tags"], "by": kept[key]["by"]} for key, row in rows.items()
                if (kept.get(key) or {}).get("ask") == premise_daily.asked_for(cfg, task, row)}

    def record(key, row):
        if "value" in row and key in rows:
            kept[key] = {"ask": premise_daily.asked_for(cfg, task, rows[key]), "tags": row["value"], "by": row["by"]}
            answered[key] = {"tags": row["value"], "by": row["by"]}
            day.paid.save()
    spent = collect(day, "premise_tags", cfg, task, rows, lib_llm.Budget(cap), record)
    ask = [row for key, row in rows.items() if key not in answered and key not in pending_keys(day, "premise_tags")]
    report["waiting"] = len(ask)
    if report["due"]:
        # The worklist's own estimate, over every title it holds: an upper bound on what is submitted.
        day.ledger.reserve("premiseTags", premise_daily.projected(manifest, cfg), cap)
        submit(day, "premise_tags", cfg, task, ask)
    day.ledger.actual("premiseTags", spent)
    report["costUSD"] = round(spent, 6)
    if not answered:
        return None
    collected = os.path.join(day.ctx.out_dir, "premise-increment", "collected")
    premise_daily.write_collected(collected, answered)
    models = {}
    for row in answered.values():
        models[row["by"]["model"]] = models.get(row["by"]["model"], 0) + 1
    result = {"provider": cfg["provider"], "model": cfg["model"], "mode": "batch", "titles": len(answered),
              "generated": len(answered), "resumed": 0, "byModel": dict(sorted(models.items())), "untagged": [],
              "inputTokens": 0, "outputTokens": 0, "reasoningTokens": 0, "costUSD": round(spent, 6),
              "projectedSpendUSD": 0.0, "spendCapUSD": cap}
    try:
        premise_daily.merge(day.ctx, collected, result, day.now)
    except (OSError, ValueError, RuntimeError) as error:
        raise StageError(f"premise_tags: {error}") from error
    day.premise = result
    day.ran.append("premise_tags")
    print(f"==> premise_tags: {json.dumps(result, sort_keys=True)}", file=sys.stderr)
    return result


def update_premise(day):
    """The daily cadence: build the exact new/regained worklist, reserve it, generate it online, and merge
    its strings."""
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
                  "resumed": 0, "byModel": {}, "untagged": [], "inputTokens": 0, "outputTokens": 0,
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


# --- Batch jobs --------------------------------------------------------------------------------------------

def collect(day, step, cfg, task, items_by_key, budget, record):
    """Poll the step's jobs; take each finished one's answers through `record` and drop it from the ledger.
    Returns what collecting cost (the job at Batch price, and whatever was finished online)."""
    report, spent = day.models[step], 0.0
    for job in jobs(day, step):
        items = [items_by_key[key] for key in job_keys(job) if key in items_by_key]
        try:
            outcome, _rows, calls = lib_llm.collect(cfg, job, task, items, budget, record=record)
        except (lib_llm.OverBudget, lib_llm.providers.Unavailable) as error:
            raise StageError(f"{step}: collecting {job['label']}: {error}") from error
        if outcome == "running":
            report["pending"].append({"label": job["label"], "submittedAt": job["submittedAt"],
                                      "ageHours": age(job, day.now), "titles": len(job_keys(job))})
            continue
        if outcome in ("expired", "failed"):
            report["expired"].append(job["label"])
        report["collected"] += len(items)
        spent += sum(call["costUSD"] for call in calls)
        day.paid.data["batches"].remove(job)
        day.paid.save()
    return spent


def submit(day, step, cfg, task, items):
    """One Batch job for `items`, recorded in the ledger before anything else can fail."""
    st = state(day, step)
    today = day.now.date().isoformat()
    if items:
        label = f"{step.replace('_', '-')}-{today}"
        try:
            job = lib_llm.submit(cfg, task, items, label, os.path.join(day.ctx.out_dir, "batches"))
        except lib_llm.providers.Unavailable as error:
            raise StageError(f"{step}: submitting {label}: {error}") from error
        job["submittedAt"] = day.now.isoformat(timespec="seconds")
        day.paid.data["batches"].append(job)
        day.models[step]["submitted"] = {"label": label, "titles": len(items)}
        day.models[step]["pending"].append({"label": label, "submittedAt": job["submittedAt"], "ageHours": 0.0,
                                            "titles": len(items)})
    st["lastSubmit"] = today
    day.paid.save()
