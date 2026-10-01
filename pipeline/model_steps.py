"""The daily job's two steps that ask a text model through `lib/llm.py` — fan picks and premise tags — on the
cadence `data/models.json` gives each (oxyc/den-dataset#187 item 3).

**Daily** (`"cadence": "daily"`, online): the day's new titles are asked that day, as they always were.

**Weekly** (`"cadence": "weekly", "day": "mon"`, Batch, half price): on its day the step submits one Batch job
for every title that still waits for it, and any later run collects the job. Due is worked out from the date
and the last submit the paid-answers ledger records (`pipeline/paid.py`), not from which cron fired, so a
failed Monday is caught up on Tuesday. A title waiting is published without that section, which atlas already
reads as "not yet": no fan-pick row, and a premise score at the pool floor.

What the job costs is held to the caps:
  * a submit is sized to what the step's daily cap and the month have left (`spend.Ledger.allowance`); what
    does not fit waits, and with nothing affordable the step stays due for the next run instead of failing
    the day. A submitted job's projection is committed spend until it is collected (`spend.Ledger.pending`);
  * a submit is recorded as an intent before it is sent, so a run that dies before it sees the job finds it
    by its label (`reconcile`) instead of buying the titles again;
  * collecting reads whatever the job answered, spend or no spend. What it refused, could not parse or never
    answered — every title of a job that expired, failed or has run past `STALE_HOURS` — is finished online
    (with the step's fallback) inside today's allowance, and across as many runs as that takes: a title kept
    as answered is never asked again. A job is never resubmitted.

Which titles wait:
  * fan picks — every title of the published corpus or the change set's added that has no anchor;
  * premise tags — every title a kept classify shard holds that has no tags, less those the worklist turned
    away for good on that classification (not a screen work, not narrative, no story-premise section). A
    title whose article changed since it was classified, or since it was submitted, waits: it is never
    settled, an answer bought for the old article is kept but not used, and classify asks it again on the
    new article (`changes/reclassify.txt`), after which the next submit asks for its tags.

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
#: The most waiting titles one run builds a premise worklist for: ~$0.15 at gpt-5.6-luna's Batch price, well
#: inside the step's cap, so a backlog is worked off over several runs instead of refused.
PREMISE_LIMIT = 400
#: A job still reported running this long after it was submitted is taken as expired and finished online.
#: Every provider's window is 24 hours.
STALE_HOURS = 72
#: A job with titles still owed this long after it was submitted, as through a stretch of runs that may not
#: spend, is let go with what it answered kept: its other titles wait again and a later submit asks them.
GIVE_UP_DAYS = 14
#: What the worklist turns a title away for that holds while its classification does.
SETTLES = ("badValidity", "nonNarrative", "review")


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


def intents(day, step):
    return [intent for intent in day.paid.data["intents"] if intent["step"] == step]


def job_keys(job):
    return [key for keys in job["chunks"].values() for key in keys]


def pending_keys(day, step):
    """Every title a job or an unconfirmed submit of the step holds."""
    return {key for job in jobs(day, step) + intents(day, step) for key in job_keys(job)}


def status(day, step, cfg):
    """What the report says about a step, filled in as it runs."""
    return day.models.setdefault(step, {
        "cadence": cfg.get("cadence", "daily"), "day": cfg.get("day"), "mode": cfg["mode"],
        "provider": cfg["provider"], "model": cfg["model"], "due": None, "waiting": 0, "deferred": 0,
        "pending": [], "collected": 0, "expired": [], "submitted": None, "costUSD": 0.0})


def age(job, now):
    submitted = datetime.datetime.fromisoformat(job["submittedAt"])
    return round((now - submitted).total_seconds() / 3600, 1)


def keys_of_corpus(path):
    if not os.path.exists(path):
        return set()
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return {json.loads(line)["key"] for line in fh if line.strip()}


def charge(day, step, amount):
    """Count `amount` measured today against the step."""
    name = REPORT_KEYS[step]
    day.ledger.actual(name, day.ledger.current(name) + amount)
    day.models[step]["costUSD"] = round(day.models[step]["costUSD"] + amount, 6)


def room(day, step, cap, collecting=0.0):
    """What the step may still spend today, with `collecting` (a job's projection) set aside for its cost."""
    name = REPORT_KEYS[step]
    return max(0.0, day.ledger.allowance(name, cap, collecting) - day.ledger.current(name) - collecting)


# --- which titles wait -------------------------------------------------------------------------------------

def classified(day):
    """`{key: articleSha256}` for every title a kept classify shard holds, the newest run's row winning."""
    shards = [shard for name, shard in day.paid.data["shards"].items() if name.startswith("combined-")]
    shards.sort(key=lambda shard: json.loads(shard["manifest"]).get("runStartedAt") or "")
    return {f"{row['mediaType']}:{row['tmdbId']}": row.get("articleSha256")
            for shard in shards for row in map(json.loads, filter(str.strip, shard["rows"].splitlines()))}


def write_waiting(day):
    """After the change set: each weekly step's waiting titles, and `changes/waiting.txt` — those and the ones
    in a job not collected yet — for the articles stage to dump."""
    ctx, plan = day.ctx, changes.planned(day.ctx)
    day.waiting, day.reasks = {}, {}
    if plan is None:
        return day.waiting
    fan_cfg, premise_cfg = fan_picks.CFG, premise_daily.config()
    if weekly(fan_cfg) and day.can_buy_fan_picks and os.path.exists(ctx.path(artifacts.FAN_PICKS)):
        with open(ctx.path(artifacts.FAN_PICKS), encoding="utf-8") as fh:
            anchored = set(json.load(fh).get("anchors") or {})
        titles = keys_of_corpus(ctx.path(artifacts.PUBLISHED_CORPUS)) | set(plan.get("added") or [])
        day.waiting["fan_picks"] = sorted(titles - anchored - set(plan.get("withdrawn") or {}))
        day.reasks = reasks_due(day)
    if weekly(premise_cfg) and day.paid.path:
        with open(premise_daily.ensure_tags(ctx), encoding="utf-8") as fh:
            tagged = set(json.load(fh).get("tags") or {})
        kept = classified(day)
        shas = {**{key: None for key in changes.listed(ctx, "new")}, **kept}
        st = state(day, "premise_tags")
        settled = st.get("settled") or {}
        # A verdict holds for the classification it was made on: a title classified again is looked at again.
        st["waiting"] = day.waiting["premise_tags"] = sorted(
            key for key, sha in shas.items() if key not in tagged and not (key in settled and settled[key] == sha))
        # Classify asks one of these again once its article is not the one its row read
        # (`classify.changed_articles`): the worklist cuts the premise sections from that article by offset.
        reclassify = [key for key in day.waiting["premise_tags"] if key in kept]
        caching.write_atomically(os.path.join(ctx.path(artifacts.CHANGES), "reclassify.txt"),
                                 "".join(f"{key}\n" for key in reclassify).encode("utf-8"))
        day.paid.save()
    listed = set().union(*day.waiting.values(), day.reasks, *(pending_keys(day, step) for step in REPORT_KEYS))
    caching.write_atomically(os.path.join(ctx.path(artifacts.CHANGES), "waiting.txt"),
                             "".join(f"{key}\n" for key in sorted(listed)).encode("utf-8"))
    return day.waiting


def reasks_due(day):
    """`{key: [dates]}` — the fan-pick re-asks due by today and not yet asked (#187 item 5).

    A title the model did not know, or one released within six months of its first ask, is asked again three
    and six months on (`fan_picks.reask_dates`) with today's prompt and a fresh lead. The schedule is fixed
    once — from `data/fan-picks-reask.json` for titles asked before the ledger, else when its answer is kept
    (`schedule_reasks`) — so asking it again never moves it. Each date is asked once."""
    st = state(day, "fan_picks")
    schedule, done = st.setdefault("reaskPlan", {}), st.setdefault("reasked", {})
    for key, seed in fan_picks.load_reask().items():
        schedule.setdefault(key, fan_picks.reask_dates(seed["askedAt"], seed["known"], seed["released"]))
    today = day.now.date().isoformat()
    due_now = {key: [d for d in dates if d <= today and d not in done.get(key, [])] for key, dates in schedule.items()}
    return {key: dates for key, dates in due_now.items() if dates}


def schedule_reasks(day, rows):
    """Fix the re-ask dates of every kept fan-pick answer that has none yet, from its title's release date in
    today's corpus `rows` — which holds a title added today, as the published corpus does not."""
    schedule = state(day, "fan_picks").setdefault("reaskPlan", {})
    for key, entry in day.paid.answers("fan_picks").items():
        answer = entry.get("answer") or {}
        if key not in schedule and answer.get("at"):
            released = fan_picks.release_date(rows[key].get("facts") or {}) if key in rows else None
            schedule[key] = fan_picks.reask_dates(answer["at"], answer.get("known"), released)


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
    st = state(day, "fan_picks")
    reask_answers = st.setdefault("reaskAnswers", {})
    cap = day.args.fan_picks_max_spend_usd
    asked = []
    if not day.paid.path:
        day.skip("fan_picks", "a weekly Batch step needs the paid-answers ledger (--paid-state) to find its job "
                              "again; the existing input is carried")
    else:
        if not day.can_buy_fan_picks:
            day.skip("fan_picks", "not given --spend with the fan-picks provider's key: nothing is asked, a "
                                  "finished Batch job is still read, and the existing input is carried")
        rows = fan_picks.corpus_rows(ctx.path(artifacts.CORPUS))
        reconcile(day, "fan_picks", cfg)
        waiting = [key for key in day.waiting.get("fan_picks", []) if key in rows]
        reasks = {key: dates for key, dates in day.reasks.items() if key in rows}
        held = pending_keys(day, "fan_picks")
        titles = fan_picks.titles_for(sorted(set(waiting) | set(reasks) | {k for k in held if k in rows}),
                                      rows, ctx.path(artifacts.ARTICLES))
        # A title with no name has no card to ask about (`titles_for`).
        waiting = [key for key in waiting if key in titles]
        reasks = {key: dates for key, dates in reasks.items() if key in titles}
        task = fan_picks.Task(cfg)

        def record(job, key, row):
            reask = key in (job.get("reasks") or ())
            if "value" in row:
                answer = {**row["value"], "key": key, "costUSD": 0.0, "mode": row["by"]["mode"],
                          "provider": row["by"]["provider"], "model": row["by"]["model"],
                          "at": day.now.isoformat(timespec="seconds")}
                if reask:
                    reask_answers[key] = answer
                else:
                    kept[key] = {"requestSha256": fan_picks.fingerprint(key), "accepted": True, "answer": answer,
                                 "error": None}
            elif row.get("refused") and not reask:
                kept[key] = {"requestSha256": fan_picks.fingerprint(key), "accepted": True, "answer": None,
                             "error": {"key": key, "usage": {}, "text": "", "refused": True, "costUSD": 0.0}}
            day.paid.save()

        def done(job, key):
            if key in (job.get("reasks") or ()):
                return (reask_answers.get(key) or {}).get("at", "") >= job["submittedAt"]
            return fan_picks.reusable(kept.get(key), fan_picks.fingerprint(key))

        collect(day, "fan_picks", cfg, task, lambda job: {k: titles[k] for k in job_keys(job) if k in titles},
                cap, record, done, day.can_buy_fan_picks)
        schedule_reasks(day, rows)
        report["due"] = due(cfg, day.now.date(), st.get("lastSubmit"))
        ready = [key for key in titles if fan_picks.reusable(kept.get(key), fan_picks.fingerprint(key))]
        held = pending_keys(day, "fan_picks")
        ask = [titles[key] for key in waiting if key not in ready and key not in held]
        again = [titles[key] for key in sorted(reasks) if key not in held]
        report["waiting"] = len(ask)
        report["reasks"] = {"due": len(again), "answered": len(reask_answers)}
        if report["due"] and day.can_buy_fan_picks:
            each = fan_picks.PILOT_COST * fan_picks.MARGIN * lib_llm.BATCH_FACTOR

            def reasked(take):
                return {"reasks": [t["key"] for t in take if t["key"] in reasks],
                        "reaskDates": {t["key"]: reasks[t["key"]] for t in take if t["key"] in reasks}}
            intent = submit(day, "fan_picks", cfg, task, again + ask, cap, each, reasked)
            # Each date is asked once, from the moment its submit is recorded: a submit never confirmed gives
            # its dates back (`reconcile`).
            for key, dates in ((intent or {}).get("reaskDates") or {}).items():
                st.setdefault("reasked", {}).setdefault(key, []).extend(dates)
            day.paid.save()
        asked = ready
    try:
        result = fan_picks.daily_update(
            corpus_path=ctx.path(artifacts.CORPUS), articles_path=ctx.path(artifacts.ARTICLES),
            franchises_path=ctx.path(artifacts.FRANCHISES), existing_path=existing, out=existing, keys=asked,
            max_spend=cap, backfill=fan_picks.load_backfill(), kept=kept, persist=day.paid.save,
            reasks=reask_answers, generate=batch_only)
    except (OSError, ValueError, RuntimeError) as error:
        raise StageError(f"fan_picks: {error}") from error
    day.ran.append("fan_picks")
    print(f"==> fan_picks: {json.dumps(result, sort_keys=True)}", file=sys.stderr)
    return result


def batch_only(title, key, ask):
    """The weekly step merges only answers it already holds; nothing is asked online outside a collect."""
    raise StageError(f"fan_picks: {key} has no kept answer, and the weekly step asks only by Batch")


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
                                 "DEN_EMBED_URL: nothing is asked, and a finished Batch job is still read")
    st, kept = state(day, "premise_tags"), day.paid.answers("premise_tags")
    reconcile(day, "premise_tags", cfg)
    held = pending_keys(day, "premise_tags")
    waiting = set(day.waiting.get("premise_tags", []))
    report["due"] = due(cfg, day.now.date(), st.get("lastSubmit"))
    answered_before = {key for key in waiting if (kept.get(key) or {}).get("tags")}
    if not held and not answered_before and not (report["due"] and waiting and day.can_buy_premise):
        report["waiting"] = len(waiting)
        return None
    cap = getattr(day.args, "premise_max_spend_usd", 1.0)
    tags_path = premise_daily.ensure_tags(day.ctx)
    work = os.path.join(day.ctx.out_dir, "premise-increment", "weekly")
    plan_path = os.path.join(work, "plan.json")
    os.makedirs(work, exist_ok=True)
    # A backlog past what one run can hold waits for the next run rather than failing this one. A title with
    # an answer already kept goes first: it costs nothing to merge.
    order = sorted(waiting - held, key=lambda key: (key not in answered_before, key))
    report["deferred"] = len(order[PREMISE_LIMIT:])
    with open(plan_path, "w", encoding="utf-8") as fh:
        json.dump({"baseline": changes.planned(day.ctx)["baseline"],
                   "added": sorted(set(order[:PREMISE_LIMIT]) | held), "changed": {}}, fh)
    ceiling = max(1, int(cap / lib_llm.price(cfg["model"])[1]))
    work, manifest = premise_daily.prepare(day.ctx, tags_path, ceiling, plan=plan_path, work=work)
    rows = premise_daily.rows_of(os.path.join(work, "gen"))
    settled = st.setdefault("settled", {})
    for key, why in (manifest.get("skippedKeys") or {}).items():
        if why["reason"] in SETTLES and key not in held:
            settled[key] = why.get("articleSha256")
    task, today = premise_daily.Task(cfg), day.now.date().isoformat()
    asks = {key: premise_daily.asked_for(cfg, task, row) for key, row in rows.items()}
    answered = {key: {"tags": kept[key]["tags"], "by": kept[key]["by"]} for key in rows
                if (kept.get(key) or {}).get("tags") and kept[key].get("ask") == asks[key]}

    def record(job, key, row):
        ask = job["asks"][key]
        if "value" in row:
            kept[key] = {"ask": ask, "tags": row["value"], "by": row["by"]}
            if asks.get(key) == ask:
                answered[key] = {"tags": row["value"], "by": row["by"]}
        else:
            premise_daily.mark_untaggable(kept, key, "refused" if row.get("refused") else "short", ask,
                                          row.get("by"), today)
        day.paid.save()

    def done(job, key):
        entry = kept.get(key) or {}
        return entry.get("ask") == job["asks"].get(key) and bool(entry.get("tags") or entry.get("untaggable"))

    def askable(job):
        # A title whose evidence is not what was submitted — its article changed, or it left the worklist —
        # has its answer kept but is never finished online on other evidence.
        return {key: rows[key] for key in job_keys(job) if key in rows and asks[key] == job["asks"].get(key)}
    collect(day, "premise_tags", cfg, task, askable, cap, record, done, day.can_buy_premise)
    held = pending_keys(day, "premise_tags")
    resting = [key for key in rows
               if key not in answered and premise_daily.untaggable(kept.get(key), asks[key], today)]
    ask = [row for key, row in rows.items() if key not in answered and key not in held and key not in resting]
    report["waiting"], report["notRetriedYet"] = len(ask), len(resting)
    if report["due"] and day.can_buy_premise:
        each = premise_daily.projected(manifest, cfg) / max(1, manifest["titles"])
        submit(day, "premise_tags", cfg, task, ask, cap, each,
               lambda take: {"asks": {row["key"]: asks[row["key"]] for row in take}})
    if not answered:
        return None
    if not day.environ.get("DEN_EMBED_URL"):
        day.skip("premise_tags", f"{len(answered)} title(s) with kept tags wait for DEN_EMBED_URL, which embeds "
                                 "them")
        return None
    collected = os.path.join(day.ctx.out_dir, "premise-increment", "collected")
    premise_daily.write_collected(collected, answered)
    models = {}
    for row in answered.values():
        models[row["by"]["model"]] = models.get(row["by"]["model"], 0) + 1
    result = {"provider": cfg["provider"], "model": cfg["model"], "mode": "batch", "titles": len(answered),
              "generated": len(answered), "resumed": 0, "byModel": dict(sorted(models.items())), "untagged": [],
              "inputTokens": 0, "outputTokens": 0, "reasoningTokens": 0, "costUSD": report["costUSD"],
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
                  "resumed": 0, "byModel": {}, "untagged": [], "refused": [], "short": [], "inputTokens": 0,
                  "outputTokens": 0, "reasoningTokens": 0, "costUSD": 0.0, "projectedSpendUSD": 0.0,
                  "spendCapUSD": premise_cap}
    else:
        try:
            result = premise_daily.generate(os.path.join(work, "gen"), premise_cap, cfg,
                                            kept=day.paid.answers("premise_tags") if day.paid.path else None,
                                            persist=day.paid.save, today=day.now.date().isoformat())
            premise_daily.merge(day.ctx, os.path.join(work, "gen"), result, day.now)
        except (OSError, ValueError, RuntimeError) as error:
            raise StageError(f"premise_tags: {error}") from error
    day.ledger.actual("premiseTags", result["costUSD"])
    day.premise = result
    day.ran.append("premise_tags")
    print(f"==> premise_tags: {json.dumps(result, sort_keys=True)}", file=sys.stderr)
    return result


