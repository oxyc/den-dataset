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
                 "fallback": None}

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
    """A dollar cap over a run's calls, checked against each call's ceiling before it is sent."""

    def __init__(self, cap, spent=0.0):
        self.cap, self.spent = cap, spent
        self._lock = threading.Lock()

    def admit(self, amount):
        with self._lock:
            if self.spent + amount > self.cap:
                raise OverBudget(f"a call can cost up to ${amount:.4f}; ${self.spent:.4f} is spent, crossing the "
                                 f"${self.cap:.2f} cap before the call")

    def charge(self, amount):
        with self._lock:
            self.spent += amount


# --- asking ---------------------------------------------------------------------------------------------

def request(prompt, max_tokens, system=None, schema=None, json_out=True, name=None):
    return {"prompt": prompt, "system": system, "schema": schema, "maxOutputTokens": max_tokens,
            "json": json_out, "name": name}


def online(cfg, req, budget=None):
    """One call. The answer, with `costUSD` and `by`; raises `providers.Unavailable` or `OverBudget`."""
    if budget is not None:
        budget.admit(ceiling(cfg, req))
    result = providers.PROVIDERS[cfg["provider"]].online(cfg, req)
    result["costUSD"] = cost(cfg, result["usage"], "online")
    if budget is not None:
        budget.charge(result["costUSD"])
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
            if record is not None:
                record(key, row)

    def ask(cfg_, group, mode="online"):
        """One call over `group`; returns the items to ask again (split, retried or fallen back)."""
        try:
            result = online(cfg_, task.request(group), budget)
        except providers.Unavailable as error:
            for item in group:
                settle(task.key(item), {"error": str(error), "unavailable": True})
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

def submit(cfg, task, items, label, directory):
    """Submit `items` as one Batch job. Returns the job record a later run collects with `collect`."""
    groups = chunks(items, cfg["titlesPerCall"])
    customs = {f"c{n:05d}": group for n, group in enumerate(groups)}
    os.makedirs(directory, exist_ok=True)
    job = providers.PROVIDERS[cfg["provider"]].submit(
        cfg, {custom: task.request(group) for custom, group in customs.items()}, label,
        os.path.join(directory, f"{label}.jsonl"))
    return {**job, "step": cfg["step"], "provider": cfg["provider"], "model": cfg["model"], "mode": "batch",
            "version": task.version, "label": label,
            "chunks": {custom: [task.key(item) for item in group] for custom, group in customs.items()},
            "submittedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}


def collect(cfg, job, task, items, budget=None, workers=1, record=None):
    """Poll `job`. Returns `(state, rows, calls)`: `running` with nothing, else every title's row.

    What the job answered is parsed; what it refused, could not parse, or never answered — every title of an
    expired or failed job — is finished online through `generate`, with its fallback. A job is never
    resubmitted: a title already paid for in a batch is not paid for in another."""
    state, out = providers.PROVIDERS[job["provider"]].poll(cfg, job, job["chunks"])
    if state == "running":
        return state, {}, []
    by_key = {task.key(item): item for item in items}
    rows, calls, leftover = {}, [], []
    for custom, keys in job["chunks"].items():
        group = [by_key[key] for key in keys if key in by_key]
        result = out.get(custom) if isinstance(out, dict) else None
        if not group:
            continue
        if not result or "error" in result:
            leftover += group
            continue
        result["costUSD"] = cost(cfg, result["usage"], "batch")
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
                rows[key] = {"value": parsed[key], "by": provenance(cfg, task, "batch")}
                if record is not None:
                    record(key, rows[key])
            else:
                leftover.append(item)
    if leftover:
        more, more_calls = generate({**cfg, "mode": "online"}, task, leftover, budget, workers, record)
        rows.update(more)
        calls += more_calls
    return state, rows, calls
