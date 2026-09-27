#!/usr/bin/env python3
"""Preregister and run the bounded Jev gate for a More Like This reranker (#132).

The paid input is deliberately not committed: step 9d is derived from MovieLens.  This tool turns an
explicit 9d export plus ``articles.jsonl`` into a frozen work directory before any call is made.  Running
without ``--spend`` only validates that directory and reports its exact call/state size.
"""
import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import random
import re
import sys
import threading
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from lib.typesafe_client import TypeSafe, api_key  # noqa: E402
from pipeline import run_combined as combined  # noqa: E402
from pipeline.article_sections import parse_sections  # noqa: E402

SCHEMA = "jev-more-like-gate-v1"
RULER_SCHEMA = "issue18-step9d-export-v1"
MODEL = "jev-1.13.0"
SAMPLE_SIZE = 512
MIN_POPULATION = 2_100
MAX_EVIDENCE_CHARS = 28_000
MAX_STATE_CHARS = 100_000
BOOTSTRAPS = 2_000
BOOTSTRAP_SEED = 20260927
DEFAULT_SPEND_CAP = 1.0
# System One's short-state price probe measured a ~265-token fixed component (#18 step 9b).  Reserve nearly
# four times that on every request, then pessimistically count every UTF-8 byte of the exact JSON body as a
# token.  A byte-level tokenizer cannot emit more tokens than input bytes; the separate allowance covers the
# provider's measured hidden wrapper.  This is intentionally much larger than the estimate used for reporting:
# it exists so the spend cap can be checked BEFORE, rather than after, a paid request.
PROVIDER_WRAPPER_TOKEN_CEILING = 1_024
SOURCE_COMMENT = "https://github.com/oxyc/den-dataset/issues/18#issuecomment-5784663498"

