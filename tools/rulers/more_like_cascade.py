#!/usr/bin/env python3
"""Preregister and run the bounded pilot of the More Like This cascade (#132).

The cascade reranks the first 100 titles of atlas's live More Like This row in two bounded Jev steps:

1. **Screen** — one call per anchor over title, year and type only: one Noul per candidate asking whether
   people who enjoyed the anchor are unusually likely to enjoy it (the title/year prior's question). Titles
   released in or after `CUTOFF_YEAR` are not screened, and an anchor released then is not screened at all.
2. **Evidence** — atlas's first ten are always finalists. A candidate ranked 11-100 is promoted to a finalist
   only when its screen score is recognised (>= 0.70) and beats the weakest screened top-ten score by at
   least 0.15 — the pair rule that decided 112/112 calibration pairs correctly — at most five per anchor.
   One call per anchor then carries the anchor and every finalist with lead + extractor-selected story
   evidence and asks one overall Noul per finalist.

The finalists are ordered by that Noul (ties by atlas rank) in front of every other title, which keeps
atlas's order. A failed call or an anchor without article evidence keeps atlas's row whole.

Everything a paid call depends on is frozen and hashed before it is made: `prepare` writes the rows, the
screen worklist and the metric/pass plan; `finalize` derives the evidence worklist from the screen answers by
the rule above and appends its hash. Without `--spend` the paid steps only report their exact ceilings.
The live atlas address is an argument and never written into an artifact.
"""
import argparse
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
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from more_like_gate import (MAX_EVIDENCE_CHARS, MODEL, TypeSafe, _chosen, _ruler, canonical,  # noqa: E402
                            comparable_heading, digest, file_digest, read_jsonl)
from lib.typesafe_client import api_key  # noqa: E402
from pipeline import run_combined as combined  # noqa: E402
from pipeline.article_sections import parse_sections  # noqa: E402

SCHEMA = "jev-more-like-cascade-pilot-v1"
POOL_K = 100
ROW_FETCH = 200
TOP = 10
MAX_PROMOTIONS = 5
RECOGNISED = 0.70
MARGIN = 0.15
CUTOFF_YEAR = 2025
K = 10
# The model reads at most 32k tokens of state plus its longest question. Article evidence is shared out of a
# fixed character budget so that no evidence call can exceed it: a work shorter than its fair share keeps all
# of its evidence and gives the rest back to the longer ones.
EVIDENCE_BUDGET_CHARS = 90_000
WRAPPER_TOKEN_CEILING = 1_024
BOOTSTRAPS = 2_000
BOOTSTRAP_SEED = 20260928
GRADE_GAIN = {"good": 2.0, "ok": 1.0, "bad": 0.0}


def dataset_key(atlas_type, tmdb_id):
    return f"{'tv' if atlas_type in ('series', 'tv') else 'movie'}:{int(tmdb_id)}"


def atlas_path(key):
    kind, tmdb_id = key.split(":")
    return f"{'series' if kind == 'tv' else 'movie'}/{tmdb_id}"


def _get(base, path):
    with urllib.request.urlopen(f"{base.rstrip('/')}{path}", timeout=60) as fh:
        return json.load(fh)


def _write_json(path, value):
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    os.replace(path + ".tmp", path)


def _read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def judged_anchors(judged_path):
    return [dataset_key(*case["seed"].split(":")) for case in _read_json(judged_path)["cases"]]


def ruler_cases(ruler_path):
    return _chosen(_ruler(_read_json(ruler_path), require_prior=False), 512)


# --- fetch ---------------------------------------------------------------------------------------------

def popularity_order(atlas):
    """Each browsable title's rank in its type's popularity order, with that type's size, title and year."""
    popularity = {}
    for kind in ("movie", "series"):
        rows, skip, order = [], 0, None
        while True:
            page = _get(atlas, f"/index/filter/{kind}/titles.json?skip={skip}&limit=500")
            if order is None:
                order = page.get("order")
            elif page.get("order") != order:
                raise SystemExit(f"{kind} popularity order changed while paging")
            rows.extend(page["titles"])
            skip += len(page["titles"])
            if not page["titles"] or skip >= page["total"]:
                break
        for rank, title in enumerate(rows, 1):
            popularity[dataset_key(title["type"], title["id"])] = {
                "rank": rank, "typeSize": len(rows), "title": title["title"], "year": title.get("year")}
    return popularity


