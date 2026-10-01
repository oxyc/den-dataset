#!/usr/bin/env python3
"""You Might Also Like: titles a fan would love, named by a model and matched to the store (oxyc/den-atlas#121).

For each title, the `fan_picks` model in `data/models.json` (Gemini Flash; asked through `lib/llm.py`) is told
the title, its year, whether it is a film or a series, and the lead of
its Wikipedia article — our own data, nothing from TMDB — and names up to 20 films or series a fan would also
love, in any genre, era or country. Each name is matched to a corpus title by the store's own Wikidata names;
a name that matches no title or more than one is dropped, never guessed. The seed, its curated franchise, its
other versions and its sequel links are dropped too: other rows show them.

  prepare  freeze every title's prompt inputs and the popularity order the run follows
  sample   a seeded sample stratified across popularity bands, as a keys file
  run      ask: `--online` (generateContent, retried) for small runs, else one Batch API job per chunk
  collect  poll the submitted batch jobs and record their answers
  match    match every answer's picks to the store, and report a slice's numbers
  export   the store's input: every asked title with its matched picks, in the model's order
  backfill ask the step's fallback model about titles the primary refused, into data/fan-picks-backfill.json

Every answer is appended as it arrives, with its token usage, cost and `modelVersion`; a rerun asks only what
has no answer. Spend is checked against the manifest's cap before anything is asked: a chunk is admitted only
while the recorded spend plus the chunk's projected cost, at the observed cost per title with a margin, stays
under it.

The key is read from the provider's environment variable (GEMINI_API_KEY) and sent only as a header.
"""
import argparse
import collections
import concurrent.futures
import datetime
import gzip
import hashlib
import json
import math
import os
import random
import re
import sys
import threading

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from lib import llm  # noqa: E402
from lib import llm_providers as providers  # noqa: E402
from store import aliases  # noqa: E402
from store.cards import display_title, release_year  # noqa: E402

SCHEMA = "fan-picks-run-v1"
EXPORT_SCHEMA = "fan-picks-v1"
#: Who answers, from `data/models.json`.
CFG = llm.step("fan_picks")
MODEL = CFG["model"]
THINKING = CFG["thinking"]
MAX_OUTPUT_TOKENS = CFG["maxOutputTokens"]
LEAD_CHARS = 1500
#: Standard price per token, in and out (thinking bills as output), from `lib/llm.py`'s table; the Batch
#: API is half.
PRICE_IN, PRICE_OUT = llm.price(MODEL)[:2]
BATCH_FACTOR = llm.BATCH_FACTOR
#: What a title cost in the pilot at standard price (351 in, 665 out), used until this run has its own.
PILOT_COST = 351 * PRICE_IN + 665 * PRICE_OUT
#: A chunk is reserved at the observed cost per title times this.
MARGIN = 1.3
#: A scheduled day is small, but a broken change plan must not turn an unattended run into a catalogue
#: purchase. The CLI may lower or deliberately raise this; the whole set must fit before the first call.
DAILY_SPEND_CAP = 1.0
#: Popularity bands the sample is stratified over and a report is broken down by: positions in the order.
BANDS = ((0, 1000), (1000, 3000), (3000, 6000), (6000, 10000), (10000, 20000), (20000, None))

DAILY_CHECKPOINT_SCHEMA = "fan-picks-daily-responses-v1"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def file_digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def read_gz_json(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, value, gz=False):
    opener = gzip.open if gz else open
    with opener(path + ".tmp", "wt", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False, sort_keys=True, indent=None if gz else 1)
    os.replace(path + ".tmp", path)


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def corpus_rows(path):
    """The corpus's rows by key, with the alias decisions applied as the store build applies them."""
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    aliases.apply(rows)
    return {row["key"]: row for row in rows}


def media_kind(key):
    return "series" if key.startswith("tv:") else "film"


def card_year(facts):
    return release_year(facts.get("released") or facts.get("started"))


# --- prepare --------------------------------------------------------------------------------------------

def lead_of(text):
    """The article's first section, cut at a sentence end to at most LEAD_CHARS."""
    lead = (text or "").split("\n\n==")[0].strip()
    if len(lead) > LEAD_CHARS:
        cut = lead[:LEAD_CHARS]
        lead = cut.rsplit(". ", 1)[0] + "." if ". " in cut else cut
    return lead


def prompt(title):
    kind = media_kind(title["key"])
    when = f"{title['year']}, {kind}" if title.get("year") else kind
    lead = title.get("lead") or ""
    if lead and not lead.endswith("."):
        lead += "."
    # The compact answer (#187, format B): short keys and one array per pick, 43% cheaper than keyed objects
    # for the same picks kept (300 titles, blind-judged equal). `parse` still reads the keyed shape.
    return (f"A friend loved {title['title']} ({when}). {lead} ".replace("  ", " ")
            + "Name up to 20 films or series they would also love — any genre, era or country; taste, not "
              "similarity. If you don't know this title well, say so and name only picks you're confident in. "
              'Return only JSON: {"k":bool,"p":[["Title",1999,"f"|"s"]]}')


def question(title, cfg=CFG):
    """The request for one title, as `lib/llm.py` sends it."""
    return llm.request(prompt(title), cfg["maxOutputTokens"])


def request_body(title, cfg=CFG):
    """What goes on the wire for one title: the request in the configured provider's dialect."""
    return providers.PROVIDERS[cfg["provider"]].body(cfg, question(title, cfg))


def popularity_order(pool, keys):
    """Most popular first within its own type (share of the type), then unranked, ties by key — the order
    the More Like This cascade ran in, frozen in its pool file."""
    def rank(key):
        item = pool.get("popularity", {}).get(key) or {}
        return (item["rank"] / item["typeSize"] if item.get("rank") and item.get("typeSize") else 2.0, key)
    return sorted(keys, key=rank)


