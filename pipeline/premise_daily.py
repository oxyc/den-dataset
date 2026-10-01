"""Paid, resumable premise tags for a daily run's newly admitted titles, asked through `lib/llm.py`.

The `premise_tags` step in `data/models.json` says who answers: gpt-5.6-luna with a JSON schema, and Claude
Haiku 4.5 for a title luna refuses (#183). The prompt is `data/premise-tags-v1.SPEC.md`, the validator
`validate_premise_batch.py`, and each answered title is written to its batch's output as it arrives, with
who answered it, so a stopped run resumes without paying for it again.
"""
import collections
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

from lib import llm
from lib import llm_providers as providers

from . import artifacts, embed_canary
from .contract import REPO, StageError
from . import validate_premise_batch as validate
from store import vector_blob

STEP = "premise_tags"
SPEC = os.path.join(REPO, "data", "premise-tags-v1.SPEC.md")
MARGIN = 1.35
MAX_RETRIES = 4
#: What the provider is held to: one row per title, its key echoed and its tags as strings. The validator
#: still checks the keys and every tag; a schema only stops the answer being something other than rows.
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["rows"],
          "properties": {"rows": {"type": "array", "items": {
              "type": "object", "additionalProperties": False, "required": ["key", "tags"],
              "properties": {"key": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}}}}}}


class GenerationError(RuntimeError):
    def __init__(self, message, cost_usd):
        super().__init__(message)
        self.cost_usd = round(cost_usd, 6)


def config():
    return llm.step(STEP)


def can_buy(environ, cfg=None):
    """Whether the configured provider's key is in `environ`. The fallback's is optional: without it a
    refused title waits for a later run."""
    cfg = cfg or config()
    name = getattr(providers.PROVIDERS[cfg["provider"]], "KEY", None)
    return bool(name and environ.get(name))


def _json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _write(path, value):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _extract(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S)
    value = json.loads(text)
    if isinstance(value, dict) and isinstance(value.get("rows"), list):
        value = value["rows"]
    if not isinstance(value, list):
        raise ValueError("response is not a JSON array")
    return value


def projected(manifest, cfg=None):
    """The worklist's estimated tokens at the configured model's price, with a margin."""
    cfg = cfg or config()
    rate_in, rate_out, _ = llm.price(cfg["model"])
    factor = llm.BATCH_FACTOR if cfg["mode"] == "batch" else 1.0
    return round((manifest.get("estimatedInputTokens", 0) * rate_in +
                  manifest.get("estimatedOutputTokens", 0) * rate_out) * MARGIN * factor, 6)


def _clean(row, source):
    kept = []
    for tag in row.get("tags") or []:
        fixed = validate.normalise(tag) if isinstance(tag, str) else ""
        if not fixed or not validate.TAG.match(fixed) or validate.has_non_ascii(fixed):
            continue
        if validate.genre_words(fixed) or fixed in validate.VAGUE_TAGS \
                or validate.proper_nouns(fixed, source):
            continue
        if fixed not in kept:
            kept.append(fixed)
    return {"key": row.get("key"), "tags": kept[:validate.MAX_TAGS]}


def _checked(batch_in, batch_out):
    fatal, _notes = validate.check(batch_in, batch_out, strict_language=True)
    if fatal:
        raise RuntimeError("; ".join(fatal[:4]))
    sources = {row["key"]: row.get("plot", "") for row in batch_in}
    return [_clean(row, sources[row["key"]]) for row in batch_out]


class Task:
    """Premise tags for a list of worklist rows, as `lib/llm.py` asks them."""
    name = STEP

    def __init__(self, cfg):
        with open(SPEC, encoding="utf-8") as handle:
            self.spec = handle.read()
        self.version = f"premise-tags-v1.SPEC.md@{hashlib.sha256(self.spec.encode()).hexdigest()[:12]}"
        self.max_tokens = cfg["maxOutputTokens"]

    @staticmethod
    def key(row):
        return row["key"]

    def request(self, rows):
        return llm.request("Return only the JSON answer array for these rows:\n"
                           + json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
                           self.max_tokens, system=self.spec, schema=SCHEMA, name=STEP)

    def parse(self, rows, answer):
        """The rows' cleaned tags. A row left under the floor once its invalid tags are dropped is not
        returned, so `lib/llm.py` asks that title again on its own — the repair, by another name.

        An answer that left a title out is read for the titles it has: only the missing ones are asked
        again, not the whole call. A key it invented, a duplicate or a shifted batch still condemns it."""
        value = answer["value"] if answer["value"] is not None else _extract(answer["text"])
        if isinstance(value, dict):
            value = value.get("rows")
        if not isinstance(value, list):
            raise ValueError("the answer has no rows")
        answered = {row.get("key") for row in value if isinstance(row, dict)}
        try:
            cleaned = _checked([row for row in rows if row["key"] in answered] or rows, value)
        except RuntimeError as error:
            raise ValueError(str(error)) from None
        return {row["key"]: row["tags"] for row in cleaned if len(row["tags"]) >= validate.MIN_TAGS}


