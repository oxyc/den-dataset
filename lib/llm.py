"""One model provider layer: any paid text step, on any model, online or Batch, by config (oxyc/den-dataset#183).

    cfg = llm.step("premise_tags")                 # data/models.json, or DEN_MODELS=<file> for a bake-off
    rows, calls = llm.generate(cfg, task, items)   # online, `titlesPerCall` items a call
    job = llm.submit(cfg, task, items, label, dir) # Batch: submit in one run …
    state, rows, calls = llm.collect(job, task, items)   # … collect in the next

A step names what it asks — a `task` with `name`, `version`, `request(items)` and `parse(items, answer)` —
and the config names who answers: provider, model, mode, titles per call, thinking and a fallback. The
providers' dialects are in `lib/llm_providers.py`.

**Refusals don't sink a call's other titles.** A refused multi-title call is asked again one title per call;
a title refused alone is asked once more, then by the step's `fallback`. A title counts as refused only when
the fallback refuses it too. A call whose answer doesn't parse is split the same way, and a single title that
still doesn't parse is a per-title error.

**Every call is priced** from one table (`PRICES`, dated, so Gemini Flash's 2027 doubling is in it) and
returned with its normalised usage, so the spend caps read one record whatever model answered. A model with
no price is refused before it is asked: a switch must never make a cap blind.

**Every row records who answered**: `by` = provider, model, mode and the task's version, so a mixed index can
be found and re-run later.
"""
import concurrent.futures
import datetime
import json
import math
import os
import threading

from . import llm_providers as providers

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(REPO, "data", "models.json")
#: Batch is half the standard price on all three APIs.
BATCH_FACTOR = 0.5
CLI_PROVIDERS = ("claude-cli", "codex-cli")
STEP_DEFAULTS = {"mode": "online", "titlesPerCall": 1, "thinking": None, "maxOutputTokens": 4096,
                 "fallback": None, "cadence": "daily", "day": None}
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

#: List prices per token: model -> [(from date, input, output, cached input)], oldest first. Cached input is
#: listed at the full input price where the provider's discount is not one we rely on, so a cache hit can
#: only make a projection pessimistic. Sources and dates: docs/MODEL-COSTS.md.
PRICES = {
    "gemini-3.7-flash": [("2026-01-01", 0.75e-6, 3.75e-6, 0.75e-6), ("2027-01-01", 1.50e-6, 7.50e-6, 1.50e-6)],
    "gemini-3.8-flash": [("2026-01-01", 0.75e-6, 3.75e-6, 0.75e-6), ("2027-01-01", 1.50e-6, 7.50e-6, 1.50e-6)],
    "gemini-3.5-flash-lite": [("2026-01-01", 0.30e-6, 2.50e-6, 0.30e-6)],
    "gemini-3.1-pro": [("2026-01-01", 2.00e-6, 12.0e-6, 2.00e-6)],
    "gpt-5.6-luna": [("2026-01-01", 0.20e-6, 1.20e-6, 0.20e-6)],
    "gpt-5.6-terra": [("2026-01-01", 2.00e-6, 12.0e-6, 2.00e-6)],
    "gpt-5.6-sol": [("2026-01-01", 4.00e-6, 20.0e-6, 4.00e-6)],
    "gpt-6-luna": [("2026-01-01", 0.10e-6, 0.50e-6, 0.10e-6)],
    "gpt-6-sol": [("2026-01-01", 2.00e-6, 10.0e-6, 2.00e-6)],
    "claude-haiku-4-5-20251001": [("2026-01-01", 1.00e-6, 5.00e-6, 0.10e-6)],
    "claude-haiku-4-5": [("2026-01-01", 1.00e-6, 5.00e-6, 0.10e-6)],
    "claude-sonnet-5": [("2026-01-01", 2.00e-6, 10.0e-6, 0.20e-6)],
    "claude-opus-5": [("2026-01-01", 5.00e-6, 25.0e-6, 0.50e-6)],
}


class OverBudget(RuntimeError):
    """The next call could cost more than the cap leaves. Nothing was sent."""


#: A row's error when the cap would not admit its call (`generate`).
OVER_BUDGET = "over budget"


def error_code(error):
    """A short name for a failed call, for what is published (the daily report, the paid-answers ledger, a
    public Actions log): a provider's error text can carry account or organisation ids."""
    if isinstance(error, OverBudget):
        return OVER_BUDGET
    status = getattr(error, "status", None)
    return f"HTTP {status}" if status else "unavailable"


# --- config ---------------------------------------------------------------------------------------------

def load(path=None):
    with open(path or os.environ.get("DEN_MODELS") or CONFIG, encoding="utf-8") as fh:
        return json.load(fh)