def prepare(corpus_path, articles_path, pool_path, work, cap):
    rows = corpus_rows(corpus_path)
    leads = {}
    with open(articles_path, encoding="utf-8") as fh:
        for line in fh:
            article = json.loads(line)
            key = f"{article['mediaType']}:{article['tmdbId']}"
            if key in rows:
                leads[key] = lead_of(article.get("text"))
    titles = {}
    for key, row in rows.items():
        facts = row.get("facts") or {}
        name = display_title(facts.get("titles"))
        if name:
            titles[key] = {"key": key, "title": name, "year": card_year(facts), "lead": leads.get(key, "")}
    order = popularity_order(read_gz_json(pool_path), titles)
    os.makedirs(work, exist_ok=True)
    manifest = {
        "schema": SCHEMA, "issue": "oxyc/den-atlas#121", "model": MODEL, "thinkingLevel": THINKING,
        "maxOutputTokens": MAX_OUTPUT_TOKENS, "leadChars": LEAD_CHARS,
        "promptSha256": digest(prompt({"key": "movie:0", "title": "X", "year": 2000, "lead": "L"})),
        "inputs": {"corpus": file_digest(corpus_path), "articles": file_digest(articles_path),
                   "pool": file_digest(pool_path)},
        "files": {"titles.json.gz": digest(titles), "order.json": digest(order)},
        "pricePerToken": {"in": PRICE_IN, "out": PRICE_OUT, "batchFactor": BATCH_FACTOR},
        "spendCapUSD": cap,
    }
    path = os.path.join(work, "manifest.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            if json.load(fh) != manifest:
                raise SystemExit(f"{path}: refusing to replace a different manifest")
    else:
        write_json(os.path.join(work, "titles.json.gz"), titles, gz=True)
        write_json(os.path.join(work, "order.json"), order)
        write_json(path, manifest)
    return {"titles": len(titles), "withLead": sum(bool(t["lead"]) for t in titles.values()),
            "unnamed": len(rows) - len(titles)}


class Work:
    """A prepared run: its manifest and frozen inputs, each checked against the hash it was frozen with."""

    def __init__(self, work):
        self.dir = work
        with open(os.path.join(work, "manifest.json"), encoding="utf-8") as fh:
            self.manifest = json.load(fh)
        if self.manifest.get("schema") != SCHEMA or self.manifest.get("model") != MODEL:
            raise SystemExit(f"{work}: incompatible manifest")
        if digest(prompt({"key": "movie:0", "title": "X", "year": 2000, "lead": "L"})) != self.manifest["promptSha256"]:
            raise SystemExit("the prompt changed after the manifest was frozen")
        self.titles = read_gz_json(os.path.join(work, "titles.json.gz"))
        with open(os.path.join(work, "order.json"), encoding="utf-8") as fh:
            self.order = json.load(fh)
        if digest(self.titles) != self.manifest["files"]["titles.json.gz"] \
                or digest(self.order) != self.manifest["files"]["order.json"]:
            raise SystemExit(f"{work}: a frozen input changed after the manifest")
        self.position = {key: i for i, key in enumerate(self.order)}

    def path(self, name):
        return os.path.join(self.dir, name)

    def answers(self):
        """Recorded answers by (key, ask); the first one wins."""
        out = {}
        for answer in read_jsonl(self.path("answers.jsonl")):
            out.setdefault((answer["key"], answer.get("ask", 0)), answer)
        return out

    def spent(self):
        return sum(a.get("costUSD", 0.0) for a in read_jsonl(self.path("answers.jsonl"))) \
            + sum(e.get("costUSD", 0.0) for e in read_jsonl(self.path("errors.jsonl")))

    def per_title_estimate(self):
        """Standard-price cost of one ask: this run's mean once it has 50 answers, else the pilot's."""
        answers = read_jsonl(self.path("answers.jsonl"))
        if len(answers) < 50:
            return PILOT_COST
        return sum(standard_cost(a["usage"]) for a in answers) / len(answers)


def standard_cost(usage):
    """A recorded answer's cost at standard price. Rows keep Gemini's own usage names."""
    if "inputTokens" in usage:
        return llm.cost(CFG, usage, "online")
    return llm.cost(CFG, {"inputTokens": usage.get("promptTokenCount") or 0, "cachedTokens": 0,
                          "outputTokens": usage.get("candidatesTokenCount") or 0,
                          "reasoningTokens": usage.get("thoughtsTokenCount") or 0}, "online")


# --- sample ---------------------------------------------------------------------------------------------

def band_of(position):
    for i, (lo, hi) in enumerate(BANDS):
        if position >= lo and (hi is None or position < hi):
            return i
    raise ValueError(position)


def band_name(i):
    lo, hi = BANDS[i]
    return f"{lo:,}–{hi:,}" if hi else f"{lo:,}+"


def sample(work, per_band, seed, out):
    w = Work(work)
    rng = random.Random(seed)
    keys = []
    for i in range(len(BANDS)):
        members = [k for k in w.order if band_of(w.position[k]) == i]
        keys += sorted(rng.sample(members, min(per_band, len(members))), key=w.position.get)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(keys, fh, indent=1)
    return {"out": out, "keys": len(keys)}


# --- asking ---------------------------------------------------------------------------------------------

#: Format B's one-letter types, and the words a model sometimes writes there instead.
SHORT_TYPES = {"f": "film", "s": "series", "film": "film", "series": "series"}


def parse(text):
    """The answer's `known` and picks, or ValueError. A pick needs a title; year and type are kept as given.

    Both answer shapes are read: the compact `{"k", "p": [[title, year, "f"|"s"]]}` the prompt asks for now,
    and the keyed `{"known", "picks": [{"title", "year", "type"}]}` every answer before it is stored in."""
    found = re.search(r"\{.*\}", text or "", re.S)
    value = json.loads(found.group(0) if found else text)
    if "p" in value:
        picks = [{"title": pick[0], "year": pick[1] if len(pick) > 1 else None,
                  "type": SHORT_TYPES.get(pick[2]) if len(pick) > 2 and isinstance(pick[2], str) else None}
                 for pick in value["p"] if isinstance(pick, list) and pick] \
            if isinstance(value["p"], list) else None
        known = value.get("k")
    else:
        picks, known = value.get("picks"), value.get("known")
    if not isinstance(picks, list):
        raise ValueError("no picks list")
    kept = []
    for pick in picks:
        if isinstance(pick, dict) and isinstance(pick.get("title"), str) and pick["title"].strip():
            year = pick.get("year")
            kept.append({"title": pick["title"].strip(),
                         "year": year if isinstance(year, int) and not isinstance(year, bool) else None,
                         "type": pick.get("type") if pick.get("type") in ("film", "series") else None})
    return bool(known), kept


def record(response, key, ask, mode, batch=None):
    """An answer (or an error) row from one generateContent response."""
    result = providers.PROVIDERS["gemini"].read(response, {})
    result["costUSD"] = llm.cost(CFG, result["usage"], mode)
    return recorded(result, key, ask, mode, batch)


def recorded(result, key, ask, mode, batch=None, cfg=CFG):
    """An answer (or an error) row from one `lib/llm.py` answer. Gemini's rows keep its own usage names; a
    row another model answered (the fallback) says which provider and model it was."""
    raw = result.get("raw") or {}
    if cfg["provider"] == "gemini":
        usage = raw.get("usageMetadata") or {}
        usage = {k: usage.get(k) for k in ("promptTokenCount", "candidatesTokenCount", "thoughtsTokenCount",
                                           "totalTokenCount")}
    else:
        usage = result["usage"]
    row = {"key": key, "ask": ask, "mode": mode, "modelVersion": result["modelVersion"], "usage": usage,
           "costUSD": result["costUSD"],
           "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    if (cfg["provider"], cfg["model"]) != (CFG["provider"], CFG["model"]):
        row["provider"], row["model"] = cfg["provider"], cfg["model"]
    if batch:
        row["batch"] = batch
    text = result["text"]
    try:
        row["known"], row["picks"] = parse(text)
        row["text"] = text
        return row, None
    except (ValueError, json.JSONDecodeError) as exc:
        candidates = raw.get("candidates") if cfg["provider"] == "gemini" else None
        finish = ((candidates or [{}])[0]).get("finishReason") if cfg["provider"] == "gemini" else result["why"]
        row["error"] = f"{type(exc).__name__}: {exc}; finishReason={finish}"[:300]
        row["text"] = text[:2000]
        return None, row


def todo(w, keys, asks):
    answered = w.answers()
    pending = submitted(w)
    return [(k, n) for k in keys for n in range(asks) if (k, n) not in answered and (k, n) not in pending]


def admit(w, items, mode, run_cap=None):
    """The longest prefix of `items` whose projected cost fits under the manifest's cap, and under `run_cap`
    for this invocation alone when one is given, and the projection."""
    cap, spent = w.manifest["spendCapUSD"], w.spent()
    each = w.per_title_estimate() * MARGIN * (BATCH_FACTOR if mode == "batch" else 1.0)
    room = cap - spent - reserved(w)
    if run_cap is not None:
        room = min(room, run_cap)
    fits = max(0, int(room / each))
    return items[:fits], {"spentUSD": round(spent, 4), "reservedUSD": round(reserved(w), 4),
                          "perAskUSD": round(each, 6), "capUSD": cap, "runCapUSD": run_cap}


def run_online(w, items, workers=8, log=sys.stderr):
    lock = threading.Lock()

    def one(item):
        key, ask = item
        answer, error, _accepted = generate_online(w.titles[key], key, ask)
        with lock:
            name = "answers.jsonl" if answer else "errors.jsonl"
            with open(w.path(name), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(answer or error, ensure_ascii=False, sort_keys=True) + "\n")
        return answer is not None

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        ok = sum(pool.map(one, items))
    print(json.dumps({"asked": len(items), "answered": ok, "spentUSD": round(w.spent(), 4)}), file=log)
    return ok


def generate_online(title, key, ask=0, cfg=CFG):
    """One retried ask through `lib/llm.py`: `(answer, error, accepted)`.

    `accepted` separates an HTTP/transport failure from a response received from the provider. A safety
    refusal or malformed response is still an error. A provider-accepted empty response is a durable
    asked-empty anchor; malformed non-empty output remains unasked.
    """
    try:
        result = llm.online(cfg, question(title, cfg))
    except providers.Unavailable as exc:
        return None, {"key": key, "ask": ask, "mode": "online", "error": str(exc), "costUSD": 0.0}, False
    answer, error = recorded(result, key, ask, "online", cfg=cfg)
    return answer, error, True


def answer_title(title, key, ask=0, cfg=CFG):
    """`generate_online`, with what #183 decided for an answer that is not one: a refused, empty or malformed
    answer is asked once more, and then by the step's fallback model. Only a title the fallback cannot answer
    either keeps the refusal (#170's asked-empty rule). The row's `costUSD` is every attempt's.

    A fallback that cannot be reached (no key, an outage) leaves the primary's refusal standing, as it stood
    before there was a fallback, rather than refusing the whole day."""
    spent, last = 0.0, None
    attempts = [cfg, cfg] + ([cfg["fallback"]] if cfg.get("fallback") else [])
    for n, attempt in enumerate(attempts):
        answer, error, accepted = generate_online(title, key, ask, attempt)
        if not accepted:
            if last is None or n < 2:
                return answer, error, accepted
            last[1]["fallbackError"] = error.get("error")
            return last
        row = answer or error
        spent += row.get("costUSD", 0.0)
        row["costUSD"] = spent
        last = answer, error, accepted
        if answer is not None:
            return last
    return last


# --- batches --------------------------------------------------------------------------------------------

def batches(w):
    path = w.path("batches.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_batches(w, value):
    write_json(w.path("batches.json"), value)


def submitted(w):
    """(key, ask) of every item in a batch job not yet collected."""
    return {(k, n) for b in batches(w) if b["state"] == "submitted" for k, n in b["items"]}


def reserved(w):
    return sum(b["reservedUSD"] for b in batches(w) if b["state"] == "submitted")


def submit(w, items, label, reserve):
    os.makedirs(w.path("batch-inputs"), exist_ok=True)
    requests = {f"{key}#{ask}": question(w.titles[key]) for key, ask in items}
    try:
        job = providers.PROVIDERS[CFG["provider"]].submit(CFG, requests, f"fan-picks-{label}",
                                                          w.path(f"batch-inputs/{label}.jsonl"))
    except providers.Unavailable as exc:
        # The API says why (an enqueued-token limit, a bad file); nothing is recorded, so a rerun resubmits.
        raise SystemExit(f"batch {label} refused: {exc}")
    entry = {"name": job["name"], "label": label, "file": job.get("file"), "items": [list(i) for i in items],
             "state": "submitted", "reservedUSD": reserve,
             "submittedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    save_batches(w, batches(w) + [entry])
    return entry


def collect(w, log=sys.stderr):
    """Poll every submitted job; record a finished one's answers. Returns the jobs' states."""
    jobs = batches(w)
    states = {}
    for job in jobs:
        if job["state"] != "submitted":
            states[job["label"]] = job["state"]
            continue
        wanted = {f"{k}#{n}" for k, n in job["items"]}
        state, out = providers.PROVIDERS[CFG["provider"]].poll(CFG, job, wanted)
        states[job["label"]] = state
        if state in ("failed", "expired"):
            job["state"] = state
            job["error"] = out.get("error") if state == "failed" else None
            continue
        if state != "done":
            continue
        answers, errors = [], []
        for label, result in out.items():
            wanted.discard(label)
            key, _, ask = label.rpartition("#")
            if "error" not in result:
                result["costUSD"] = llm.cost(CFG, result["usage"], "batch")
                answer, error = recorded(result, key, int(ask), "batch", job["name"])
            else:
                answer, error = None, {"key": key, "ask": int(ask), "mode": "batch", "batch": job["name"],
                                       "error": result["error"], "costUSD": 0.0}
            (answers if answer else errors).append(answer or error)
        errors += [{"key": label.rpartition("#")[0], "ask": int(label.rpartition("#")[2]), "mode": "batch",
                    "batch": job["name"], "error": "no response in the results file", "costUSD": 0.0}
                   for label in sorted(wanted)]
        for name, rows in (("answers.jsonl", answers), ("errors.jsonl", errors)):
            with open(w.path(name), "a", encoding="utf-8") as fh:
                fh.writelines(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)
        job["state"] = "collected"
        job["answered"], job["errors"] = len(answers), len(errors)
        job["costUSD"] = round(sum(r.get("costUSD", 0.0) for r in answers + errors), 6)
        states[job["label"]] = "collected"
        print(json.dumps({"collected": job["label"], "answered": len(answers), "errors": len(errors)}), file=log)
    save_batches(w, jobs)
    return states


def run(work, keys_path=None, start=0, count=None, online=False, asks=1, workers=8, retry_errors=False,
        label=None, run_cap=None, log=sys.stderr):
    w = Work(work)
    if keys_path:
        with open(keys_path, encoding="utf-8") as fh:
            keys = [k for k in json.load(fh) if k in w.titles]
    else:
        keys = w.order[start:start + count if count else None]
    if not retry_errors:
        failed = {(e["key"], e.get("ask", 0)) for e in read_jsonl(w.path("errors.jsonl"))}
        items = [i for i in todo(w, keys, asks) if i not in failed]
    else:
        items = todo(w, keys, asks)
    mode = "online" if online else "batch"
    admitted, budget = admit(w, items, mode, run_cap)
    summary = {"requested": len(items), "admitted": len(admitted), **budget}
    if len(admitted) < len(items):
        summary["stopped"] = "cap: the rest does not fit under the spend cap"
    if not admitted:
        return summary
    if online:
        summary["answered"] = run_online(w, admitted, workers, log)
    else:
        reserve = round(len(admitted) * budget["perAskUSD"], 4)
        entry = submit(w, admitted, label or f"{start}-{start + len(admitted)}", reserve)
        summary["batch"] = {k: entry[k] for k in ("name", "label", "reservedUSD")}
    summary["spentUSD"] = round(w.spent(), 4)
    return summary


# --- match ----------------------------------------------------------------------------------------------

NUMBERS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
           "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty"]
WORD_TO_DIGIT = {w: str(i) for i, w in enumerate(NUMBERS)}
DIGIT_TO_WORD = {str(i): w for i, w in enumerate(NUMBERS)}
#: How far a pick's year may be from the store's and still name that title.
YEAR_SLACK = 2
_PAREN = re.compile(r"^(.*?)\s*\(([^()]*)\)\s*$")
_POSSESSIVE = re.compile(r"^(?:[\w.\-]+\s){0,2}[\w.\-]+['’]s\s+(.+)$")


def _swapped(folded):
    words = folded.split(" ")
    return {" ".join(WORD_TO_DIGIT.get(x, x) for x in words), " ".join(DIGIT_TO_WORD.get(x, x) for x in words)}


def name_tiers(title):
    """The folded names a written title may stand for, in tiers tried in turn: as written (with a trailing
    parenthetical or `A / B` read as either name), then with numbers as digits or words, then without a
    leading possessive ("Kurosawa's Dreams")."""
    written = {title.strip()}
    found = _PAREN.match(title.strip())
    if found:
        written |= {found.group(1), found.group(2)}
    for name in list(written):
        written |= set(re.split(r"\s+/\s+|\s+a\.?k\.?a\.?\s+", name, flags=re.I))
    first = {aliases.hard_fold(n) for n in written} - {""}
    second = {s for n in first for s in _swapped(n)} - first
    stripped = {m.group(1) for n in written for m in [_POSSESSIVE.match(n.strip())] if m}
    third = {aliases.hard_fold(n) for n in stripped} - {""}
    third |= {s for n in third for s in _swapped(n)}
    return [t for t in (first, second, third - first - second) if t]


class Names:
    """Every corpus title's names, folded, by type: its English and original labels and its aliases (after
    the alias decisions), each also without a disambiguating parenthetical."""

    def __init__(self, rows):
        self.by_name = collections.defaultdict(list)
        for key, row in rows.items():
            facts = row.get("facts") or {}
            titles = facts.get("titles") or {}
            names = {titles.get("en"), titles.get("orig"), *(titles.get("aliases") or [])}
            names |= {display_title({"en": n}) for n in list(names) if isinstance(n, str)}
            year = card_year(facts)
            for folded in {aliases.hard_fold(n) for n in names if isinstance(n, str) and n.strip()} - {""}:
                self.by_name[(folded, media_kind(key))].append((key, year))

    def resolve(self, pick):
        """`("matched", key)`, `("ambiguous", None)` or `("unmatched", None)`.

        Within a name tier, the titles of the type within one year of the pick's, then within `YEAR_SLACK`,
        then — only when every title carrying the name has no year in the store — those. The first of
        those with any title decides: one is the match, more is ambiguous and dropped. The tighter window
        comes first so a remake two years off never makes the original ambiguous. A pick with no year
        takes the name's titles whatever their years.

        A dated title further off is not taken even when it is the only one: measured on 41,583 answers,
        such joins were mostly another work of the same name that the store does not hold (the 1947 *The
        Fugitive* for the 1993 one, the 2001 *Metropolis* for the 1927 one)."""
        kind = pick.get("type") or "film"
        year = pick.get("year")
        for tier in name_tiers(pick["title"]):
            found = {key: y for name in tier for key, y in self.by_name.get((name, kind), ())}
            if year is None:
                windows = (list(found),)
            else:
                def within(slack):
                    return [key for key, y in found.items() if y is not None and abs(y - year) <= slack]
                undated = list(found) if all(y is None for y in found.values()) else []
                windows = (within(1), within(YEAR_SLACK), undated)
            for candidates in windows:
                if len(candidates) == 1:
                    return "matched", candidates[0]
                if candidates:
                    return "ambiguous", None
        return "unmatched", None


def related(rows, franchises, follows):
    """Per title, what other rows own: its curated franchise's members, its other versions and its sequel
    links (both directions)."""
    members = collections.defaultdict(set)
    for fid, group in (franchises.get("franchises") or {}).items():
        for member in group.get("members") or []:
            members[fid].add(member["key"])
    out = collections.defaultdict(set)
    for key, row in rows.items():
        facts = row.get("facts") or {}
        primary = ((franchises.get("titles") or {}).get(key) or {}).get("primary")
        out[key] |= members.get(primary, set())
        out[key] |= {v["key"] for v in facts.get("otherVersions") or [] if isinstance(v, dict)}
        for qid in (facts.get("follows") or []) + (facts.get("followedBy") or []):
            for other in follows.get(qid, ()):
                if other != key:
                    out[key].add(other)
                    out[other].add(key)
    for key in out:
        out[key].discard(key)
    return out


def sequel_keys(rows, seeds=None):
    """Relevant follows/followedBy Q-ids' corpus keys, through the shared response cache.

    A full match asks every link. A daily match asks only links out of its new seeds plus each seed's own
    Wikidata item, which is enough to find links into it too and avoids a catalogue-wide lookup for four titles.
    """
    from lib import wikidata, wikidata_facts
    selected = rows.values() if seeds is None else (rows[key] for key in seeds)
    qids = {q for row in selected for f in ("follows", "followedBy")
            for q in (row.get("facts") or {}).get(f) or []}
    if seeds is not None:
        qids |= {rows[key].get("source", {}).get("wikidataItem") for key in seeds}
        qids.discard(None)
    return wikidata_facts.tmdb_keys(qids, wikidata.cache_for())


def match_answer(answer, seed, names, owned):
    """Each pick with its status: matched (with key), ambiguous, unmatched, or dropped for a reason."""
    out, seen = [], set()
    for pick in answer.get("picks") or []:
        status, key = names.resolve(pick)
        if status == "matched":
            if key == seed:
                status = "seed"
            elif key in owned:
                status = "franchise or version"
            elif key in seen:
                status = "duplicate"
            seen.add(key)
        out.append({**pick, "status": status, **({"key": key} if key else {})})
    return out


def accepted_empty(error):
    """A billed provider response with no candidate text, not a transport or parsing failure."""
    return isinstance(error, dict) and isinstance(error.get("usage"), dict) \
        and isinstance(error.get("text"), str) and not error["text"].strip()


def daily_checkpoint_path(out):
    return out + ".daily-checkpoint.json"


def load_daily_checkpoint(path):
    if not os.path.exists(path):
        return {"schema": DAILY_CHECKPOINT_SCHEMA, "model": MODEL, "responses": {}}
    try:
        with open(path, encoding="utf-8") as fh:
            value = json.load(fh)
    except (OSError, ValueError) as error:
        raise RuntimeError(f"daily fan picks: unreadable response checkpoint {path}: {error}") from error
    if not isinstance(value, dict) or (value.get("schema"), value.get("model")) != (DAILY_CHECKPOINT_SCHEMA, MODEL) \
            or not isinstance(value.get("responses"), dict):
        raise RuntimeError(f"daily fan picks: incompatible response checkpoint {path}")
    return value


def fingerprint(title, cfg=CFG):
    """What an answer was bought for: who was asked, and the request on the wire. A kept answer is reused
    only for the same fingerprint, so a new prompt or model is the one way to pay for a title again."""
    return digest({"provider": cfg["provider"], "model": cfg["model"], "request": request_body(title, cfg)})


def reusable(saved, wanted):
    """A kept response is an answer, or a provider's accepted empty answer, bought for this request. A
    malformed one is asked again: the title would otherwise never get picks."""
    return isinstance(saved, dict) and saved.get("requestSha256") == wanted and saved.get("accepted") is True \
        and (saved.get("answer") is not None or accepted_empty(saved.get("error")))


def daily_update(corpus_path, articles_path, franchises_path, existing_path, out, keys, workers=8,
                 generate=answer_title, follows=None, max_spend=DAILY_SPEND_CAP, backfill=None,
                 kept=None, persist=None):
    """Ask the fan-picks model online for new daily titles, match with the full run's rule, and merge the
    durable input.

    Existing anchors and picks that no longer join the current corpus are removed before the store sees
    them. A provider-accepted empty answer is asked-empty, while malformed non-empty output remains unasked;
    a transport failure refuses the update so an outage can never become `fan_picks_a`. The whole request
    set must fit the invocation's projected spend cap before any request is made.

    `backfill` is answers bought outside the daily job (`data/fan-picks-backfill.json`): each is matched into
    a title the input has no picks for, so a title the primary model refused gets the fallback's picks.

    `kept` is the paid-answers ledger's fan-picks section (`pipeline/paid.py`): every accepted response by
    title, reused while its fingerprint matches, and `persist` is called after each one is added, so a run
    that stops later loses nothing it paid for. Without it, a checkpoint file beside `out` does the same for
    one out-dir and is removed once the update succeeds.
    """
    rows = corpus_rows(corpus_path)
    if not os.path.exists(existing_path):
        raise RuntimeError(f"{existing_path}: no existing fan picks to merge; publish it in the corpus bundle first")
    with open(existing_path, encoding="utf-8") as fh:
        previous = json.load(fh)
    anchors = previous.get("anchors")
    if not isinstance(anchors, dict):
        raise RuntimeError(f"{existing_path}: no anchors object")
    current = set(rows)
    malformed = next(((key, picks) for key, picks in anchors.items()
                      if not isinstance(key, str) or not isinstance(picks, list)
                      or any(not isinstance(pick, str) for pick in picks)), None)
    if malformed:
        raise RuntimeError(f"{existing_path}: malformed anchor {malformed[0]!r}")
    anchors = {key: list(dict.fromkeys(pick for pick in picks if pick in current and pick != key))
               for key, picks in anchors.items() if key in current}

    # `changes.added` is the discovery/change-plan set. A tags-only title with no article can be admitted
    # there but still have no corpus/store row after the model and composition stages. There is nowhere to
    # attach `fan_picks_a` for such a key, so report it and ask only titles the store will actually carry.
    keys = list(dict.fromkeys(keys))
    not_in_corpus = [key for key in keys if key not in rows]
    corpus_keys = [key for key in keys if key in rows]
    wanted = set(corpus_keys)
    leads = {}
    if os.path.exists(articles_path):
        with open(articles_path, encoding="utf-8") as fh:
            for line in fh:
                article = json.loads(line)
                key = f"{article['mediaType']}:{article['tmdbId']}"
                if key in wanted:
                    leads[key] = lead_of(article.get("text"))
    titles = {}
    for key in corpus_keys:
        if key in anchors:
            continue
        row = rows[key]
        facts = row.get("facts") or {}
        title = display_title(facts.get("titles"))
        if not title:
            raise RuntimeError(f"daily fan picks: {key} has no store title to ask Gemini about")
        titles[key] = {"key": key, "title": title, "year": card_year(facts), "lead": leads.get(key, "")}

    if not math.isfinite(max_spend) or max_spend <= 0:
        raise ValueError("daily fan picks: max spend must be a positive finite dollar amount")
    projected = len(titles) * PILOT_COST * MARGIN
    if projected > max_spend:
        raise RuntimeError(f"daily fan picks: refusing {len(titles)} requests: their ${projected:.4f} "
                           f"projected spend crosses the ${max_spend:.2f} per-run cap")

    answered, empty_answers, parse_errors, unavailable = {}, {}, {}, {}
    checkpoint_path = daily_checkpoint_path(out)
    checkpoint = load_daily_checkpoint(checkpoint_path) if kept is None else {"responses": kept}
    fingerprints = {key: fingerprint(title) for key, title in titles.items()}

    def classify(key, result):
        answer, error, accepted = result
        if not accepted:
            unavailable[key] = error
        elif answer is not None:
            answered[key] = answer
        elif accepted_empty(error):
            empty_answers[key] = error
        else:
            parse_errors[key] = error

    pending = {}
    resumed = 0
    for key, title in titles.items():
        saved = checkpoint["responses"].get(key)
        if reusable(saved, fingerprints[key]):
            classify(key, (saved.get("answer"), saved.get("error"), True))
            resumed += 1
        else:
            pending[key] = title

    def one(item):
        key, title = item
        return key, generate(title, key, 0)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(one, item) for item in pending.items()]
        for future in concurrent.futures.as_completed(futures):
            key, result = future.result()
            answer, error, accepted = result
            classify(key, result)
            if accepted:
                checkpoint["responses"][key] = {
                    "requestSha256": fingerprints[key], "accepted": True,
                    "answer": answer, "error": error,
                }
                if kept is None:
                    write_json(checkpoint_path, checkpoint)
                elif persist is not None:
                    persist()
    if unavailable:
        first = next(iter(unavailable.items()))
        raise RuntimeError(f"daily fan picks: Gemini unreachable for {len(unavailable)} title(s); "
                           f"{first[0]}: {first[1].get('error')}")

    backfilled = {key: answer for key, answer in (backfill or {}).items()
                  if key in rows and key not in titles and not anchors.get(key)}
    if titles or backfilled:
        with open(franchises_path, encoding="utf-8") as fh:
            franchises = json.load(fh)
        names = Names(rows)
        seeds = set(titles) | set(backfilled)
        owned = related(rows, franchises, sequel_keys(rows, seeds) if follows is None else follows)
        for key, answer in [*answered.items(), *backfilled.items()]:
            matched = match_answer(answer, key, names, owned.get(key, set()))
            anchors[key] = merged([matched])
        for key in empty_answers:
            anchors[key] = []

    costs = [row.get("costUSD", 0.0)
             for row in [*answered.values(), *empty_answers.values(), *parse_errors.values()]]
    value = {"schema": EXPORT_SCHEMA, "issue": "oxyc/den-atlas#121", "model": MODEL,
             "count": len(anchors), "anchors": dict(sorted(anchors.items()))}
    write_json(out, value)
    if kept is None and os.path.exists(checkpoint_path):
        os.remove(checkpoint_path)
    return {"asked": len(titles), "answered": len(answered), "emptyAnswers": len(empty_answers),
            "parseErrors": len(parse_errors),
            "generated": len(pending), "resumed": resumed,
            "notInCorpus": not_in_corpus, "backfilled": len(backfilled),
            "anchors": len(anchors), "picks": sum(len(v) for v in anchors.values()),
            "costUSD": round(sum(costs), 6), "projectedSpendUSD": round(projected, 6),
            "spendCapUSD": max_spend}


#: Answers bought outside the daily job for titles the input has no picks for, merged by every daily run.
BACKFILL = os.path.join(REPO, "data", "fan-picks-backfill.json")
BACKFILL_SCHEMA = "fan-picks-backfill-v1"


def load_backfill(path=BACKFILL):
    """`{key: answer}` from the committed backfill, or nothing when there is none."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        value = json.load(fh)
    if value.get("schema") != BACKFILL_SCHEMA or not isinstance(value.get("answers"), dict):
        raise RuntimeError(f"{path}: not a {BACKFILL_SCHEMA} file")
    return value["answers"]


def backfill(work, keys_path, out=BACKFILL, max_spend=1.0, cfg=None, log=sys.stderr):
    """Ask the fallback model about `keys_path`'s titles with the full run's frozen inputs, and keep each
    answer in `out` for the daily job to match (oxyc/den-dataset#183: the titles Gemini always refused).

    A key `out` already answers is not asked again; one it records as refused or malformed is. Every call is
    logged with its cost and checked against `max_spend` before it is sent."""
    w, cfg = Work(work), cfg or CFG["fallback"]
    with open(keys_path, encoding="utf-8") as fh:
        keys = [key for key in json.load(fh) if key in w.titles]
    value = {"schema": BACKFILL_SCHEMA, "issue": "oxyc/den-dataset#183", "answers": {}, "refused": {}}
    if os.path.exists(out):
        with open(out, encoding="utf-8") as fh:
            value = json.load(fh)
    budget = llm.Budget(max_spend)
    for key in keys:
        if key in value["answers"]:
            continue
        try:
            result = llm.online(cfg, question(w.titles[key], cfg), budget)
        except providers.Unavailable as exc:
            print(json.dumps({"key": key, "error": str(exc)}), file=log)
            continue
        answer, error = recorded(result, key, 0, "online", cfg=cfg)
        row = answer or error
        print(json.dumps({"key": key, "answered": answer is not None, "costUSD": round(row["costUSD"], 6),
                          "spentUSD": round(budget.spent, 6)}), file=log)
        keep = ("provider", "model", "modelVersion", "at", "costUSD")
        if answer is not None:
            value["answers"][key] = {**{k: answer.get(k) for k in keep}, "known": answer["known"],
                                     "picks": answer["picks"]}
            value["refused"].pop(key, None)
        else:
            value["refused"][key] = {**{k: row.get(k) for k in keep}, "error": row.get("error")}
        value["answers"] = dict(sorted(value["answers"].items()))
        value["refused"] = dict(sorted(value["refused"].items()))
        write_json(out, value)
    return {"out": out, "answers": len(value["answers"]), "refused": len(value["refused"]),
            "spentUSD": round(budget.spent, 6)}


def match(work, corpus_path, franchises_path, follows_path=None):
    w = Work(work)
    rows = corpus_rows(corpus_path)
    with open(franchises_path, encoding="utf-8") as fh:
        franchises = json.load(fh)
    if follows_path and os.path.exists(follows_path):
        with open(follows_path, encoding="utf-8") as fh:
            follows = json.load(fh)
    else:
        follows = sequel_keys(rows)
        if follows_path:
            write_json(follows_path, follows)
    names, owned = Names(rows), related(rows, franchises, follows)
    matched = {}
    for (key, ask), answer in sorted(w.answers().items()):
        matched.setdefault(key, {"known": answer.get("known"), "asks": []})
        matched[key]["asks"].append(match_answer(answer, key, names, owned.get(key, set())))
    write_json(w.path("matched.json.gz"), matched, gz=True)
    return {"titles": len(matched), "out": w.path("matched.json.gz")}


def merged(asks):
    """One ask's matched keys in its order; with two, the keys both named first, in the first ask's order."""
    lists = [[p["key"] for p in ask if p["status"] == "matched"] for ask in asks]
    if len(lists) == 1:
        return lists[0]
    both = [k for k in lists[0] if k in lists[1]]
    rest = [k for k in lists[0] + lists[1] if k not in both]
    return list(dict.fromkeys(both + rest))


# --- report ---------------------------------------------------------------------------------------------

def report(work, keys_path=None, start=0, count=None, examples=3, seed=121):
    w = Work(work)
    matched = read_gz_json(w.path("matched.json.gz"))
    if keys_path:
        with open(keys_path, encoding="utf-8") as fh:
            keys = json.load(fh)
    else:
        keys = w.order[start:start + count if count else None]
    answers = {k: a for (k, n), a in w.answers().items() if n == 0}
    keys = [k for k in keys if k in matched]
    errors = {e["key"] for e in read_jsonl(w.path("errors.jsonl"))} - set(answers)

    def stats(subset):
        picks = [p for k in subset for p in matched[k]["asks"][0]]
        statuses = collections.Counter(p["status"] for p in picks)
        named = len(picks)
        found = statuses["matched"] + statuses["seed"] + statuses["franchise or version"] + statuses["duplicate"]
        kept = [len(merged(matched[k]["asks"])) for k in subset]
        usage = [answers[k]["usage"] for k in subset if k in answers]
        cost = [answers[k]["costUSD"] for k in subset if k in answers]
        n = max(len(subset), 1)
        return {
            "titles": len(subset),
            "knownFalse": round(sum(1 for k in subset if not matched[k]["known"]) / n, 3),
            "picksNamed": round(named / n, 1),
            "matchRate": round(found / max(named, 1), 3),
            "ambiguous": round(statuses["ambiguous"] / max(named, 1), 3),
            "unmatched": round(statuses["unmatched"] / max(named, 1), 3),
            "dropped": {s: statuses[s] for s in ("seed", "franchise or version", "duplicate")},
            "keptPerTitle": round(sum(kept) / n, 1),
            "atLeast3": round(sum(x >= 3 for x in kept) / n, 3),
            "atLeast5": round(sum(x >= 5 for x in kept) / n, 3),
            "atLeast10": round(sum(x >= 10 for x in kept) / n, 3),
            "tokens": {f: round(sum(u.get(f) or 0 for u in usage) / max(len(usage), 1))
                       for f in ("promptTokenCount", "candidatesTokenCount", "thoughtsTokenCount")},
            "costPerTitleUSD": round(sum(cost) / max(len(cost), 1), 6),
            "standardCostPerTitleUSD": round(sum(standard_cost(u) for u in usage) / max(len(usage), 1), 6),
        }

    by_band = collections.defaultdict(list)
    for k in keys:
        by_band[band_of(w.position[k])].append(k)
    rng = random.Random(seed)
    shown = []
    for k in rng.sample(keys, min(examples, len(keys))):
        t = w.titles[k]
        kept = merged(matched[k]["asks"])
        shown.append({"seed": f"{t['title']} ({t.get('year')}, {media_kind(k)})", "band": band_name(band_of(w.position[k])),
                      "known": matched[k]["known"],
                      "picks": [f"{w.titles[x]['title']} ({w.titles[x].get('year')})" for x in kept if x in w.titles],
                      "unmatched": [p["title"] for p in matched[k]["asks"][0] if p["status"] in ("unmatched", "ambiguous")]})
    return {"all": stats(keys), "errors": len(errors),
            "byBand": {band_name(b): stats(v) for b, v in sorted(by_band.items())}, "examples": shown,
            "spentUSD": round(w.spent(), 4), "projectedAllTitlesBatchUSD":
                round(len(w.order) * stats(keys)["standardCostPerTitleUSD"] * BATCH_FACTOR, 2)}


# --- export ---------------------------------------------------------------------------------------------

def export(work, out):
    """Every asked title with its matched picks in the model's order; a title asked with none keeps an empty
    list, which the store records as asked."""
    w = Work(work)
    matched = read_gz_json(w.path("matched.json.gz"))
    answered = {key for key, _ in w.answers()}
    errors_path = w.path("errors.jsonl")
    refused = {error["key"] for error in read_jsonl(errors_path)
               if accepted_empty(error)} - answered
    anchors = {key: [] for key in refused}
    anchors.update({k: merged(v["asks"]) for k, v in sorted(matched.items())})
    anchors = dict(sorted(anchors.items()))
    value = {"schema": EXPORT_SCHEMA, "issue": "oxyc/den-atlas#121", "model": MODEL,
             "manifestSha256": file_digest(w.path("manifest.json")),
             "answersSha256": file_digest(w.path("answers.jsonl")),
             "errorsSha256": file_digest(errors_path) if os.path.exists(errors_path) else hashlib.sha256(b"").hexdigest(),
             "count": len(anchors), "anchors": anchors}
    with open(out + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    os.replace(out + ".tmp", out)
    return {"out": out, "sha256": file_digest(out), "anchors": len(anchors),
            "withPicks": sum(1 for v in anchors.values() if v), "picks": sum(len(v) for v in anchors.values())}


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    for name in ("corpus", "articles", "pool", "work"):
        p.add_argument(f"--{name}", required=True)
    p.add_argument("--max-spend-usd", type=float, required=True)
    p = sub.add_parser("sample")
    p.add_argument("--work", required=True)
    p.add_argument("--per-band", type=int, default=84)
    p.add_argument("--seed", type=int, default=121)
    p.add_argument("--out", required=True)
    p = sub.add_parser("run")
    p.add_argument("--work", required=True)
    p.add_argument("--keys", help="a keys file (from `sample`); else a slice of the popularity order")
    p.add_argument("--start", type=int, default=0)
    p.add_argument("--count", type=int)
    p.add_argument("--online", action="store_true", help="generateContent per title instead of a batch job")
    p.add_argument("--twice", action="store_true", help="ask each title twice; picks named both times lead")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--retry-errors", action="store_true")
    p.add_argument("--label")
    p.add_argument("--run-cap-usd", type=float, help="this invocation's own limit, under the manifest's cap")
    p = sub.add_parser("collect")
    p.add_argument("--work", required=True)
    p = sub.add_parser("match")
    for name in ("work", "corpus", "franchises"):
        p.add_argument(f"--{name}", required=True)
    p.add_argument("--follows", help="cache of the sequel links' corpus keys, written on first use")
    p = sub.add_parser("report")
    p.add_argument("--work", required=True)
    p.add_argument("--keys")
    p.add_argument("--start", type=int, default=0)
    p.add_argument("--count", type=int)
    p.add_argument("--examples", type=int, default=3)
    p = sub.add_parser("export")
    p.add_argument("--work", required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("backfill", help="ask the fallback model about titles the primary refused")
    p.add_argument("--work", required=True)
    p.add_argument("--keys", required=True)
    p.add_argument("--out", default=BACKFILL)
    p.add_argument("--max-spend-usd", type=float, required=True)
    p.add_argument("--provider", help="instead of the configured fallback's, e.g. claude-cli for a local backfill")
    p.add_argument("--model")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        result = prepare(args.corpus, args.articles, args.pool, args.work, args.max_spend_usd)
    elif args.command == "sample":
        result = sample(args.work, args.per_band, args.seed, args.out)
    elif args.command == "run":
        result = run(args.work, args.keys, args.start, args.count, args.online, 2 if args.twice else 1,
                     args.workers, args.retry_errors, args.label, args.run_cap_usd)
    elif args.command == "collect":
        result = collect(Work(args.work))
    elif args.command == "match":
        result = match(args.work, args.corpus, args.franchises, args.follows)
    elif args.command == "report":
        result = report(args.work, args.keys, args.start, args.count, args.examples)
    elif args.command == "backfill":
        cfg = {**CFG["fallback"], **{k: v for k, v in (("provider", args.provider), ("model", args.model)) if v}}
        llm.check(cfg)
        result = backfill(args.work, args.keys, args.out, args.max_spend_usd, cfg)
    else:
        result = export(args.work, args.out)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