AXES = {
    "premise": "the central premise, conflicts and consequential events",
    "engine": "the recurring story engine or kind of situation that generates the narrative",
    "characters": "the central character roles, relationships and group dynamics",
    "experience": "the intended emotional and comic or dramatic experience",
    "world": "the kind of setting, social world and constraints that materially shape the story",
    "overall": "the overall expectation that someone wanting more of the anchor would want this work",
}
VERDICTS = {
    "strong": "A strong More Like This recommendation for the anchor.",
    "plausible": "A defensible recommendation, though important differences remain.",
    "weak": "Only a weak relationship; it should not displace clearly similar candidates.",
    "reject": "Not a useful More Like This recommendation for the anchor.",
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_digest(path):
    result = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def request_token_upper_bound(state, qs):
    """Conservative billed-token ceiling for the exact request TypeSafe serializes."""
    body = json.dumps({"state": state, "model": MODEL, "questions": qs}).encode()
    return len(body) + PROVIDER_WRAPPER_TOKEN_CEILING


def read_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        for number, line in enumerate(fh, 1):
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{number}: {exc}") from None


def questions():
    result = {}
    for candidate in ("a", "b"):
        for axis, meaning in AXES.items():
            result[f"{candidate}__{axis}"] = {
                "type": "noul",
                "instructions": (
                    f"Using the supplied article evidence, does candidate {candidate.upper()} match ANCHOR "
                    f"on {meaning}? Judge the works, not article-writing quality, fame, shared release year, "
                    "or broad genre alone."
                ),
            }
        result[f"{candidate}__verdict"] = {
            "type": "choice",
            "instructions": (
                f"How suitable is candidate {candidate.upper()} as a More Like This recommendation for "
                "ANCHOR? Use the article evidence and do not reward fame, release-year proximity or broad "
                "genre by themselves."
            ),
            "criteria": VERDICTS,
        }
    return result


def _work_key(row):
    return f"{row['mediaType']}:{row['tmdbId']}"


LANG_HEADING = re.compile(r"\{\{\s*lang(?:-[^|}]+)?\s*\|[^|{}]+\|([^{}]+)\}\}", re.IGNORECASE)


def comparable_heading(heading):
    """Bridge headings cleaned by Enterprise with old action-API headings that retain wiki emphasis."""
    while True:
        replaced = LANG_HEADING.sub(r"\1", heading)
        if replaced == heading:
            break
        heading = replaced
    return re.sub(r"''+", "", heading).strip().casefold()


def load_evidence_overrides(path):
    if not path:
        return {}, None
    with open(path, encoding="utf-8") as fh:
        blob = json.load(fh)
    if blob.get("schema") != "jev-more-like-evidence-overrides-v1" \
            or not isinstance(blob.get("rows"), list):
        raise ValueError(f"{path}: invalid evidence-override schema")
    rows = {}
    for row in blob["rows"]:
        key = row.get("key") if isinstance(row, dict) else None
        if not isinstance(key, str) or key in rows or not isinstance(row.get("plotSections"), list) \
                or any(not isinstance(heading, str) for heading in row["plotSections"]):
            raise ValueError(f"{path}: malformed or duplicate evidence override")
        rows[key] = row
    return rows, file_digest(path)


def attach_matching_evidence(records, directory, evidence_overrides=None):
    """Attach extractor metadata from the newest retained batch for this exact dumped article.

    A frozen article dump can legitimately predate a later grounding change.  `classify` wants the newest
    enriched row, but a retrospective gate needs the newest historical row whose article AND language match
    the prose it is about to send.  Mixing headings from the new grounding with old prose is invalid; so is
    silently ignoring a selected heading that no longer exists in the dumped article revision.
    """
    missing = {_work_key(rec) for rec in records if "year" not in rec or "plotSections" not in rec}
    source_batches = {}
    if missing:
        if not directory:
            raise SystemExit(
                f"article dump lacks year/plotSections for {len(missing):,} rows; pass --enriched-dir")
        try:
            names = os.listdir(directory)
        except OSError as exc:
            raise SystemExit(f"cannot read --enriched-dir {directory}: {exc}") from None
        batches = [(int(match.group(1)), name) for name in names
                   if (match := re.fullmatch(r"batch-(\d+)\.json", name))]
        wanted = {key: rec for rec in records if (key := _work_key(rec)) in missing}
        found = {}
        for _, name in sorted(batches, reverse=True):
            with open(os.path.join(directory, name), encoding="utf-8") as fh:
                batch = json.load(fh)
            for item in batch:
                key = f"{item.get('mediaType')}:{item.get('tmdbId')}"
                rec = wanted.get(key)
                if rec is None or key in found:
                    continue
                if item.get("plotArticle") == rec.get("article") \
                        and (item.get("plotLanguage") or "en") == (rec.get("language") or "en"):
                    found[key] = item
                    source_batches[key] = name
            if len(found) == len(missing):
                break
        absent = missing - set(found)
        if absent:
            raise SystemExit(
                f"--enriched-dir has no retained article/language-matching record for {len(absent):,} rows")
        for rec in records:
            if (item := found.get(_work_key(rec))) is not None:
                if "year" not in rec:
                    rec["year"] = item.get("year")
                if "plotSections" not in rec:
                    rec["plotSections"] = item.get("plotSections") or []
                rec["extractorArticleRevId"] = item.get("plotRevId")

    overrides, overrides_sha = load_evidence_overrides(evidence_overrides)
    for rec in records:
        key = _work_key(rec)
        if (override := overrides.get(key)) is not None:
            expected = {"article": rec.get("article"), "language": rec.get("language") or "en",
                        "articleRevId": rec.get("revId")}
            if any(override.get(field) != value for field, value in expected.items()):
                raise ValueError(f"{evidence_overrides}: {key} override identity/revision mismatch")
            rec["extractorPlotSectionsOriginal"] = list(rec.get("plotSections") or ())
            rec["plotSections"] = list(override["plotSections"])
            rec["extractorArticleRevId"] = override["articleRevId"]
            source_batches[key] = f"override:{overrides_sha}"

        original = list(rec.get("plotSections") or ())
        available = [section["heading"] for section in parse_sections(rec["text"])[1:]]
        by_comparable = {}
        for heading in available:
            by_comparable.setdefault(comparable_heading(heading), []).append(heading)
        matched, absent = [], []
        for heading in original:
            choices = by_comparable.get(comparable_heading(heading)) or []
            (matched if choices else absent).append(choices.pop(0) if choices else heading)
        if absent:
            raise SystemExit(
                f"{_work_key(rec)} extractor headings differ from dumped article: "
                f"{dict(Counter(absent))}; pass a revision-matched --evidence-overrides refresh")
        if matched != original:
            rec["extractorPlotSectionsOriginal"] = original
            rec["plotSections"] = matched

    evidence = {
        _work_key(rec): {
            "year": rec.get("year"), "plotSections": rec.get("plotSections") or [],
            "extractorPlotSectionsOriginal": rec.get("extractorPlotSectionsOriginal"),
            "article": rec.get("article"), "language": rec.get("language") or "en",
            "articleRevId": rec.get("revId"),
            "extractorArticleRevId": rec.get("extractorArticleRevId", rec.get("revId")),
            "enrichedBatch": source_batches.get(_work_key(rec)),
        }
        for rec in records
    }
    evidence["_overridesSha256"] = overrides_sha
    return digest(evidence)


def load_articles(path, enriched_dir=None, wanted=None, evidence_overrides=None):
    records = [row for row in read_jsonl(path) if wanted is None or _work_key(row) in wanted]
    # Old article dumps intentionally lack target year and the extractor's plot/story headings.  Recover
    # those from retained metadata for the exact dumped article, rather than quietly reducing every work
    # to its lead or mixing in the headings of a newer grounding.
    evidence_metadata_sha = attach_matching_evidence(records, enriched_dir, evidence_overrides)
    rows = {}
    for row in records:
        key = _work_key(row)
        if key in rows:
            raise ValueError(f"{path}: duplicate article key {key}")
        if not isinstance(row.get("text"), str) or not row["text"].strip():
            raise ValueError(f"{path}: {key} has no article text")
        rows[key] = row
    return rows, evidence_metadata_sha


def evidence(row):
    """Lead plus extractor-selected story sections, capped identically for every work."""
    sections = parse_sections(row["text"], row.get("plotSections") or ())
    selected = [section for section in sections if section["id"] == "s000" or section["extractorSelected"]]
    text = "\n\n".join(f"## {section['heading']}\n{section['text']}" for section in selected).strip()
    if not text:
        raise ValueError(f"{_work_key(row)} has no usable article evidence")
    return text[:MAX_EVIDENCE_CHARS]


def _ruler(blob, minimum_population=MIN_POPULATION, require_prior=True):
    if blob.get("schema") != RULER_SCHEMA:
        raise ValueError(f"ruler schema must be {RULER_SCHEMA!r}")
    if blob.get("sourceComment") != SOURCE_COMMENT:
        raise ValueError("ruler does not name the published step 9d contract")
    cases = blob.get("cases")
    if not isinstance(cases, list) or len(cases) != minimum_population:
        raise ValueError(f"ruler needs the exact {minimum_population:,}-case step-9d population")
    seen = set()
    for case in cases:
        pair_id = case.get("pairId")
        if not isinstance(pair_id, str) or not pair_id or pair_id in seen:
            raise ValueError("every ruler case needs a unique non-empty pairId")
        seen.add(pair_id)
        for role in ("anchor", "positive", "negative"):
            item = case.get(role)
            if not isinstance(item, dict) or not isinstance(item.get("key"), str):
                raise ValueError(f"{pair_id}: malformed {role}")
            if not item["key"].startswith("movie:"):
                raise ValueError(f"{pair_id}: 9d is movies-only")
        prior = case.get("titleYear")
        if prior is None and not require_prior:
            prior = None
        elif not isinstance(prior, dict) or any(not isinstance(prior.get(k), (int, float))
                                                for k in ("positive", "negative")):
            raise ValueError(f"{pair_id}: missing title+year prior scores")
        if prior is not None and any(isinstance(prior[k], bool) or not math.isfinite(prior[k])
                                     or not 0 <= prior[k] <= 1 for k in ("positive", "negative")):
            raise ValueError(f"{pair_id}: invalid title+year prior scores")
        plot = case.get("plot")
        if not isinstance(plot, dict) or any(isinstance(plot.get(k), bool)
                                             or not isinstance(plot.get(k), (int, float))
                                             or not math.isfinite(plot[k]) or not -1 <= plot[k] <= 1
                                             for k in ("positive", "negative")):
            raise ValueError(f"{pair_id}: missing or invalid plot cosine scores")
        controls = case.get("controls") or {}
        if controls.get("yearGapEqual") is not True or controls.get("seedGenreEqual") is not True:
            raise ValueError(f"{pair_id}: year and genre are not controlled as step 9d requires")
        if isinstance(controls.get("popularityLogGap"), bool) \
                or not isinstance(controls.get("popularityLogGap"), (int, float)) \
                or not math.isfinite(controls["popularityLogGap"]) \
                or abs(controls["popularityLogGap"]) > 0.12:
            raise ValueError(f"{pair_id}: popularity is outside step 9d's 0.12 caliper")
    return cases


def _chosen(cases, sample_size):
    if len(cases) < sample_size:
        raise ValueError(f"need {sample_size} cases, got {len(cases)}")
    return sorted(cases, key=lambda case: hashlib.sha256(
        f"den-dataset#132-v1\0{case['pairId']}".encode()).digest())[:sample_size]


def build_state(case, articles):
    works = {}
    for role in ("anchor", "positive", "negative"):
        item = case[role]
        row = articles.get(item["key"])
        if row is None:
            raise ValueError(f"{case['pairId']}: {item['key']} has no article")
        article = evidence(row)
        # The ruler deliberately retains the MovieLens title/year used by the title-only arm.  Article state
        # uses the current catalogue's canonical identity: aliases and year corrections must not make a valid
        # ruler key unusable, and the two arms remain independently hashable in the worklist.
        works[role] = {"key": item["key"], "title": row.get("title") or item["title"],
                       "year": row.get("year") or item["year"],
                       "wikipediaTitle": row.get("article"), "articleEvidence": article,
                       "articleSha256": hashlib.sha256(row["text"].encode()).hexdigest(),
                       "evidenceSha256": hashlib.sha256(article.encode()).hexdigest()}
    # Never tell Jev which candidate MovieLens called positive.  The swap is stable and independent of every
    # score, so a resumed run sees the same blinded state.
    positive_label = "a" if hashlib.sha256(
        f"den-dataset#132-blind-v1\0{case['pairId']}".encode()).digest()[0] & 1 else "b"
    negative_label = "b" if positive_label == "a" else "a"
    state = {"task": "More Like This pair comparison",
             "works": {"anchor": works["anchor"], positive_label: works["positive"],
                       negative_label: works["negative"]}}
    if len(canonical(state)) > MAX_STATE_CHARS:
        raise ValueError(f"{case['pairId']}: state exceeds {MAX_STATE_CHARS:,} chars")
    return state, positive_label


def prepare(ruler_path, articles_path, work, sample_size=SAMPLE_SIZE, minimum_population=MIN_POPULATION,
            enriched_dir=None, evidence_overrides=None):
    with open(ruler_path, encoding="utf-8") as fh:
        blob = json.load(fh)
    cases = _chosen(_ruler(blob, minimum_population, require_prior=False), sample_size)
    missing_prior = [case["pairId"] for case in cases if case.get("titleYear") is None]
    if missing_prior:
        raise ValueError(f"ruler lacks fresh title+year prior scores for {len(missing_prior)} selected cases")
    wanted = {case[role]["key"] for case in cases for role in ("anchor", "positive", "negative")}
    articles, evidence_metadata_sha = load_articles(
        articles_path, enriched_dir, wanted, evidence_overrides)
    qs = questions()
    os.makedirs(work, exist_ok=True)
    rows = []
    for case in cases:
        state, positive_label = build_state(case, articles)
        rows.append({"pairId": case["pairId"], "state": state, "stateSha256": digest(state),
                     "positiveLabel": positive_label,
                     "titleYear": case["titleYear"], "plot": case.get("plot"),
                     "controls": case["controls"]})
    worklist = os.path.join(work, "worklist.jsonl")
    worklist_text = "".join(canonical(row) + "\n" for row in rows)
    worklist_sha = hashlib.sha256(worklist_text.encode()).hexdigest()
    registration = {
        "schema": SCHEMA, "issue": "oxyc/den-dataset#132", "sourceComment": SOURCE_COMMENT,
        "model": MODEL, "sampleMethod": "smallest sha256('den-dataset#132-v1\\0' + pairId)",
        "sampleSize": len(rows), "populationSize": len(blob["cases"]),
        "rulerSha256": file_digest(ruler_path), "articlesSha256": file_digest(articles_path),
        "evidenceMetadataSha256": evidence_metadata_sha,
        "worklistSha256": worklist_sha, "questionsSha256": digest(qs),
        "questions": {"axes": list(AXES), "verdicts": list(VERDICTS)},
        "primaryScore": "overall Noul; verdict and five component axes are diagnostics only",
        "bootstrap": {"unit": "pair", "resamples": BOOTSTRAPS, "seed": BOOTSTRAP_SEED},
        "gate": {"articleAucLower95AtLeast": 0.70, "articleMinusTitleYearLower95Above": 0.0,
                 "articleMinusPlotLower95Above": 0.0,
                 "actionOnFailure": "do not buy the corpus precompute"},
        "limitations": ["movies only", "MovieLens titles with at least 50 likes", "TV and long tail unmeasured"],
    }
    path = os.path.join(work, "preregistration.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            previous = json.load(fh)
        if previous != registration:
            raise ValueError(f"{path}: refusing to replace a different preregistration")
        if not os.path.exists(worklist) or file_digest(worklist) != worklist_sha:
            raise ValueError(f"{worklist}: changed or missing after preregistration")
        return previous
    with open(worklist + ".tmp", "w", encoding="utf-8") as fh:
        fh.write(worklist_text)
    os.replace(worklist + ".tmp", worklist)
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(registration, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(path + ".tmp", path)
    return registration


def load_plan(work):
    registration_path = os.path.join(work, "preregistration.json")
    worklist_path = os.path.join(work, "worklist.jsonl")
    with open(registration_path, encoding="utf-8") as fh:
        registration = json.load(fh)
    if registration.get("schema") != SCHEMA or registration.get("model") != MODEL:
        raise ValueError(f"{work}: incompatible preregistration")
    if file_digest(worklist_path) != registration.get("worklistSha256"):
        raise ValueError(f"{worklist_path}: changed after preregistration")
    if digest(questions()) != registration.get("questionsSha256"):
        raise ValueError("question contract changed after preregistration")
    rows = list(read_jsonl(worklist_path))
    if len(rows) != registration.get("sampleSize"):
        raise ValueError(f"{worklist_path}: row count changed after preregistration")
    for row in rows:
        if digest(row["state"]) != row.get("stateSha256"):
            raise ValueError(f"{row.get('pairId')}: state hash mismatch")
    return registration, rows


def _validate(answers, qs):
    combined.validate_answers(answers, qs)


def run(work, out, spend=False, client=None, max_spend=DEFAULT_SPEND_CAP, workers=1, env_path=None):
    if not math.isfinite(max_spend) or max_spend <= 0:
        raise ValueError("max spend must be a positive finite dollar amount")
    registration, rows = load_plan(work)
    done = {}
    prior_tokens = 0
    if os.path.exists(out):
        for row in read_jsonl(out):
            if row.get("pairId") in done:
                raise ValueError(f"{out}: duplicate {row.get('pairId')}")
            source = next((r for r in rows if r["pairId"] == row.get("pairId")), None)
            if source is None or row.get("stateSha256") != source["stateSha256"] \
                    or row.get("questionsSha256") != registration["questionsSha256"]:
                raise ValueError(f"{out}: answer provenance mismatch for {row.get('pairId')}")
            if row.get("model") != MODEL or row.get("requestedModel") != MODEL:
                raise ValueError(f"{out}: answer model mismatch for {row.get('pairId')}")
            expected_upper = request_token_upper_bound(source["state"], questions())
            if row.get("requestTokenUpperBound") != expected_upper:
                raise ValueError(f"{out}: request ceiling mismatch for {row.get('pairId')}")
            usage = row.get("usage") or {}
            tokens = usage.get("input_tokens")
            if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
                raise ValueError(f"{out}: invalid recorded input usage for {row.get('pairId')}")
            prior_tokens += tokens
            done[row["pairId"]] = row
    todo = [row for row in rows if row["pairId"] not in done]
    qs = questions()
    upper_tokens = [request_token_upper_bound(row["state"], qs) for row in todo]
    rate = TypeSafe.RATE_PER_INPUT_TOKEN
    plan = {"cases": len(rows), "answered": len(done), "callsPlanned": len(todo),
            "stateChars": sum(len(canonical(row["state"])) for row in todo),
            "questionsPerCall": len(qs), "model": MODEL, "spendCapUSD": max_spend,
            "spentRecordedUSD": prior_tokens * rate,
            "remainingRequestsTokenUpperBound": sum(upper_tokens),
            "remainingRequestsSpendUpperBoundUSD": sum(upper_tokens) * rate,
            "finishWithinCapIfEveryRequestHitsUpperBound":
                (prior_tokens + sum(upper_tokens)) * rate <= max_spend}
    if not spend:
        return plan
    if not isinstance(workers, int) or workers < 1:
        raise ValueError("workers must be a positive integer")
    # Reserve the entire remaining serialized request ceiling before starting. This makes parallel calls safe:
    # no in-flight set can overshoot the cap, even if every request reaches its byte-level upper bound.
    if (prior_tokens + sum(upper_tokens)) * rate > max_spend:
        next_upper_tokens = upper_tokens[0] if upper_tokens else 0
        raise RuntimeError(
            f"refusing the next request: the remaining requests' ${sum(upper_tokens) * rate:.6f} "
            f"conservative ceiling would cross the ${max_spend:.2f} spend cap")
    client = client or TypeSafe(key=api_key(env_path), model=MODEL)
    lock = threading.Lock()
    fh = open(out, "a", encoding="utf-8")

    def one(row, next_upper_tokens):
        actual_tokens = getattr(client, "input_tokens", 0)
        if isinstance(actual_tokens, bool) or not isinstance(actual_tokens, int) or actual_tokens < 0:
            raise ValueError("provider client reports invalid input-token usage")
        # The whole-run reservation above is load-bearing. Keep the request-local validation too, so a resume
        # records and checks exactly the same ceiling as a single-worker run.
        if (prior_tokens + actual_tokens + next_upper_tokens) * rate > max_spend:
            raise RuntimeError(
                f"refusing the next request: its ${next_upper_tokens * rate:.6f} conservative ceiling "
                f"would cross the ${max_spend:.2f} spend cap")
        answers, metadata = client.ask_with_metadata(row["state"], qs)
        _validate(answers, qs)
        if metadata.get("model") != MODEL:
            raise ValueError(f"provider returned {metadata.get('model')!r}, expected {MODEL!r}")
        usage = metadata.get("usage") or {}
        if isinstance(usage.get("input_tokens"), bool) \
                or not isinstance(usage.get("input_tokens"), int) or usage["input_tokens"] < 0:
            raise ValueError("provider returned invalid input-token usage")
        if usage["input_tokens"] > next_upper_tokens:
            raise ValueError("provider usage exceeded the preregistered per-request token ceiling")
        result = {"pairId": row["pairId"], "stateSha256": row["stateSha256"],
                  "questionsSha256": registration["questionsSha256"], "model": metadata["model"],
                  "requestedModel": MODEL, "answers": answers, "usage": usage,
                  "requestTokenUpperBound": next_upper_tokens}
        with lock:
            fh.write(canonical(result) + "\n")
            fh.flush()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(lambda args: one(*args), zip(todo, upper_tokens)))
    finally:
        fh.close()
    return {**plan, "written": len(todo), "inputTokens": getattr(client, "input_tokens", 0),
            "spendUSD": getattr(client, "spend", 0.0)}


def _auc(rows, getter):
    values = []
    for row in rows:
        positive, negative = getter(row)
        values.append(1.0 if positive > negative else 0.5 if positive == negative else 0.0)
    return sum(values) / len(values)


def _interval(rows, getter):
    rng = random.Random(BOOTSTRAP_SEED)
    values = []
    for _ in range(BOOTSTRAPS):
        sample = [rows[rng.randrange(len(rows))] for _ in rows]
        values.append(getter(sample))
    values.sort()
    return [values[math.floor(0.025 * BOOTSTRAPS)], values[math.ceil(0.975 * BOOTSTRAPS) - 1]]


def score(work, answers_path):
    registration, planned = load_plan(work)
    answers = {}
    for row in read_jsonl(answers_path):
        if row.get("pairId") in answers:
            raise ValueError(f"{answers_path}: duplicate {row.get('pairId')}")
        answers[row.get("pairId")] = row
    if set(answers) != {row["pairId"] for row in planned}:
        raise ValueError("answers are not an exact cover of the preregistered worklist")
    rows = []
    for source in planned:
        answer = answers[source["pairId"]]
        if answer.get("stateSha256") != source["stateSha256"] \
                or answer.get("questionsSha256") != registration["questionsSha256"]:
            raise ValueError(f"{source['pairId']}: answer provenance mismatch")
        _validate(answer["answers"], questions())
        positive = source["positiveLabel"]
        negative = "b" if positive == "a" else "a"
        rows.append({**source, "article": {
            "positive": answer["answers"][f"{positive}__overall"]["noul"],
            "negative": answer["answers"][f"{negative}__overall"]["noul"]}})
    article_auc = _auc(rows, lambda row: (row["article"]["positive"], row["article"]["negative"]))
    prior_auc = _auc(rows, lambda row: (row["titleYear"]["positive"], row["titleYear"]["negative"]))
    plot_rows = [row for row in rows if isinstance(row.get("plot"), dict)]
    plot_auc = _auc(plot_rows, lambda row: (row["plot"]["positive"], row["plot"]["negative"])) \
        if len(plot_rows) == len(rows) else None
    article_ci = _interval(rows, lambda sample: _auc(sample, lambda row: (
        row["article"]["positive"], row["article"]["negative"])))
    delta_prior_ci = _interval(rows, lambda sample: _auc(sample, lambda row: (
        row["article"]["positive"], row["article"]["negative"])) - _auc(
            sample, lambda row: (row["titleYear"]["positive"], row["titleYear"]["negative"])))
    delta_plot_ci = None
    if plot_auc is not None:
        delta_plot_ci = _interval(rows, lambda sample: _auc(sample, lambda row: (
            row["article"]["positive"], row["article"]["negative"])) - _auc(
                sample, lambda row: (row["plot"]["positive"], row["plot"]["negative"])))
    passed = article_ci[0] >= 0.70 and delta_prior_ci[0] > 0 \
        and delta_plot_ci is not None and delta_plot_ci[0] > 0
    return {"schema": f"{SCHEMA}-result", "sampleSize": len(rows),
            "articleAuc": article_auc, "articleAuc95": article_ci,
            "titleYearAuc": prior_auc, "articleMinusTitleYear": article_auc - prior_auc,
            "articleMinusTitleYear95": delta_prior_ci, "plotAuc": plot_auc,
            "articleMinusPlot": article_auc - plot_auc if plot_auc is not None else None,
            "articleMinusPlot95": delta_plot_ci, "passed": passed,
            "decision": "eligible for full precompute" if passed else "do not buy full precompute",
            "limitations": registration["limitations"]}


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--ruler", required=True)
    prep.add_argument("--articles", required=True)
    prep.add_argument("--enriched-dir", help="newest-wins enriched batches for an older article dump")
    prep.add_argument("--evidence-overrides",
                      help="revision-matched no-model extractor refreshes for changed dumped articles")
    prep.add_argument("--work", required=True)
    ask = sub.add_parser("run")
    ask.add_argument("--work", required=True)
    ask.add_argument("--out", required=True)
    ask.add_argument("--spend", action="store_true")
    ask.add_argument("--max-spend-usd", type=float, default=DEFAULT_SPEND_CAP)
    ask.add_argument("--workers", type=int, default=1)
    ask.add_argument("--env", help="optional den.env path (the key is never written to gate artifacts)")
    scoring = sub.add_parser("score")
    scoring.add_argument("--work", required=True)
    scoring.add_argument("--answers", required=True)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        result = prepare(args.ruler, args.articles, args.work, enriched_dir=args.enriched_dir,
                         evidence_overrides=args.evidence_overrides)
    elif args.command == "run":
        result = run(args.work, args.out, args.spend, max_spend=args.max_spend_usd,
                     workers=args.workers, env_path=args.env)
    else:
        result = score(args.work, args.answers)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