def step(name, overrides=None, path=None):
    """The config for step `name`: `data/models.json` (or `DEN_MODELS`), then `overrides`, checked."""
    steps = load(path)
    if name not in steps:
        raise ValueError(f"no model is configured for the {name} step")
    cfg = {**STEP_DEFAULTS, **steps[name], **{k: v for k, v in (overrides or {}).items() if v is not None},
           "step": name}
    if cfg["fallback"]:
        cfg["fallback"] = {**STEP_DEFAULTS, "maxOutputTokens": cfg["maxOutputTokens"], **cfg["fallback"],
                           "mode": "online", "fallback": None, "step": name}
        check(cfg["fallback"])
    check(cfg)
    return cfg


def check(cfg):
    if cfg.get("provider") not in providers.PROVIDERS:
        raise ValueError(f"{cfg.get('step')}: unknown provider {cfg.get('provider')!r}")
    if cfg["mode"] not in ("online", "batch"):
        raise ValueError(f"{cfg['step']}: mode is online or batch, not {cfg['mode']!r}")
    if cfg["provider"] in CLI_PROVIDERS:
        if cfg["mode"] == "batch":
            raise ValueError(f"{cfg['step']}: {cfg['provider']} has no Batch mode")
    else:
        price(cfg["model"])
    if not isinstance(cfg["titlesPerCall"], int) or cfg["titlesPerCall"] < 1:
        raise ValueError(f"{cfg['step']}: titlesPerCall must be a positive integer")
    cadence, day = cfg.get("cadence", "daily"), cfg.get("day")
    if cadence not in ("daily", "weekly") or (cadence == "weekly") != (day in WEEKDAYS):
        raise ValueError(f"{cfg['step']}: cadence is daily, or weekly with a day ({', '.join(WEEKDAYS)})")


def provenance(cfg, task, mode=None):
    return {"provider": cfg["provider"], "model": cfg["model"], "mode": mode or cfg["mode"],
            "version": task.version}


# --- prices ---------------------------------------------------------------------------------------------

def price(model, on=None):
    """`(input, output, cached input)` per token on date `on` (default today, UTC)."""
    if model not in PRICES:
        raise ValueError(f"no price for {model!r}: add it to lib/llm.py PRICES before asking it")
    day = (on or datetime.datetime.now(datetime.timezone.utc).date()).isoformat()
    rows = [row for row in PRICES[model] if row[0] <= day] or PRICES[model][:1]
    return rows[-1][1:]


def cost(cfg, usage, mode, on=None):
    """Dollars for `usage` (normalised). The owner's plans bill nothing per call."""
    if cfg["provider"] in CLI_PROVIDERS:
        return 0.0
    rate_in, rate_out, rate_cached = price(cfg["model"], on)
    billed = ((usage["inputTokens"] - usage["cachedTokens"]) * rate_in + usage["cachedTokens"] * rate_cached
              + (usage["outputTokens"] + usage["reasoningTokens"]) * rate_out)
    return billed * (BATCH_FACTOR if mode == "batch" else 1.0)


def ceiling(cfg, request):
    """The most one call can cost: its characters as tokens at three a token, plus every output token."""
    if cfg["provider"] in CLI_PROVIDERS:
        return 0.0
    chars = len(request.get("system") or "") + len(request["prompt"]) + len(json.dumps(request.get("schema"))) + 300
    rate_in, rate_out, _ = price(cfg["model"])
    return math.ceil(chars / 3) * rate_in + request["maxOutputTokens"] * rate_out


class Budget:
    """A dollar cap over a run's calls, checked against each call's ceiling before it is sent. What an
    admitted call may cost is held until it is charged, so calls in flight on other threads count too."""

    def __init__(self, cap, spent=0.0):
        self.cap, self.spent, self.held = cap, spent, 0.0
        self._lock = threading.Lock()

    def admit(self, amount):
        with self._lock:
            if self.spent + self.held + amount > self.cap:
                raise OverBudget(f"a call can cost up to ${amount:.4f}; ${self.spent + self.held:.4f} is spent or "
                                 f"in flight, crossing the ${self.cap:.2f} cap before the call")
            self.held += amount

    def charge(self, amount, held=0.0):
        """Record `amount` spent and release `held` (what `admit` held for the call)."""
        with self._lock:
            self.spent += amount
            self.held -= held


# --- asking ---------------------------------------------------------------------------------------------

def request(prompt, max_tokens, system=None, schema=None, json_out=True, name=None):
    return {"prompt": prompt, "system": system, "schema": schema, "maxOutputTokens": max_tokens,
            "json": json_out, "name": name}