def asked_for(cfg, task, row):
    """What a title's tags were bought for: the configured model, the spec, and the evidence it was shown."""
    return hashlib.sha256(json.dumps([cfg["provider"], cfg["model"], task.version, row["plot"]],
                                     ensure_ascii=False).encode()).hexdigest()


def generate(phase, cap, cfg=None, workers=1, kept=None, persist=None):
    """Fill missing batch outputs through the configured model, title by title.

    A batch output holds every title answered so far, each with `by` (who answered it); a rerun asks only
    the titles no output holds. A title no model could tag is left out and reported, and waits for a later
    run: it does not stop the day.

    `kept` is the paid-answers ledger's premise section (`pipeline/paid.py`): a title it answers for the same
    model, spec and evidence is written from it and not asked, and every new answer is added to it, with
    `persist` called after each, so a run that stops later loses nothing it paid for."""
    cfg = cfg or config()
    manifest = _json(os.path.join(phase, "manifest.json"))
    estimate = projected(manifest, cfg)
    if not cap > 0 or estimate > cap:
        raise RuntimeError(f"premise tags project ${estimate:.4f}, crossing the ${cap:.2f} daily cap")
    task = Task(cfg)
    out_dir = os.path.join(phase, "out")
    os.makedirs(out_dir, exist_ok=True)
    batches, where, pending, resumed = {}, {}, [], 0
    for index in range(manifest["batches"]):
        name = f"batch-{index:04d}.json"
        batch_in = _json(os.path.join(phase, "in", name))
        path = os.path.join(out_dir, name)
        done = {}
        if os.path.exists(path):
            wanted = {row["key"] for row in batch_in}
            done = {row["key"]: row for row in _json(path)
                    if row.get("key") in wanted and len(row.get("tags") or []) >= validate.MIN_TAGS}
        for row in batch_in:
            where[row["key"]] = name
            saved = (kept or {}).get(row["key"])
            if row["key"] not in done and saved and saved.get("ask") == asked_for(cfg, task, row):
                done[row["key"]] = {"key": row["key"], "tags": saved["tags"], "by": saved["by"]}
            if row["key"] not in done:
                pending.append(row)
        batches[name] = done
        resumed += len(done)
        if done:
            _write(path, list(done.values()))
    asks = {row["key"]: asked_for(cfg, task, row) for row in pending}

    def record(key, row):
        if "value" in row:
            name = where[key]
            batches[name][key] = {"key": key, "tags": row["value"], "by": row["by"]}
            _write(os.path.join(out_dir, name), list(batches[name].values()))
            if kept is not None:
                kept[key] = {"ask": asks[key], "tags": row["value"], "by": row["by"]}
                if persist is not None:
                    persist()

    budget = llm.Budget(cap)
    try:
        rows, calls = llm.generate(cfg, task, pending, budget, workers, record)
    except llm.OverBudget as error:
        raise GenerationError(str(error), budget.spent) from error
    usage = {field: sum(call["usage"][field] for call in calls)
             for field in ("inputTokens", "cachedTokens", "outputTokens", "reasoningTokens")}
    answered = [row for row in rows.values() if "value" in row]
    return {"provider": cfg["provider"], "model": cfg["model"], "mode": "online",
            "titles": manifest["titles"], "generated": len(answered), "resumed": resumed,
            "byModel": dict(sorted(collections.Counter(row["by"]["model"] for row in answered).items())),
            "untagged": sorted(key for key, row in rows.items() if "value" not in row),
            # Why: refused by every model, or answered without 8 usable tags. A title the provider could not
            # be reached for is untagged and in neither: it is asked again on the next run.
            "refused": sorted(key for key, row in rows.items() if row.get("refused")),
            "short": sorted(key for key, row in rows.items()
                            if "value" not in row and not row.get("refused") and not row.get("unavailable")),
            "calls": len(calls), **usage, "costUSD": round(sum(call["costUSD"] for call in calls), 6),
            "projectedSpendUSD": estimate, "spendCapUSD": cap}


