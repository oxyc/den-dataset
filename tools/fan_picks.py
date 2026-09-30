#!/usr/bin/env python3
"""You Might Also Like: titles a fan would love, named by a model and matched to the store (oxyc/den-atlas#121).

For each title, Gemini Flash is told the title, its year, whether it is a film or a series, and the lead of
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

Every answer is appended as it arrives, with its token usage, cost and `modelVersion`; a rerun asks only what
has no answer. Spend is checked against the manifest's cap before anything is asked: a chunk is admitted only
while the recorded spend plus the chunk's projected cost, at the observed cost per title with a margin, stays
under it.

The key is read from GEMINI_API_KEY and sent only as the `x-goog-api-key` header.
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
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from store import aliases  # noqa: E402
from store.cards import display_title, release_year  # noqa: E402

SCHEMA = "fan-picks-run-v1"
EXPORT_SCHEMA = "fan-picks-v1"
MODEL = "gemini-3.7-flash"
THINKING = "low"
MAX_OUTPUT_TOKENS = 2048
LEAD_CHARS = 1500
#: Standard price per token, in and out (thinking bills as output); the Batch API is half. Google's list
#: price for Gemini 3.7 Flash, on its promotion to 31 Dec 2026.
PRICE_IN, PRICE_OUT = 0.75e-6, 3.75e-6
BATCH_FACTOR = 0.5
#: What a title cost in the pilot at standard price (351 in, 665 out), used until this run has its own.
PILOT_COST = 351 * PRICE_IN + 665 * PRICE_OUT
#: A chunk is reserved at the observed cost per title times this.
MARGIN = 1.3
#: A scheduled day is small, but a broken change plan must not turn an unattended run into a catalogue
#: purchase. The CLI may lower or deliberately raise this; the whole set must fit before the first call.
DAILY_SPEND_CAP = 1.0
#: Popularity bands the sample is stratified over and a report is broken down by: positions in the order.
BANDS = ((0, 1000), (1000, 3000), (3000, 6000), (6000, 10000), (10000, 20000), (20000, None))

API = "https://generativelanguage.googleapis.com"


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
    return (f"A friend loved {title['title']} ({when}). {lead} ".replace("  ", " ")
            + "Name up to 20 films or series they would also love — any genre, era or country; taste, not "
              "similarity. If you don't know this title well, say so and name only picks you're confident in. "
              'Return only JSON: {"known": bool, "picks":[{"title":str, "year":int, "type":"film"|"series"}]}')


def request_body(title):
    return {"contents": [{"role": "user", "parts": [{"text": prompt(title)}]}],
            "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": MAX_OUTPUT_TOKENS,
                                 "thinkingConfig": {"thinkingLevel": THINKING}}}


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
    out = (usage.get("candidatesTokenCount") or 0) + (usage.get("thoughtsTokenCount") or 0)
    return (usage.get("promptTokenCount") or 0) * PRICE_IN + out * PRICE_OUT


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

def api_key():
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise SystemExit("GEMINI_API_KEY is not set")
    return key


def http(method, url, body=None, headers=None, raw=False, timeout=300):
    """One request with the key as a header. Returns (headers, body); raises HTTPError."""
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"x-goog-api-key": api_key(), "Content-Type": "application/json",
                                          **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = resp.read()
        return resp.headers, (payload if raw else json.loads(payload or b"{}"))


def text_of(response):
    candidates = response.get("candidates") or []
    parts = ((candidates[0].get("content") or {}).get("parts") or []) if candidates else []
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


def parse(text):
    """The answer's `known` and picks, or ValueError. A pick needs a title; year and type are kept as given."""
    found = re.search(r"\{.*\}", text or "", re.S)
    value = json.loads(found.group(0) if found else text)
    picks = value.get("picks")
    if not isinstance(picks, list):
        raise ValueError("no picks list")
    kept = []
    for pick in picks:
        if isinstance(pick, dict) and isinstance(pick.get("title"), str) and pick["title"].strip():
            year = pick.get("year")
            kept.append({"title": pick["title"].strip(),
                         "year": year if isinstance(year, int) and not isinstance(year, bool) else None,
                         "type": pick.get("type") if pick.get("type") in ("film", "series") else None})
    return bool(value.get("known")), kept