def online(cfg, req, budget=None):
    """One call. The answer, with `costUSD` and `by`; raises `providers.Unavailable` or `OverBudget`.

    A request sent again after its response was lost may have been billed both times, so its cost counts
    once for every send (`providers.sends`); the transport tries such a request at most twice."""
    hold = ceiling(cfg, req) * providers.GENERATION_SENDS
    if budget is not None:
        budget.admit(hold)
    try:
        result = providers.PROVIDERS[cfg["provider"]].online(cfg, req)
    except providers.Unavailable:
        if budget is not None:
            budget.charge(ceiling(cfg, req) * (providers.sends() - 1), held=hold)
        raise
    result["costUSD"] = cost(cfg, result["usage"], "online") * providers.sends()
    if budget is not None:
        budget.charge(result["costUSD"], held=hold)
    return result


def chunks(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


def call_record(cfg, mode, result, count):
    return {"provider": cfg["provider"], "model": cfg["model"], "mode": mode, "items": count,
            "usage": result["usage"], "costUSD": result["costUSD"], "refused": result["refused"]}


def generate(cfg, task, items, budget=None, workers=1, record=None):
    """Ask `task` about `items` online, `cfg["titlesPerCall"]` at a time, with the refusal and parse
    fallbacks above. Returns `(rows, calls)`.

    `rows[key]` is `{"value", "by"}`, `{"refused": True, "by"}` or `{"error", "unavailable"?}`. `record(key,
    row)` is called as each title's row is final, from one thread at a time, so a caller can keep it before
    the run ends."""
    by_key = {task.key(item): item for item in items}
    rows, calls, lock = {}, [], threading.Lock()

    def settle(key, row):
        with lock:
            rows[key] = row
            # An unanswered title is not final: it is asked again later, so nothing is kept for it.
            if record is not None and not row.get("unavailable"):
                record(key, row)

    def ask(cfg_, group, mode="online"):
        """One call over `group`; returns the items to ask again (split, retried or fallen back)."""
        try:
            result = online(cfg_, task.request(group), budget)
        except (providers.Unavailable, OverBudget) as error:
            # Not answered — an outage, or a call the cap would not admit, so nothing was sent. The titles
            # are asked again by a later run; the ones already answered keep their rows.
            for item in group:
                settle(task.key(item), {"error": error_code(error), "unavailable": True})
            return [], []
        with lock:
            calls.append(call_record(cfg_, mode, result, len(group)))
        if result["refused"]:
            return group, []
        try:
            parsed = task.parse(group, result)
        except (ValueError, KeyError, TypeError) as error:
            if len(group) > 1:
                return [], group
            settle(task.key(group[0]), {"error": f"{type(error).__name__}: {error}"[:300],
                                        "by": provenance(cfg_, task, mode)})
            return [], []
        for item in group:
            key = task.key(item)
            if key in parsed:
                settle(key, {"value": parsed[key], "by": provenance(cfg_, task, mode)})
            elif len(group) == 1:
                settle(key, {"error": "the answer has no row for it", "by": provenance(cfg_, task, mode)})
        return [], [item for item in group if task.key(item) not in parsed] if len(group) > 1 else []

    def phase(cfg_, groups):
        refused, unparsed = [], []
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for again, split in pool.map(lambda group: ask(cfg_, group), groups):
                refused += again
                unparsed += split
        return refused, unparsed

    def singles(group):
        return [[item] for item in group]

    lone, split = phase(cfg, chunks(list(by_key.values()), cfg["titlesPerCall"]))
    if lone and cfg["titlesPerCall"] > 1 or split:
        # A multi-title call that was refused or did not parse: each of its titles alone. A single title that
        # does not parse is settled as an error inside `ask`, so only refusals come back from here.
        lone, _ = phase(cfg, singles(lone + split))
    # A title refused on its own is asked once more: Gemini's empty answers are not always its last word.
    still, _ = phase(cfg, singles(lone))
    fallback = cfg.get("fallback")
    if fallback and still:
        still, _ = phase(fallback, singles(still))
    for item in still:
        settle(task.key(item), {"refused": True, "by": provenance(fallback or cfg, task, "online")})
    return rows, calls


# --- Batch ----------------------------------------------------------------------------------------------

def batch_record(cfg, task, items, label):
    """What a Batch job of `items` is, before the provider has made it: who is asked, under which label, and
    which titles each request carries. A caller can keep it before submitting, to find the job by its label
    (`adopt`) if the submit's response is lost."""
    groups = chunks(items, cfg["titlesPerCall"])
    return {"step": cfg["step"], "provider": cfg["provider"], "model": cfg["model"], "mode": "batch",
            "version": task.version, "label": label,
            "chunks": {f"c{n:05d}": [task.key(item) for item in group] for n, group in enumerate(groups)},
            "submittedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}


def adopt(cfg, record):
    """The job a `batch_record` was submitted as, found by its label, or None when the provider has none (or
    cannot list its jobs: Anthropic)."""
    find = getattr(providers.PROVIDERS[record["provider"]], "find", None)
    job = find(job_cfg(cfg, record), record["label"]) if find else None
    return {**job, **record} if job else None


def submit(cfg, task, items, label, directory):
    """Submit `items` as one Batch job. Returns the job record a later run collects with `collect`."""
    record = batch_record(cfg, task, items, label)
    by_key = {task.key(item): item for item in items}
    os.makedirs(directory, exist_ok=True)
    # A create whose response was lost may still have made the job: find it by label before making another.
    return adopt(cfg, record) or {**providers.PROVIDERS[cfg["provider"]].submit(
        cfg, {custom: task.request([by_key[key] for key in keys]) for custom, keys in record["chunks"].items()},
        label, os.path.join(directory, f"{label}.jsonl")), **record}


def job_cfg(cfg, job):
    """The config `job` was submitted under: its own provider and model, so its answers are priced and
    credited to the model that gave them even after the step's model is switched. The fallback is today's."""
    return {**cfg, "provider": job["provider"], "model": job["model"], "mode": "batch"}


def collect(cfg, job, task, items, budget=None, workers=1, record=None, done=None, finish=None, stale=False,
            unaskable=()):
    """Poll `job`. Returns `(state, rows, calls, unfinished)`; `running` comes with nothing done.

    What the job answered is parsed and recorded. What it refused, could not parse or never answered — every
    title of an expired or failed job, or of one the provider no longer knows (404) — is finished online
    through `generate`, with its fallback. A job is never resubmitted: a title already paid for in a batch is
    not paid for in another.

    The online finish can span several runs. `done(key)` says a title is already settled and kept, so it is
    neither recorded nor asked again; `finish` is how many titles this run may finish online (None: all; 0:
    none, as on a day that may not spend). `unfinished` counts the titles still owed — finish them with a
    later `collect` of the same job. The job's Batch cost is counted the first time it is read
    (`job["batchCounted"]`), so reading it again reports no spend twice.

    `stale` says the job has run far past every provider's window: one still reported running is taken as
    expired and finished online, so a state nothing here knows cannot hold its titles forever.

    `unaskable` names items whose evidence is gone since the job was submitted: what the job answered for
    them is recorded, but they are never finished online and are not owed."""
    cfg = job_cfg(cfg, job)
    done = done or (lambda key: False)
    try:
        state, out = providers.PROVIDERS[job["provider"]].poll(cfg, job, job["chunks"])
    except providers.Unavailable as error:
        if error.status != 404:
            raise
        state, out = "expired", {}
    if state == "running" and stale:
        state, out = "expired", {}
    if state == "running":
        return state, {}, [], sum(len(keys) for keys in job["chunks"].values())
    by_key = {task.key(item): item for item in items}
    rows, calls, leftover, missing = {}, [], [], 0
    first = not job.get("batchCounted")
    for custom, keys in job["chunks"].items():
        group = [by_key[key] for key in keys if key in by_key]
        missing += sum(1 for key in keys if key not in by_key and not done(key))
        result = out.get(custom) if isinstance(out, dict) else None
        if not group:
            continue
        if not result or "error" in result:
            leftover += group
            continue
        result["costUSD"] = cost(cfg, result["usage"], "batch")
        if first:
            calls.append(call_record(cfg, "batch", result, len(group)))
        parsed = {}
        if not result["refused"]:
            try:
                parsed = task.parse(group, result)
            except (ValueError, KeyError, TypeError):
                parsed = {}
        for item in group:
            key = task.key(item)
            if key in parsed:
                if not done(key):
                    rows[key] = {"value": parsed[key], "by": provenance(cfg, task, "batch")}
                    if record is not None:
                        record(key, rows[key])
            else:
                leftover.append(item)
    job["batchCounted"] = True
    leftover = [item for item in leftover if not done(task.key(item)) and task.key(item) not in unaskable]
    now = leftover if finish is None else leftover[:max(0, finish)]
    if now:
        more, more_calls = generate({**cfg, "mode": "online"}, task, now, budget, workers, record)
        rows.update(more)
        calls += more_calls
    unanswered = sum(1 for item in now if rows.get(task.key(item), {}).get("unavailable"))
    return state, rows, calls, len(leftover) - len(now) + unanswered + missing