# --- Batch jobs --------------------------------------------------------------------------------------------

def reconcile(day, step, cfg):
    """Settle every submit an earlier run recorded and never saw answered: the job its label finds is taken
    into the ledger; without one, the titles and any re-ask dates it held are given back to wait again.

    Anthropic cannot list its jobs by label, so a lost submit there is given back too and may be bought twice;
    no step submits to Anthropic today (fallbacks are asked online)."""
    report = day.models[step]
    for intent in intents(day, step):
        try:
            job = lib_llm.adopt(cfg, intent)
        except lib_llm.providers.Unavailable as error:
            report["pending"].append({"label": intent["label"], "titles": len(job_keys(intent)),
                                      "error": lib_llm.error_code(error)})
            continue
        day.paid.data["intents"].remove(intent)
        if job:
            day.paid.data["batches"].append(job)
        else:
            day.ledger.collected(intent.get("projectedUSD") or 0.0)
            done = state(day, step).get("reasked") or {}
            for key, dates in (intent.get("reaskDates") or {}).items():
                done[key] = [date for date in done.get(key, []) if date not in dates]
        day.paid.save()


def collect(day, step, cfg, task, askable, cap, record, done, spend):
    """Poll the step's jobs and take what each answered through `record(job, key, row)`; finish what it did
    not online inside today's allowance when `spend` (nothing otherwise), and drop a job once nothing is
    owed. `askable(job)` gives the items of the job's titles that may be finished online; any other is read
    from the job and never asked. `done(job, key)` says a title is already kept."""
    report = day.models[step]
    for job in jobs(day, step):
        projected = job.get("projectedUSD") or 0.0
        hours = age(job, day.now)
        items_by_key = askable(job)
        items = [items_by_key.get(key) or {"key": key} for key in job_keys(job)]
        gone = {key for key in job_keys(job) if key not in items_by_key}
        waiting = {"label": job["label"], "submittedAt": job["submittedAt"], "ageHours": hours,
                   "titles": len(job_keys(job))}
        budget = lib_llm.Budget(room(day, step, cap, projected) if spend else 0.0)
        try:
            outcome, _rows, calls, unfinished = lib_llm.collect(
                cfg, job, task, items, budget, record=lambda key, row: record(job, key, row),
                done=lambda key: done(job, key), finish=None if spend else 0, stale=hours > STALE_HOURS,
                unaskable=gone)
        except (lib_llm.OverBudget, lib_llm.providers.Unavailable) as error:
            # The job stays in the ledger and is collected by a later run: an outage must not stop the day
            # or lose the job.
            report["pending"].append({**waiting, "error": lib_llm.error_code(error)})
            continue
        charge(day, step, sum(call["costUSD"] for call in calls))
        if outcome == "running":
            report["pending"].append(waiting)
            continue
        if projected:
            # Read once: its cost is measured now, and no longer committed.
            day.ledger.collected(projected)
            job["projectedUSD"] = 0.0
        if outcome in ("expired", "failed"):
            report["expired"].append(job["label"])
        report["collected"] += len(job_keys(job)) - unfinished
        if unfinished and hours < GIVE_UP_DAYS * 24:
            report["pending"].append({**waiting, "unfinished": unfinished})
        else:
            day.paid.data["batches"].remove(job)
        day.paid.save()