def prepare(ctx, tags_path, token_ceiling):
    work = os.path.join(ctx.out_dir, "premise-increment")
    command = [sys.executable, os.path.join(REPO, "pipeline", "build_premise_worklist.py")]
    for path in ctx.paths(artifacts.COMBINED):
        command += ["--combined", path]
    command += ["--articles", ctx.path(artifacts.ARTICLES), "--changes",
                os.path.join(ctx.path(artifacts.CHANGES), "plan.json"), "--token-ceiling", str(token_ceiling),
                "--existing-tags", tags_path, "--out-dir", work]
    result = subprocess.run(command)
    if result.returncode:
        raise StageError(f"premise tags: worklist exited {result.returncode}")
    return work, _json(os.path.join(work, "gen", "manifest.json"))


def ensure_tags(ctx):
    target = ctx.path(artifacts.PREMISE_TAGS)
    if not os.path.exists(target):
        source = os.path.join(REPO, "data", artifacts.PREMISE_TAGS.filename)
        with open(source, "rb") as src:
            payload = src.read()
        os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
        with open(target, "wb") as dst:
            dst.write(payload)
    return target


def merge(ctx, phase, result, now):
    tags_path = ensure_tags(ctx)
    models = ", ".join(result["byModel"]) or result["model"]
    command = [sys.executable, os.path.join(REPO, "pipeline", "merge_premise_tags.py"),
               "--into", tags_path, "--phase", phase, "--note",
               f"daily new-title generation {now.date().isoformat()} with {models}"]
    completed = subprocess.run(command)
    if completed.returncode:
        raise StageError(f"premise tags: merge exited {completed.returncode}")
    value = _json(tags_path)
    # Who answered each title, so a mixed index can be found and re-run (#183).
    generated_by = dict(value.get("generatedBy") or {})
    out_dir = os.path.join(phase, "out")
    for name in sorted(os.listdir(out_dir)) if os.path.isdir(out_dir) else ():
        for row in _json(os.path.join(out_dir, name)):
            if row.get("by"):
                generated_by[row["key"]] = row["by"]
    value["generatedBy"] = dict(sorted(generated_by.items()))
    increments = list(value.get("dailyIncrements") or [])
    increments.append({"date": now.date().isoformat(), "model": models, "titles": result["titles"],
                       "generated": result["generated"], "untagged": len(result["untagged"]),
                       "inputTokens": result["inputTokens"], "outputTokens": result["outputTokens"],
                       "reasoningTokens": result["reasoningTokens"], "costUSD": result["costUSD"]})
    value["dailyIncrements"] = increments
    _write(tags_path, value)
    return tags_path


def _embed(url, texts):
    endpoint = url.rstrip("/") + "/embed/batch"
    request = urllib.request.Request(endpoint, data=json.dumps({"texts": texts}).encode(),
                                     headers={"content-type": "application/json"})
    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                return json.load(response)["vectors"]
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as error:
            if attempt == MAX_RETRIES - 1:
                raise StageError(f"premise tags: embed request failed after {MAX_RETRIES} attempts "
                                 f"({type(error).__name__})") from None
            time.sleep(2 ** attempt)