def fetch(atlas, expect_version, ruler_path, judged_path, out, workers=8):
    """Atlas's live rows (first 200) for every pilot anchor, and each type's popularity order."""
    meta = _get(atlas, "/dataset.json")
    if meta.get("datasetVersion") != expect_version:
        raise SystemExit(f"atlas serves {meta.get('datasetVersion')}, not {expect_version}")
    popularity = popularity_order(atlas)
    anchors = sorted(set(judged_anchors(judged_path)) | {case["anchor"]["key"] for case in ruler_cases(ruler_path)})

    def row(key):
        answer = _get(atlas, f"/index/similar/{atlas_path(key)}.json?limit={ROW_FETCH}")
        return key, [dataset_key(item["type"], item["id"]) for item in answer["mixed"]]

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        rows = dict(pool.map(row, anchors))
    # A title outside the browse order is still screened, so its name comes from its page; a title atlas has no
    # card for is named from its article in `prepare`.
    unnamed = sorted({key for anchor, keys in rows.items() for key in [anchor, *keys[:POOL_K]]} - set(popularity))

    def named(key):
        card = _get(atlas, f"/index/title/{atlas_path(key)}.json")
        return key, {"rank": None, "typeSize": None, "title": card.get("title"), "year": card.get("year")}

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        popularity.update(pool.map(named, unnamed))
    after = _get(atlas, "/dataset.json")
    if after.get("datasetVersion") != expect_version:
        raise SystemExit("atlas changed dataset while the rows were fetched")
    blob = {"datasetVersion": expect_version, "storeSha256": meta.get("storeSha256"),
            "fetchedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "rowLimit": ROW_FETCH, "popularity": popularity, "rows": rows}
    _write_json(out, blob)
    return {"out": out, "sha256": file_digest(out), "anchors": len(rows), "titles": len(popularity),
            "shortRows": sum(len(r) < POOL_K for r in rows.values())}


# --- evidence ------------------------------------------------------------------------------------------

def load_evidence(articles_path, enriched_dir, wanted):
    """Lead + extractor-selected story sections per wanted title, exactly as the gate built them, or why not.

    The gate refused its whole run on one unrebuildable article. Here such a title is recorded as missing
    instead: the cascade's fallback decides what happens to it, and the count is reported."""
    records = {}
    for row in read_jsonl(articles_path):
        key = f"{row.get('mediaType')}:{row.get('tmdbId')}"
        if key in wanted:
            if key in records:
                raise ValueError(f"{articles_path}: duplicate article key {key}")
            records[key] = row
    need = {key for key, row in records.items() if "plotSections" not in row}
    found = {}
    if need:
        batches = sorted(((int(m.group(1)), name) for name in os.listdir(enriched_dir)
                          if (m := re.fullmatch(r"batch-(\d+)\.json", name))), reverse=True)
        for _, name in batches:
            for item in _read_json(os.path.join(enriched_dir, name)):
                key = f"{item.get('mediaType')}:{item.get('tmdbId')}"
                if key in need and key not in found \
                        and item.get("plotArticle") == records[key].get("article") \
                        and (item.get("plotLanguage") or "en") == (records[key].get("language") or "en"):
                    found[key] = item
            if len(found) == len(need):
                break
    result = {}
    for key in sorted(wanted):
        row = records.get(key)
        if row is None or not isinstance(row.get("text"), str) or not row["text"].strip():
            result[key] = {"missing": "no article"}
            continue
        if "plotSections" in row:
            headings, year = row.get("plotSections") or [], row.get("year")
        elif key in found:
            headings, year = found[key].get("plotSections") or [], found[key].get("year")
        else:
            result[key] = {"missing": "no extractor record for the dumped article"}
            continue
        available = {}
        for section in parse_sections(row["text"])[1:]:
            available.setdefault(comparable_heading(section["heading"]), []).append(section["heading"])
        matched = []
        for heading in headings:
            choices = available.get(comparable_heading(heading))
            if not choices:
                matched = None
                break
            matched.append(choices.pop(0))
        if matched is None:
            result[key] = {"missing": "extractor headings differ from the dumped article"}
            continue
        sections = parse_sections(row["text"], matched)
        text = "\n\n".join(f"## {s['heading']}\n{s['text']}" for s in sections
                           if s["id"] == "s000" or s["extractorSelected"]).strip()[:MAX_EVIDENCE_CHARS]
        if not text:
            result[key] = {"missing": "empty evidence"}
            continue
        result[key] = {"title": row.get("title"), "year": year, "evidence": text,
                       "articleSha256": hashlib.sha256(row["text"].encode()).hexdigest()}
    return result


def shares(lengths, budget):
    """Per-work character caps: works under their fair share keep everything, the rest split what is left."""
    caps, left, remaining = {}, dict(lengths), budget
    while left:
        fair = remaining // len(left)
        short = {key: n for key, n in left.items() if n <= fair}
        if not short:
            caps.update({key: fair for key in left})
            break
        for key, n in short.items():
            caps[key] = n
            remaining -= n
            del left[key]
    return caps


def blinded(anchor, keys, width):
    """Stable labels independent of atlas's order, so a candidate's position says nothing about its rank."""
    order = sorted(keys, key=lambda key: hashlib.sha256(f"den-dataset#132-cascade-v1\0{anchor}\0{key}".encode()).digest())
    return {f"c{index:0{width}d}": key for index, key in enumerate(order, 1)}


def kind(key):
    return "series" if key.startswith("tv:") else "film"


def screen_questions(labels):
    return {label: {"type": "noul", "instructions": (
        f"Are people who enjoyed `anchor` unusually likely, compared with an average title, to also enjoy "
        f"`candidates.{label}`?")} for label in labels}


def evidence_questions(labels):
    return {label: {"type": "noul", "instructions": (
        f"Using the supplied article evidence, does `candidates.{label}` match `anchor` on the overall "
        "expectation that someone wanting more of the anchor would want this work? Judge the works, not "
        "article-writing quality, fame, shared release year, or broad genre alone.")} for label in labels}


def ceiling(state, questions):
    body = json.dumps({"state": state, "model": MODEL, "questions": questions}).encode()
    return len(body) + WRAPPER_TOKEN_CEILING


def evidence_state(anchor, finalists, identity, evidence):
    labels = blinded(anchor, finalists, 2)
    caps = shares({key: len(evidence[key]["evidence"]) for key in [anchor, *finalists]}, EVIDENCE_BUDGET_CHARS)

    def work(key):
        return {**identity[key], "articleEvidence": evidence[key]["evidence"][:caps[key]]}
    state = {"task": "More Like This ranking", "anchor": work(anchor),
             "candidates": {label: work(key) for label, key in labels.items()}}
    return state, labels


# --- slices ---------------------------------------------------------------------------------------------

def percentile(pool, key):
    item = pool["popularity"].get(key) or {}
    return item["rank"] / item["typeSize"] if item.get("rank") else None


def popularity_slice(pct):
    if pct is None:
        return "unranked"
    return "popular" if pct <= 0.05 else "mid" if pct <= 0.25 else "long tail"


PLAN = {
    "sets": {
        "judged": "den-atlas judged/rail.json, both splits, with judged/rail-cross.json's other-type grades; "
                  "grades on titles sharing the seed's primary franchise are removed (atlas's own rule)",
        "ruler": "the 512 step-9d cases the gate sampled (films, >= 50 MovieLens likes): want = the "
                 "MovieLens-positive title, drop = the matched negative"},
    "rows": f"atlas's live mixed More Like This row (first {ROW_FETCH}); the cascade reorders only its first "
            f"{POOL_K} and keeps the rest",
    "screen": {"question": "title/year prior (co-rating) per candidate, one call per anchor",
               "skip": f"titles released in or after {CUTOFF_YEAR} are not screened; nor is such an anchor",
               "finalists": f"atlas's first {TOP}, plus at most {MAX_PROMOTIONS} of ranks {TOP + 1}-{POOL_K} "
                            f"whose screen score >= {RECOGNISED} and >= {MARGIN} above the lowest screened "
                            "top-ten score, highest first, ties by atlas rank"},
    "evidence": {"question": "the gate's overall Noul per finalist, one call per anchor, lead + story evidence",
                 "budgetChars": EVIDENCE_BUDGET_CHARS, "perWorkCapChars": MAX_EVIDENCE_CHARS},
    "order": "finalists by overall Noul (ties atlas rank) first; a top-ten title without evidence keeps its "
             "atlas slot; every other title keeps atlas order",
    "fallback": "anchor without evidence, a failed or invalid screen, or a failed or invalid evidence call: "
                "atlas's row unchanged",
    "metrics": {
        "judged": f"nDCG'@{K} (condensed), nDCG@{K}, P@{K}, bad@{K}, judged@{K} as den_index::eval computes "
                  "them; means per case, bad and judged summed",
        "ruler": f"over cases whose want is in atlas's first {POOL_K}: want gain = mean 1/log2(1+rank), "
                 f"want@{K}; drop@{K} = cases whose drop is in the first {K}; pair order = share of cases with "
                 "both present where want ranks above drop",
        "cost": "measured input tokens per anchor for screen and evidence, at $0.042 per million"},
    "slices": {"type": "anchor is a film or a series",
               "popularity": "anchor's rank in atlas's popularity order of its own type, as a share of that "
                             "type: popular <= 5%, mid 5-25%, long tail > 25%"},
    "pass": [
        "P1 judged, all 62: mean nDCG'@10 cascade > atlas, and total bad@10 cascade <= atlas",
        "P2 judged series: mean nDCG'@10 cascade >= atlas, and bad@10 cascade <= atlas",
        "P3 judged anchors outside the popular 5% (the least popular the judged set holds): "
        "mean nDCG'@10 cascade >= atlas, and bad@10 cascade <= atlas",
        "P4 ruler long tail (> 25%): want gain cascade >= atlas, and drop@10 cascade <= atlas",
        "pass = all four; a pass makes the full precompute eligible, a failure means it is not bought"],
}


# --- prepare --------------------------------------------------------------------------------------------

def prepare(pool_path, ruler_path, judged_path, cross_path, franchises_path, articles_path, enriched_dir,
            work, cap):
    pool = _read_json(pool_path)
    judged = judged_anchors(judged_path)
    cases = ruler_cases(ruler_path)
    anchors = [(key, "judged") for key in judged] + [(case["anchor"]["key"], "ruler") for case in cases]
    if len({key for key, _ in anchors}) != len(anchors) or any(key not in pool["rows"] for key, _ in anchors):
        raise ValueError("pilot anchors must be distinct and each have a fetched row")
    wanted = {key for anchor, _ in anchors for key in [anchor, *pool["rows"][anchor][:POOL_K]]}
    evidence = load_evidence(articles_path, enriched_dir, wanted)
    identity = {}
    for key in wanted:
        named = pool["popularity"].get(key) or {}
        title = named.get("title") or evidence[key].get("title")
        year = named.get("year") or evidence[key].get("year")
        if title:
            identity[key] = {"title": title, "year": year, "type": kind(key)}
    plan, screen_rows = [], []
    for anchor, origin in anchors:
        row = pool["rows"][anchor][:POOL_K]
        entry = {"anchor": anchor, "origin": origin, "row": row}
        if "evidence" not in evidence[anchor] or anchor not in identity:
            entry["fallback"] = "anchor has no evidence"
        elif (identity[anchor]["year"] or 0) >= CUTOFF_YEAR:
            entry["screen"] = None
        else:
            screened = [key for key in row if key in identity and (identity[key]["year"] or 0) < CUTOFF_YEAR]
            labels = blinded(anchor, screened, 3)
            state = {"anchor": identity[anchor],
                     "candidates": {label: identity[key] for label, key in labels.items()}}
            qs = screen_questions(labels)
            entry["screen"] = labels
            screen_rows.append({"anchor": anchor, "labels": labels, "state": state, "stateSha256": digest(state),
                                "questionsSha256": digest(qs), "ceiling": ceiling(state, qs)})
        plan.append(entry)
    os.makedirs(work, exist_ok=True)
    files = {"plan.jsonl": plan, "screen-worklist.jsonl": screen_rows,
             "evidence.jsonl": [{"key": key, **value} for key, value in sorted(evidence.items())],
             "identity.jsonl": [{"key": key, **value} for key, value in sorted(identity.items())]}
    hashes = {}
    for name, rows in files.items():
        text = "".join(canonical(row) + "\n" for row in rows)
        hashes[name] = hashlib.sha256(text.encode()).hexdigest()
        files[name] = text
    # The plan's worst evidence case, for the spend check before anything is bought: every anchor that reaches
    # evidence carrying its first fifteen as finalists.
    worst = 0
    for entry in plan:
        if "fallback" in entry:
            continue
        finalists = [key for key in entry["row"][:TOP + MAX_PROMOTIONS] if "evidence" in evidence[key]
                     and key in identity]
        state, labels = evidence_state(entry["anchor"], finalists, identity, evidence)
        worst += ceiling(state, evidence_questions(labels))
    registration = {
        "schema": SCHEMA, "issue": "oxyc/den-dataset#132", "model": MODEL,
        "datasetVersion": pool["datasetVersion"], "storeSha256": pool["storeSha256"],
        "inputs": {"pool": file_digest(pool_path), "ruler": file_digest(ruler_path),
                   "judged": file_digest(judged_path), "judgedCross": file_digest(cross_path),
                   "franchises": file_digest(franchises_path), "articles": file_digest(articles_path)},
        "files": hashes, "plan": PLAN,
        "parameters": {"poolK": POOL_K, "top": TOP, "maxPromotions": MAX_PROMOTIONS, "recognised": RECOGNISED,
                       "margin": MARGIN, "cutoffYear": CUTOFF_YEAR, "k": K,
                       "evidenceBudgetChars": EVIDENCE_BUDGET_CHARS},
        "questionTemplatesSha256": digest([screen_questions(["cX"]), evidence_questions(["cX"])]),
        "anchors": {"judged": len(judged), "ruler": len(cases)},
        "spendCapUSD": cap,
        "ceilingsUSD": {"screen": sum(r["ceiling"] for r in screen_rows) * TypeSafe.RATE_PER_INPUT_TOKEN,
                        "evidenceWorstCase": worst * TypeSafe.RATE_PER_INPUT_TOKEN},
        "bootstrap": {"unit": "anchor", "resamples": BOOTSTRAPS, "seed": BOOTSTRAP_SEED},
    }
    path = os.path.join(work, "preregistration.json")
    if os.path.exists(path):
        if _read_json(path) != registration:
            raise ValueError(f"{path}: refusing to replace a different preregistration")
        return registration
    for name, text in files.items():
        with open(os.path.join(work, name) + ".tmp", "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(os.path.join(work, name) + ".tmp", os.path.join(work, name))
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(registration, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(path + ".tmp", path)
    return registration


def _load(work, name, expected):
    path = os.path.join(work, name)
    if file_digest(path) != expected:
        raise ValueError(f"{path}: changed after it was registered")
    return list(read_jsonl(path))


def load_registered(work):
    registration = _read_json(os.path.join(work, "preregistration.json"))
    if registration.get("schema") != SCHEMA or registration.get("model") != MODEL:
        raise ValueError(f"{work}: incompatible preregistration")
    if digest([screen_questions(["cX"]), evidence_questions(["cX"])]) != registration["questionTemplatesSha256"]:
        raise ValueError("question templates changed after preregistration")
    return registration


# --- calls ----------------------------------------------------------------------------------------------

PHASES = {"screen": ("screen-worklist.jsonl", "screen-answers.jsonl", screen_questions),
          "evidence": ("evidence-worklist.jsonl", "evidence-answers.jsonl", evidence_questions)}


def phase_rows(work, phase):
    registration = load_registered(work)
    if phase == "screen":
        return _load(work, "screen-worklist.jsonl", registration["files"]["screen-worklist.jsonl"])
    final = _read_json(os.path.join(work, "finalize.json"))
    return _load(work, "evidence-worklist.jsonl", final["evidenceWorklistSha256"])


def answers(work, phase, rows=None):
    """Recorded answers of a phase, each checked against the row it answers."""
    path = os.path.join(work, PHASES[phase][1])
    rows = {row["anchor"]: row for row in (rows if rows is not None else phase_rows(work, phase))}
    done = {}
    if os.path.exists(path):
        for answer in read_jsonl(path):
            source = rows.get(answer.get("anchor"))
            if source is None or answer["anchor"] in done or answer.get("stateSha256") != source["stateSha256"] \
                    or answer.get("questionsSha256") != source["questionsSha256"] or answer.get("model") != MODEL:
                raise ValueError(f"{path}: answer provenance mismatch for {answer.get('anchor')}")
            tokens = (answer.get("usage") or {}).get("input_tokens")
            if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
                raise ValueError(f"{path}: invalid recorded usage for {answer['anchor']}")
            done[answer["anchor"]] = answer
    return done


def recorded_tokens(work):
    total = 0
    for phase, (_, name, _) in PHASES.items():
        path = os.path.join(work, name)
        if os.path.exists(path):
            total += sum(row["usage"]["input_tokens"] for row in read_jsonl(path))
    return total


def run(work, phase, spend=False, workers=4, client=None, env_path=None):
    registration = load_registered(work)
    rows = phase_rows(work, phase)
    done = answers(work, phase, rows)
    todo = [row for row in rows if row["anchor"] not in done]
    rate = TypeSafe.RATE_PER_INPUT_TOKEN
    spent = recorded_tokens(work) * rate
    remaining = sum(row["ceiling"] for row in todo) * rate
    cap = registration["spendCapUSD"]
    plan = {"phase": phase, "calls": len(rows), "answered": len(done), "callsPlanned": len(todo),
            "spentRecordedUSD": spent, "remainingCeilingUSD": remaining, "capUSD": cap,
            "fitsCap": spent + remaining <= cap}
    if not spend:
        return plan
    if spent + remaining > cap:
        raise RuntimeError(f"refusing: ${spent:.6f} spent + ${remaining:.6f} ceiling crosses the ${cap:.2f} cap")
    client = client or TypeSafe(key=api_key(env_path), model=MODEL)
    make_questions = PHASES[phase][2]
    lock, errors = threading.Lock(), []
    fh = open(os.path.join(work, PHASES[phase][1]), "a", encoding="utf-8")

    def one(row):
        qs = make_questions(row["labels"])
        if digest(qs) != row["questionsSha256"]:
            raise ValueError(f"{row['anchor']}: questions differ from the worklist")
        try:
            got, metadata = client.ask_with_metadata(row["state"], qs)
            combined.validate_answers(got, qs)
            usage = metadata.get("usage") or {}
            if metadata.get("model") != MODEL or not isinstance(usage.get("input_tokens"), int) \
                    or usage["input_tokens"] > row["ceiling"]:
                raise ValueError(f"model {metadata.get('model')!r} or usage outside the registered ceiling")
        except Exception as exc:  # recorded, not swallowed: the anchor keeps atlas's row and is reported
            with lock:
                errors.append({"anchor": row["anchor"], "error": f"{type(exc).__name__}: {exc}"[:300]})
            return
        result = {"anchor": row["anchor"], "stateSha256": row["stateSha256"],
                  "questionsSha256": row["questionsSha256"], "model": MODEL,
                  "scores": {label: got[label]["noul"] for label in qs}, "usage": usage}
        with lock:
            fh.write(canonical(result) + "\n")
            fh.flush()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(one, todo))
    finally:
        fh.close()
    if errors:
        with open(os.path.join(work, f"{phase}-errors.jsonl"), "a", encoding="utf-8") as out:
            out.writelines(canonical(e) + "\n" for e in errors)
    return {**plan, "written": len(todo) - len(errors), "errors": len(errors),
            "inputTokens": client.input_tokens, "spendUSD": client.spend}


# --- finalists ------------------------------------------------------------------------------------------

def finalists_for(entry, scores, has_evidence):
    """Atlas's first ten, plus promotions by the preregistered pair rule; and the top ten without evidence."""
    row = entry["row"]
    top = row[:TOP]
    promoted = []
    if scores is not None:
        reference = [scores[key] for key in top if key in scores]
        if reference:
            floor = min(reference)
            eligible = [(-scores[key], rank, key) for rank, key in enumerate(row[TOP:], TOP)
                        if key in scores and has_evidence(key)
                        and scores[key] >= RECOGNISED and scores[key] - floor >= MARGIN]
            promoted = [key for _, _, key in sorted(eligible)[:MAX_PROMOTIONS]]
    finalists = [key for key in top if has_evidence(key)] + promoted
    fixed = [key for key in top if not has_evidence(key)]
    return finalists, fixed, promoted


def finalize(work):
    registration = load_registered(work)
    plan = _load(work, "plan.jsonl", registration["files"]["plan.jsonl"])
    evidence = {row["key"]: row for row in _load(work, "evidence.jsonl", registration["files"]["evidence.jsonl"])}
    screened = answers(work, "screen")
    identity = {row.pop("key"): row
                for row in _load(work, "identity.jsonl", registration["files"]["identity.jsonl"])}
    rows, outcomes = [], {}
    for entry in plan:
        anchor = entry["anchor"]
        if "fallback" in entry:
            outcomes[anchor] = entry["fallback"]
            continue
        scores = None
        if entry["screen"] is not None:
            if anchor not in screened:
                outcomes[anchor] = "screen failed"
                continue
            scores = {key: screened[anchor]["scores"][label] for label, key in entry["screen"].items()}
        finalists, fixed, promoted = finalists_for(
            entry, scores, lambda key: "evidence" in evidence[key] and key in identity)
        if len(finalists) < 2:
            outcomes[anchor] = "fewer than two finalists"
            continue
        state, labels = evidence_state(anchor, finalists, identity, evidence)
        qs = evidence_questions(labels)
        rows.append({"anchor": anchor, "labels": labels, "finalists": finalists, "fixed": fixed,
                     "promoted": promoted, "state": state, "stateSha256": digest(state),
                     "questionsSha256": digest(qs), "ceiling": ceiling(state, qs)})
    text = "".join(canonical(row) + "\n" for row in rows)
    record = {"screenAnswersSha256": file_digest(os.path.join(work, PHASES["screen"][1]))
              if os.path.exists(os.path.join(work, PHASES["screen"][1])) else None,
              "evidenceWorklistSha256": hashlib.sha256(text.encode()).hexdigest(),
              "evidenceCalls": len(rows), "noEvidenceCall": outcomes,
              "evidenceCeilingUSD": sum(r["ceiling"] for r in rows) * TypeSafe.RATE_PER_INPUT_TOKEN}
    path = os.path.join(work, "finalize.json")
    if os.path.exists(path):
        if _read_json(path) != record:
            raise ValueError(f"{path}: refusing to replace a different finalist set")
        return record
    with open(os.path.join(work, "evidence-worklist.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(text)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return record


# --- scoring --------------------------------------------------------------------------------------------

def reranked(row, finalists, fixed, scores):
    """The cascade's row: finalists by Noul, top-ten titles without evidence in their atlas slots, the rest."""
    rank = {key: index for index, key in enumerate(row)}
    ordered = iter(sorted(finalists, key=lambda key: (-scores[key], rank[key])))
    head = [None] * (len(finalists) + len(fixed))
    for key in fixed:
        head[rank[key]] = key
    head = [slot if slot is not None else next(ordered) for slot in head]
    placed = set(head)
    return head + [key for key in row if key not in placed]


def ndcg_scores(row, grades, k=K):
    """den_index::eval::score."""
    def dcg(gains):
        return sum(g / math.log2(i + 2) for i, g in enumerate(gains))
    ideal = dcg(sorted((GRADE_GAIN[g] for g in grades.values()), reverse=True)[:k])
    ratio = (lambda d: d / ideal) if ideal > 0 else (lambda d: 0.0)
    top = row[:k]
    condensed = [key for key in row if key in grades][:k]
    return {"ndcg": ratio(dcg(GRADE_GAIN.get(grades.get(key), 0.0) for key in top)),
            "condensed": ratio(dcg(GRADE_GAIN[grades[key]] for key in condensed)),
            "precision": sum(grades.get(key) in ("good", "ok") for key in top) / k,
            "bad": sum(grades.get(key) == "bad" for key in top),
            "judged": sum(key in grades for key in top)}


def judged_grades(judged_path, cross_path, franchises_path):
    primary = {key: value.get("primary") for key, value in _read_json(franchises_path)["titles"].items()}
    cases = {}
    for path, cross in ((judged_path, False), (cross_path, True)):
        for case in _read_json(path)["cases"]:
            seed = dataset_key(*case["seed"].split(":"))
            grades = cases.setdefault(seed, {"split": case["split"], "grades": {}})["grades"]
            for item in case["judged"]:
                key = dataset_key(*item["id"].split(":"))
                if cross and kind(key) == kind(seed):
                    raise ValueError(f"{path}: {seed} cross grade {key} is the seed's own type")
                grades[key] = item["grade"]
    reserved = 0
    for seed, case in cases.items():
        mine = primary.get(seed)
        kept = {key: grade for key, grade in case["grades"].items() if not (mine and primary.get(key) == mine)}
        reserved += len(case["grades"]) - len(kept)
        case["grades"] = kept
    return cases, primary, reserved


def _mean(values):
    return sum(values) / len(values) if values else None


def _paired_interval(pairs):
    rng = random.Random(BOOTSTRAP_SEED)
    diffs = []
    for _ in range(BOOTSTRAPS):
        sample = [pairs[rng.randrange(len(pairs))] for _ in pairs]
        diffs.append(sum(b - a for a, b in sample) / len(sample))
    diffs.sort()
    return [diffs[math.floor(0.025 * BOOTSTRAPS)], diffs[math.ceil(0.975 * BOOTSTRAPS) - 1]]


def judged_summary(rows):
    if not rows:
        return {"n": 0}
    out = {"n": len(rows)}
    for arm in ("atlas", "cascade"):
        out[arm] = {m: _mean([r[arm][m] for r in rows]) for m in ("condensed", "ndcg", "precision")}
        out[arm].update({m: sum(r[arm][m] for r in rows) for m in ("bad", "judged")})
    out["condensedDelta"] = out["cascade"]["condensed"] - out["atlas"]["condensed"]
    out["condensedDelta95"] = _paired_interval([(r["atlas"]["condensed"], r["cascade"]["condensed"]) for r in rows])
    out["badDelta"] = out["cascade"]["bad"] - out["atlas"]["bad"]
    return out


def ruler_summary(rows):
    out = {"n": len(rows)}
    for arm in ("atlas", "cascade"):
        present = [r for r in rows if r["want"][arm] is not None]
        both = [r for r in present if r["drop"][arm] is not None]
        out[arm] = {"wantPresent": len(present),
                    "wantGain": _mean([1 / math.log2(r["want"][arm] + 1) for r in present]),
                    f"want@{K}": sum(r["want"][arm] <= K for r in present),
                    f"drop@{K}": sum(r["drop"][arm] is not None and r["drop"][arm] <= K for r in rows),
                    "pairOrder": _mean([float(r["want"][arm] < r["drop"][arm]) for r in both]),
                    "pairs": len(both)}
    return out


def score(work, pool_path, ruler_path, judged_path, cross_path, franchises_path):
    registration = load_registered(work)
    for name, path in (("pool", pool_path), ("ruler", ruler_path), ("judged", judged_path),
                       ("judgedCross", cross_path), ("franchises", franchises_path)):
        if file_digest(path) != registration["inputs"][name]:
            raise ValueError(f"{path}: not the registered {name} input")
    pool = _read_json(pool_path)
    plan = _load(work, "plan.jsonl", registration["files"]["plan.jsonl"])
    final = _read_json(os.path.join(work, "finalize.json"))
    evidence_rows = {row["anchor"]: row for row in phase_rows(work, "evidence")}
    screened, weighed = answers(work, "screen"), answers(work, "evidence", list(evidence_rows.values()))
    rate = TypeSafe.RATE_PER_INPUT_TOKEN

    rows_after, outcome, cost = {}, {}, {}
    for entry in plan:
        anchor, row = entry["anchor"], entry["row"]
        screen_tokens = screened[anchor]["usage"]["input_tokens"] if anchor in screened else 0
        item = evidence_rows.get(anchor)
        evidence_tokens = weighed[anchor]["usage"]["input_tokens"] if anchor in weighed else 0
        cost[anchor] = {"screen": screen_tokens, "evidence": evidence_tokens,
                        "screened": anchor in screened, "weighed": anchor in weighed,
                        "finalists": len(item["finalists"]) if item else 0,
                        "promoted": len(item["promoted"]) if item else 0}
        if item is None or anchor not in weighed:
            rows_after[anchor] = row
            outcome[anchor] = final["noEvidenceCall"].get(anchor) or entry.get("fallback") or "evidence failed"
            continue
        scores = {key: weighed[anchor]["scores"][label] for label, key in item["labels"].items()}
        rows_after[anchor] = reranked(row, item["finalists"], item["fixed"], scores)
        outcome[anchor] = "reranked"

    def full(anchor, head):
        return head + pool["rows"][anchor][POOL_K:]

    def slices(anchor):
        return {"type": kind(anchor), "popularity": popularity_slice(percentile(pool, anchor))}

    cases, primary, reserved = judged_grades(judged_path, cross_path, franchises_path)
    judged_rows, franchise_leaks = [], 0
    for seed, case in cases.items():
        atlas_row = pool["rows"][seed]
        franchise_leaks += sum(1 for key in atlas_row if primary.get(seed) and primary.get(key) == primary[seed])
        judged_rows.append({"seed": seed, **slices(seed), "outcome": outcome[seed],
                            "atlas": ndcg_scores(atlas_row, case["grades"]),
                            "cascade": ndcg_scores(full(seed, rows_after[seed]), case["grades"])})
    ruler_rows = []
    for case in ruler_cases(ruler_path):
        anchor = case["anchor"]["key"]
        before, after = pool["rows"][anchor][:POOL_K], rows_after[anchor]

        def position(row, key):
            return row.index(key) + 1 if key in row else None
        ruler_rows.append({"anchor": anchor, **slices(anchor), "outcome": outcome[anchor],
                           "want": {"atlas": position(before, case["positive"]["key"]),
                                    "cascade": position(after, case["positive"]["key"])},
                           "drop": {"atlas": position(before, case["negative"]["key"]),
                                    "cascade": position(after, case["negative"]["key"])}})

    def by(rows, summary, **match):
        return summary([r for r in rows if all(r[k] == v for k, v in match.items())])

    judged = {"all": judged_summary(judged_rows), "films": by(judged_rows, judged_summary, type="film"),
              "series": by(judged_rows, judged_summary, type="series"),
              "popular": by(judged_rows, judged_summary, popularity="popular"),
              "outsidePopular": judged_summary([r for r in judged_rows if r["popularity"] != "popular"])}
    for band in ("mid", "long tail"):
        judged[band] = by(judged_rows, judged_summary, popularity=band)
    for t in ("film", "series"):
        for band in ("popular", "mid", "long tail"):
            judged[f"{t}s/{band}"] = by(judged_rows, judged_summary, type=t, popularity=band)
    ruler = {"all": ruler_summary(ruler_rows)}
    for band in ("popular", "mid", "long tail"):
        ruler[band] = by(ruler_rows, ruler_summary, popularity=band)

    def not_worse(s):
        return s["n"] > 0 and s["cascade"]["condensed"] >= s["atlas"]["condensed"] \
            and s["cascade"]["bad"] <= s["atlas"]["bad"]
    tail = ruler["long tail"]
    checks = {
        "P1": judged["all"]["cascade"]["condensed"] > judged["all"]["atlas"]["condensed"]
        and judged["all"]["cascade"]["bad"] <= judged["all"]["atlas"]["bad"],
        "P2": not_worse(judged["series"]),
        "P3": not_worse(judged["outsidePopular"]),
        "P4": tail["n"] > 0 and tail["cascade"]["wantGain"] >= tail["atlas"]["wantGain"]
        and tail["cascade"][f"drop@{K}"] <= tail["atlas"][f"drop@{K}"],
    }

    def cost_summary(anchors):
        c = [cost[a] for a in anchors]
        screened_c = [x for x in c if x["screened"]]
        weighed_c = [x for x in c if x["weighed"]]
        return {"anchors": len(c), "screened": len(screened_c), "weighed": len(weighed_c),
                "screenTokensPerCall": _mean([x["screen"] for x in screened_c]),
                "evidenceTokensPerCall": _mean([x["evidence"] for x in weighed_c]),
                "finalistsPerCall": _mean([x["finalists"] for x in weighed_c]),
                "promotedPerCall": _mean([x["promoted"] for x in weighed_c]),
                "usdPerAnchor": _mean([(x["screen"] + x["evidence"]) * rate for x in c]),
                "screenUsdPerAnchor": _mean([x["screen"] * rate for x in c]),
                "evidenceUsdPerAnchor": _mean([x["evidence"] * rate for x in c])}
    anchors = [e["anchor"] for e in plan]
    costs = {"all": cost_summary(anchors)}
    for t in ("film", "series"):
        costs[t] = cost_summary([a for a in anchors if kind(a) == t])
    for band in ("popular", "mid", "long tail"):
        costs[band] = cost_summary([a for a in anchors if slices(a)["popularity"] == band])

    # Diagnostics, not criteria: recognition by release year, and how much of each top ten is long tail.
    identity = {row["key"]: row for row in _load(work, "identity.jsonl", registration["files"]["identity.jsonl"])}
    by_year = {}
    for entry in plan:
        if entry.get("screen") and entry["anchor"] in screened:
            for label, key in entry["screen"].items():
                year = identity[key].get("year")
                bucket = "unknown" if not year else "<1980" if year < 1980 else str(min(year // 5 * 5, 2020))
                hit = screened[entry["anchor"]]["scores"][label] >= RECOGNISED
                n, h = by_year.get(bucket, (0, 0))
                by_year[bucket] = (n + 1, h + hit)

    def tail_share(row):
        return _mean([float(popularity_slice(percentile(pool, key)) == "long tail") for key in row[:K]])
    return {
        "schema": f"{SCHEMA}-result", "datasetVersion": registration["datasetVersion"],
        "preregistrationSha256": file_digest(os.path.join(work, "preregistration.json")),
        "outcomes": {o: sum(1 for v in outcome.values() if v == o) for o in sorted(set(outcome.values()))},
        "judged": judged, "ruler": ruler, "checks": checks, "passed": all(checks.values()),
        "reservedFranchiseGrades": reserved, "franchiseTitlesInAtlasRows": franchise_leaks,
        "cost": costs, "spentUSD": recorded_tokens(work) * rate,
        "recognitionByYear": {k: {"screened": n, "recognisedShare": h / n} for k, (n, h) in sorted(by_year.items())},
        "longTailShareOfTop10": {"atlas": _mean([tail_share(pool["rows"][a][:POOL_K]) for a in anchors]),
                                 "cascade": _mean([tail_share(rows_after[a]) for a in anchors])},
        "screenFallbacks": {"postCutoffAnchors": sum(1 for e in plan if "fallback" not in e and e["screen"] is None)},
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    get = sub.add_parser("fetch")
    get.add_argument("--atlas", required=True, help="atlas base URL; never written into an artifact")
    get.add_argument("--expect-version", required=True)
    for p in (get,):
        p.add_argument("--ruler", required=True)
        p.add_argument("--judged", required=True)
        p.add_argument("--out", required=True)
    prep = sub.add_parser("prepare")
    for name in ("pool", "ruler", "judged", "judged-cross", "franchises", "articles", "enriched-dir", "work"):
        prep.add_argument(f"--{name}", required=True)
    prep.add_argument("--max-spend-usd", type=float, required=True)
    for name in ("screen", "evidence"):
        ask = sub.add_parser(name)
        ask.add_argument("--work", required=True)
        ask.add_argument("--spend", action="store_true")
        ask.add_argument("--workers", type=int, default=4)
        ask.add_argument("--env", help="optional den.env path (the key is never written to an artifact)")
    fin = sub.add_parser("finalize")
    fin.add_argument("--work", required=True)
    scoring = sub.add_parser("score")
    for name in ("work", "pool", "ruler", "judged", "judged-cross", "franchises", "out"):
        scoring.add_argument(f"--{name}", required=True)
    args = parser.parse_args(argv)
    if args.command == "fetch":
        result = fetch(args.atlas, args.expect_version, args.ruler, args.judged, args.out)
    elif args.command == "prepare":
        result = prepare(args.pool, args.ruler, args.judged, args.judged_cross, args.franchises, args.articles,
                         args.enriched_dir, args.work, args.max_spend_usd)
    elif args.command in PHASES:
        result = run(args.work, args.command, args.spend, args.workers, env_path=args.env)
    elif args.command == "finalize":
        result = finalize(args.work)
    else:
        result = score(args.work, args.pool, args.ruler, args.judged, args.judged_cross, args.franchises)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