def submit(day, step, cfg, task, items, cap, each, extra=None):
    """One Batch job for as many of `items` as today's room holds at `each` dollars a title; the rest wait.
    With nothing affordable no job is made and the step stays due. Recorded as an intent before it is sent,
    and as a job once it is made. Returns the intent submitted, or None."""
    st, report = state(day, step), day.models[step]
    today = day.now.date().isoformat()
    take = items[:max(0, int(room(day, step, cap) / each))] if each > 0 else items
    report["deferred"] += len(items) - len(take)
    if items and not take:
        return None
    if take:
        # Unique per run, so a second run on the same day never adopts the first one's job as its own.
        label = f"{step.replace('_', '-')}-{day.now.strftime('%Y%m%dT%H%M%S')}"
        intent = {**lib_llm.batch_record(cfg, task, take, label),
                  "submittedAt": day.now.isoformat(timespec="seconds"),
                  "projectedUSD": round(len(take) * each, 6), **(extra(take) if extra else {})}
        day.paid.data["intents"].append(intent)
        day.ledger.committed(intent["projectedUSD"])
        day.paid.save()
        try:
            job = lib_llm.submit(cfg, task, take, label, os.path.join(day.ctx.out_dir, "batches"))
        except lib_llm.providers.Unavailable as error:
            # The intent stays: the next run finds the job by its label if the create went through.
            report["submitError"] = lib_llm.error_code(error)
            return intent
        day.paid.data["intents"].remove(intent)
        day.paid.data["batches"].append({**job, **intent})
        report["submitted"] = {"label": label, "titles": len(take)}
        report["pending"].append({"label": label, "submittedAt": intent["submittedAt"], "ageHours": 0.0,
                                  "titles": len(take)})
    st["lastSubmit"] = today
    day.paid.save()
    return intent if take else None
