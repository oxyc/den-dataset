#!/usr/bin/env python3
"""Build the fresh title+year control arm for the #132 More Like This gate."""
import argparse
import concurrent.futures
import json
import math
import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from more_like_gate import (MODEL, RULER_SCHEMA, SAMPLE_SIZE, SOURCE_COMMENT, TypeSafe, _chosen, _ruler,
                            canonical, digest, file_digest, read_jsonl)
from lib.typesafe_client import api_key

SCHEMA = "jev-more-like-title-prior-v1"
WRAPPER_TOKEN_CEILING = 1_024
QUESTION = {"co_rating": {"type": "noul", "instructions": (
    "Are people who enjoyed pair A unusually likely, compared with an average film, to also enjoy pair B?")}}


def request_ceiling(state):
    body = json.dumps({"state": state, "model": MODEL, "questions": QUESTION}).encode()
    return len(body) + WRAPPER_TOKEN_CEILING


def prepare(source, work, sample_size=SAMPLE_SIZE, minimum_population=2_100):
    with open(source, encoding="utf-8") as fh:
        blob = json.load(fh)
    cases = _chosen(_ruler(blob, minimum_population, require_prior=False), sample_size)
    rows = []
    for case in cases:
        for kind in ("positive", "negative"):
            state = {"pair": {"a": {"title": case["anchor"]["title"], "year": case["anchor"]["year"]},
                              "b": {"title": case[kind]["title"], "year": case[kind]["year"]}}}
            rows.append({"pairId": case["pairId"], "kind": kind, "state": state,
                         "stateSha256": digest(state)})
    os.makedirs(work, exist_ok=True)
    text = "".join(canonical(row) + "\n" for row in rows)
    manifest = {"schema": SCHEMA, "model": MODEL, "sourceSha256": file_digest(source),
                "sampleMethod": "smallest sha256('den-dataset#132-v1\\0' + pairId)",
                "sampleSize": len(cases), "calls": len(rows),
                "worklistSha256": __import__("hashlib").sha256(text.encode()).hexdigest(),
                "questionsSha256": digest(QUESTION),
                "requestTokenUpperBound": sum(request_ceiling(row["state"]) for row in rows)}
    worklist = os.path.join(work, "prior-worklist.jsonl")
    path = os.path.join(work, "prior-manifest.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            old = json.load(fh)
        if old != manifest or not os.path.exists(worklist) \
                or file_digest(worklist) != manifest["worklistSha256"]:
            raise ValueError("refusing to replace a different prior preregistration")
        return old
    with open(worklist + ".tmp", "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(worklist + ".tmp", worklist)
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(path + ".tmp", path)
    return manifest


def load_plan(work):
    with open(os.path.join(work, "prior-manifest.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    path = os.path.join(work, "prior-worklist.jsonl")
    if manifest.get("schema") != SCHEMA or manifest.get("model") != MODEL \
            or file_digest(path) != manifest.get("worklistSha256") \
            or digest(QUESTION) != manifest.get("questionsSha256"):
        raise ValueError("prior preregistration changed")
    return manifest, list(read_jsonl(path))


def run(work, out, spend=False, cap=1.0, workers=1, client=None, env_path=None):
    manifest, rows = load_plan(work)
    expected = {(row["pairId"], row["kind"]): row for row in rows}
    done, prior_tokens = {}, 0
    if os.path.exists(out):
        for answer in read_jsonl(out):
            key = (answer.get("pairId"), answer.get("kind"))
            source = expected.get(key)
            usage = answer.get("usage") or {}
            tokens = usage.get("input_tokens")
            if key in done or source is None or answer.get("stateSha256") != source["stateSha256"] \
                    or answer.get("questionsSha256") != manifest["questionsSha256"] \
                    or answer.get("model") != MODEL or isinstance(tokens, bool) \
                    or not isinstance(tokens, int) or tokens < 0:
                raise ValueError("incompatible prior answer row")
            done[key], prior_tokens = answer, prior_tokens + tokens
    todo = [row for row in rows if (row["pairId"], row["kind"]) not in done]
    ceilings = [request_ceiling(row["state"]) for row in todo]
    upper = (prior_tokens + sum(ceilings)) * TypeSafe.RATE_PER_INPUT_TOKEN
    plan = {"calls": len(rows), "answered": len(done), "callsPlanned": len(todo),
            "recordedInputTokens": prior_tokens, "remainingTokenUpperBound": sum(ceilings),
            "totalSpendUpperBoundUSD": upper, "capUSD": cap}
    if not spend:
        return plan
    if not math.isfinite(cap) or cap <= 0 or upper > cap:
        raise RuntimeError("refusing spend: the whole remaining prior run does not fit its conservative cap")
    if not isinstance(workers, int) or workers < 1:
        raise ValueError("workers must be a positive integer")
    client = client or TypeSafe(key=api_key(env_path), model=MODEL)
    lock = threading.Lock()
    fh = open(out, "a", encoding="utf-8")

    def one(row, ceiling):
        answers, metadata = client.ask_with_metadata(row["state"], QUESTION)
        answer = answers.get("co_rating")
        score = answer.get("noul") if isinstance(answer, dict) else None
        usage = metadata.get("usage") or {}
        if set(answers) != {"co_rating"} or set(answer or {}) != {"type", "noul"} \
                or answer["type"] != "noul" or isinstance(score, bool) \
                or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1 \
                or metadata.get("model") != MODEL or not isinstance(usage.get("input_tokens"), int) \
                or usage["input_tokens"] > ceiling:
            raise ValueError("malformed or over-ceiling prior response")
        result = {"pairId": row["pairId"], "kind": row["kind"],
                  "stateSha256": row["stateSha256"], "questionsSha256": manifest["questionsSha256"],
                  "model": MODEL, "score": score, "usage": usage, "requestTokenUpperBound": ceiling}
        with lock:
            fh.write(canonical(result) + "\n")
            fh.flush()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(lambda args: one(*args), zip(todo, ceilings)))
    finally:
        fh.close()
    return {**plan, "written": len(todo), "inputTokens": client.input_tokens, "spendUSD": client.spend}


def merge(source, work, answers, out):
    manifest, rows = load_plan(work)
    expected = {(row["pairId"], row["kind"]): row for row in rows}
    got = {}
    for answer in read_jsonl(answers):
        key = (answer.get("pairId"), answer.get("kind"))
        row = expected.get(key)
        if key in got or row is None or answer.get("stateSha256") != row["stateSha256"] \
                or answer.get("questionsSha256") != manifest["questionsSha256"]:
            raise ValueError("prior answers do not match the preregistration")
        got[key] = answer["score"]
    if set(got) != set(expected):
        raise ValueError(f"prior answers incomplete: {len(got)}/{len(expected)}")
    with open(source, encoding="utf-8") as fh:
        blob = json.load(fh)
    for case in blob["cases"]:
        key = (case["pairId"], "positive")
        if key in got:
            case["titleYear"] = {"positive": got[key], "negative": got[(case["pairId"], "negative")]}
    blob["freshTitlePrior"] = {"schema": SCHEMA, "model": MODEL,
                                "manifestSha256": file_digest(os.path.join(work, "prior-manifest.json")),
                                "answersSha256": file_digest(answers), "calls": len(got)}
    with open(out + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(blob, fh, separators=(",", ":"))
    os.replace(out + ".tmp", out)
    return {"out": out, "sha256": file_digest(out), "casesWithPrior": len(got) // 2}


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare"); prep.add_argument("--source", required=True); prep.add_argument("--work", required=True)
    ask = sub.add_parser("run"); ask.add_argument("--work", required=True); ask.add_argument("--out", required=True)
    ask.add_argument("--spend", action="store_true"); ask.add_argument("--max-spend-usd", type=float, default=1.0)
    ask.add_argument("--workers", type=int, default=1); ask.add_argument("--env")
    join = sub.add_parser("merge"); join.add_argument("--source", required=True); join.add_argument("--work", required=True)
    join.add_argument("--answers", required=True); join.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    if args.command == "prepare": result = prepare(args.source, args.work)
    elif args.command == "run": result = run(args.work, args.out, args.spend, args.max_spend_usd, args.workers, env_path=args.env)
    else: result = merge(args.source, args.work, args.answers, args.out)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