def record(response, key, ask, mode, batch=None):
    """An answer (or an error) row from one generateContent response."""
    usage = response.get("usageMetadata") or {}
    cost = standard_cost(usage) * (BATCH_FACTOR if mode == "batch" else 1.0)
    row = {"key": key, "ask": ask, "mode": mode, "modelVersion": response.get("modelVersion"),
           "usage": {k: usage.get(k) for k in ("promptTokenCount", "candidatesTokenCount", "thoughtsTokenCount",
                                               "totalTokenCount")},
           "costUSD": cost, "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    if batch:
        row["batch"] = batch
    text = text_of(response)
    try:
        row["known"], row["picks"] = parse(text)
        row["text"] = text
        return row, None
    except (ValueError, json.JSONDecodeError) as exc:
        finish = ((response.get("candidates") or [{}])[0]).get("finishReason")
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


def generate_online(title, key, ask=0):
    """One retried generateContent ask: `(answer, error, accepted)`.

    `accepted` separates an HTTP/transport failure from a response received from the provider. A safety
    refusal or malformed response is still an error. A provider-accepted empty response is a durable
    asked-empty anchor; malformed non-empty output remains unasked.
    """
    url = f"{API}/v1beta/models/{MODEL}:generateContent"
    for attempt in range(6):
        try:
            _, response = http("POST", url, request_body(title), {"Accept": "application/json"}, timeout=90)
            answer, error = record(response, key, ask, "online")
            return answer, error, True
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < 5:
                time.sleep(10 * (attempt + 1))
                continue
            return None, {"key": key, "ask": ask, "mode": "online",
                          "error": f"HTTP {exc.code} {exc.read()[:300]!r}", "costUSD": 0.0}, False
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt < 5:
                time.sleep(10 * (attempt + 1))
                continue
            return None, {"key": key, "ask": ask, "mode": "online",
                          "error": f"{type(exc).__name__}: {exc}", "costUSD": 0.0}, False


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


def upload(path, display):
    """The Files API's resumable upload: start, then one upload-and-finalize. Returns `files/…`."""
    size = os.path.getsize(path)
    headers, _ = http("POST", f"{API}/upload/v1beta/files", {"file": {"display_name": display}},
                      {"X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
                       "X-Goog-Upload-Header-Content-Length": str(size),
                       "X-Goog-Upload-Header-Content-Type": "application/jsonl"})
    target = headers.get("X-Goog-Upload-URL") or headers.get("x-goog-upload-url")
    with open(path, "rb") as fh:
        _, done = http("POST", target, fh.read(), {"X-Goog-Upload-Offset": "0",
                                                   "X-Goog-Upload-Command": "upload, finalize",
                                                   "Content-Type": "application/jsonl"})
    return done["file"]["name"]


def submit(w, items, label, reserve):
    os.makedirs(w.path("batch-inputs"), exist_ok=True)
    source = w.path(f"batch-inputs/{label}.jsonl")
    with open(source, "w", encoding="utf-8") as fh:
        for key, ask in items:
            fh.write(json.dumps({"key": f"{key}#{ask}", "request": request_body(w.titles[key])},
                                ensure_ascii=False) + "\n")
    file_name = upload(source, f"fan-picks-{label}")
    try:
        _, job = http("POST", f"{API}/v1beta/models/{MODEL}:batchGenerateContent",
                      {"batch": {"displayName": f"fan-picks-{label}", "inputConfig": {"fileName": file_name}}})
    except urllib.error.HTTPError as exc:
        # The API says why (an enqueued-token limit, a bad file); nothing is recorded, so a rerun resubmits.
        raise SystemExit(f"batch {label} refused: HTTP {exc.code} {exc.read()[:500].decode(errors='replace')}")
    entry = {"name": job["name"], "label": label, "file": file_name, "items": [list(i) for i in items],
             "state": "submitted", "reservedUSD": reserve,
             "submittedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    save_batches(w, batches(w) + [entry])
    return entry


def find(value, name):
    """The first value under `name` anywhere in a JSON tree: the operation's shape differs by API version."""
    if isinstance(value, dict):
        if name in value:
            return value[name]
        for child in value.values():
            found = find(child, name)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find(child, name)
            if found is not None:
                return found
    return None


def collect(w, log=sys.stderr):
    """Poll every submitted job; record a finished one's answers. Returns the jobs' states."""
    jobs = batches(w)
    states = {}
    for job in jobs:
        if job["state"] != "submitted":
            states[job["label"]] = job["state"]
            continue
        _, status = http("GET", f"{API}/v1beta/{job['name']}")
        state = find(status, "state") or "unknown"
        states[job["label"]] = state
        if state in ("BATCH_STATE_FAILED", "BATCH_STATE_CANCELLED", "BATCH_STATE_EXPIRED", "JOB_STATE_FAILED"):
            job["state"] = state
            job["error"] = json.dumps(find(status, "error"))[:500]
            continue
        if state not in ("BATCH_STATE_SUCCEEDED", "JOB_STATE_SUCCEEDED"):
            continue
        responses = find(status, "responsesFile")
        _, raw = http("GET", f"{API}/download/v1beta/{responses}:download?alt=media", raw=True, timeout=600)
        wanted = {f"{k}#{n}" for k, n in job["items"]}
        answers, errors = [], []
        for line in raw.decode("utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            label = item.get("key")
            if label not in wanted:
                continue
            wanted.discard(label)
            key, _, ask = label.rpartition("#")
            if "response" in item:
                answer, error = record(item["response"], key, int(ask), "batch", job["name"])
            else:
                answer, error = None, {"key": key, "ask": int(ask), "mode": "batch", "batch": job["name"],
                                       "error": json.dumps(item.get("error") or item)[:300], "costUSD": 0.0}
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


def daily_update(corpus_path, articles_path, franchises_path, existing_path, out, keys, workers=8,
                 generate=generate_online, follows=None, max_spend=DAILY_SPEND_CAP):
    """Ask Gemini online for new daily titles, match with the full run's rule, and merge the durable input.

    Existing anchors and picks that no longer join the current corpus are removed before the store sees
    them. A provider-accepted empty answer is asked-empty, while malformed non-empty output remains unasked;
    a transport failure refuses the update so an outage can never become `fan_picks_a`. The whole request
    set must fit the invocation's projected spend cap before any request is made.
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
    def one(item):
        key, title = item
        return key, generate(title, key, 0)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for key, (answer, error, accepted) in pool.map(one, titles.items()):
            if not accepted:
                unavailable[key] = error
            elif answer is not None:
                answered[key] = answer
            elif accepted_empty(error):
                empty_answers[key] = error
            else:
                parse_errors[key] = error
    if unavailable:
        first = next(iter(unavailable.items()))
        raise RuntimeError(f"daily fan picks: Gemini unreachable for {len(unavailable)} title(s); "
                           f"{first[0]}: {first[1].get('error')}")

    if titles:
        with open(franchises_path, encoding="utf-8") as fh:
            franchises = json.load(fh)
        names = Names(rows)
        owned = related(rows, franchises, sequel_keys(rows, titles) if follows is None else follows)
        for key, answer in answered.items():
            matched = match_answer(answer, key, names, owned.get(key, set()))
            anchors[key] = merged([matched])
        for key in empty_answers:
            anchors[key] = []

    costs = [row.get("costUSD", 0.0)
             for row in [*answered.values(), *empty_answers.values(), *parse_errors.values()]]
    value = {"schema": EXPORT_SCHEMA, "issue": "oxyc/den-atlas#121", "model": MODEL,
             "count": len(anchors), "anchors": dict(sorted(anchors.items()))}
    write_json(out, value)
    return {"asked": len(titles), "answered": len(answered), "emptyAnswers": len(empty_answers),
            "parseErrors": len(parse_errors),
            "notInCorpus": not_in_corpus,
            "anchors": len(anchors), "picks": sum(len(v) for v in anchors.values()),
            "costUSD": round(sum(costs), 6), "projectedSpendUSD": round(projected, 6),
            "spendCapUSD": max_spend}


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
    else:
        result = export(args.work, args.out)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