def extend_vectors(ctx, url, changed=(), add_missing=True):
    """Embed tag sets the premise blob lacks, and re-embed `changed` ones in place.

    A key in `changed` that already has a row gets a new vector in that same row, so the blob's key order and
    `labels-premise.json` stand. One without a row is appended like a missing key. `add_missing=False` embeds
    `changed` alone, so a run that only corrects rows touches nothing else.
    """
    tags = _json(ctx.path(artifacts.PREMISE_TAGS))["tags"]
    path = ctx.path(artifacts.PREMISE_VECTORS)
    try:
        count, dims, keys, blob, base = vector_blob.read(path, allow_legacy=False)
    except SystemExit as error:
        raise StageError(f"premise tags: {error}") from error
    if keys is None:
        raise StageError("premise tags: the live premise blob does not name its rows")
    position = {key: row for row, key in enumerate(keys)}
    restated = sorted(key for key in set(changed) if key in position)
    missing = sorted((set(tags) - set(keys)) if add_missing else (set(changed) - set(keys)))
    if not missing and not restated:
        return {"before": count, "embedded": 0, "reembedded": 0, "after": count}
    unknown = sorted(set(keys) - set(tags))
    if unknown:
        raise StageError(f"premise tags: {len(unknown)} vector rows have no tag strings")
    embed_canary.gate(url)
    rows = bytearray(blob[base:])
    order = restated + missing
    for start in range(0, len(order), 48):
        block = order[start:start + 48]
        vectors = _embed(url, [" ".join(tags[key]) for key in block])
        valid = (isinstance(vectors, list) and len(vectors) == len(block)
                 and all(isinstance(row, list) and len(row) == dims
                         and all(isinstance(value, int) and not isinstance(value, bool)
                                 and -128 <= value <= 127 for value in row)
                         for row in vectors))
        if not valid:
            raise StageError("premise tags: embedder returned the wrong premise-vector shape")
        for key, row in zip(block, vectors):
            packed = bytes((value + 256) % 256 for value in row)
            if key in position:
                rows[position[key] * dims:(position[key] + 1) * dims] = packed
            else:
                rows.extend(packed)
    all_keys = list(keys) + missing
    try:
        vector_blob.write(path, all_keys, bytes(rows), dims)
    except SystemExit as error:
        raise StageError(f"premise tags: {error}") from error
    if missing:
        ids = os.path.join(ctx.out_dir, "premise-increment", "premise-ids.json")
        _write(ids, all_keys)
        command = [sys.executable, os.path.join(REPO, "pipeline", "build_premise_labels.py"),
                   "--ids", ids, "--labels", ctx.path(artifacts.VECTOR_LABELS), "--blob", path,
                   "--out", ctx.path(artifacts.PREMISE_LABELS)]
        if subprocess.run(command).returncode:
            raise StageError("premise tags: premise-label rebuild failed")
    return {"before": count, "embedded": len(missing), "reembedded": len(restated), "after": len(all_keys)}


def committed_corrections(ctx):
    """Keys both files hold whose tags in `data/premise-tags-v2.json` differ from the run's copy, sorted.

    The rule: for a key both hold, the committed row wins. A run seeded from the published bundle carries the
    bundle's copy, which the store reads ahead of `data/`, so a fix committed there (#184) would otherwise
    never ship. It is safe because nothing else rewrites a key the committed file holds: a daily increment
    only adds titles neither file has (`build_premise_worklist.load_have`), and the merge refuses an existing
    key without `--overwrite`. So a difference on a shared key can only be a committed correction.

    Keys on one side only are left alone. One only the run holds is a daily title. One only the committed
    file holds is not a correction but a new title, and adding titles is the premise step's job, which checks
    them against the corpus first. No copy in the run means the store reads `data/` directly: nothing to do.
    """
    target = ctx.path(artifacts.PREMISE_TAGS)
    if not os.path.exists(target):
        return []
    committed = _json(os.path.join(REPO, "data", artifacts.PREMISE_TAGS.filename))["tags"]
    run = _json(target)["tags"]
    return sorted(key for key, values in committed.items() if key in run and run[key] != values)


def apply_corrections(ctx, url, keys):
    """Write the committed rows for `keys` into the run's tag copy and re-embed exactly those vectors."""
    target = ctx.path(artifacts.PREMISE_TAGS)
    committed = _json(os.path.join(REPO, "data", artifacts.PREMISE_TAGS.filename))["tags"]
    value = _json(target)
    tags = dict(value["tags"])
    tags.update({key: committed[key] for key in keys})
    value.update(tags=tags, count=len(tags))
    _write(target, value)
    vectors = (extend_vectors(ctx, url, changed=keys, add_missing=False)
               if os.path.exists(ctx.path(artifacts.PREMISE_VECTORS)) else None)
    return {"corrected": len(keys), "reembedded": vectors["reembedded"] if vectors else 0,
            "appended": vectors["embedded"] if vectors else 0, "keys": keys}


def stamp_metadata(ctx, result):
    if not result:
        return
    path = ctx.stamp_meta
    value = _json(path)
    tags = _json(ctx.path(artifacts.PREMISE_TAGS))
    value["premiseTags"] = {"coverage": tags.get("count", len(tags.get("tags") or {})),
                            "model": result["model"], "byModel": result.get("byModel"),
                            "selection": "added-or-regained-only",
                            "generatedThisRun": result["titles"],
                            "source": "Jev-classified story-premise/theme-subject sections"}
    _write(path, value)
