#!/usr/bin/env python3
"""Build a deterministic human-review queue for unresolved Taiwan BAMID rows.

The queue suggests a corpus movie only when an official source title has one exact normalized match.
It never writes the authority decision ledger: a person must verify each suggestion before copying it.
"""
import argparse
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import cache  # noqa: E402
from pipeline import maturity, maturity_decisions, maturity_taiwan  # noqa: E402
from pipeline.contract import StageError  # noqa: E402


def _year(fact):
    value = fact.get("released") or fact.get("started") or {}
    date = value.get("date") if isinstance(value, dict) else value
    match = re.match(r"^[+-]?(\d{1,4})", str(date or ""))
    return int(match.group(1)) if match else None


def _facts(path):
    with open(path, "rb") as handle:
        body = handle.read()
    try:
        root = json.loads(body.decode("utf-8"))
        rows = root["records"]
    except (UnicodeDecodeError, ValueError, KeyError, TypeError):
        raise ValueError(f"{path}: expected a facts records list") from None
    if not isinstance(rows, list):
        raise ValueError(f"{path}: expected a facts records list")
    targets, index = {}, {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"{path}: malformed facts record")
        media, ident = row.get("mediaType"), row.get("tmdbId")
        if media != "movie" or isinstance(ident, bool) or not isinstance(ident, int) or ident <= 0:
            continue
        titles = row.get("titles") or {}
        if not isinstance(titles, dict) or not isinstance(titles.get("aliases", []), list):
            raise ValueError(f"{path}: malformed titles for movie:{ident}")
        values = [titles.get("en"), titles.get("orig"), *titles.get("aliases", [])]
        display = list(dict.fromkeys(value.strip() for value in values
                                     if isinstance(value, str) and value.strip()))
        normalized = {value for raw in display if (value := maturity.normalized(raw))}
        key = f"movie:{ident}"
        if key in targets:
            raise ValueError(f"{path}: duplicate facts record {key}")
        targets[key] = {"key": key, "titles": display, "year": _year(row)}
        for title in normalized:
            index.setdefault(title, set()).add(key)
    return hashlib.sha256(body).hexdigest(), targets, index


def _score(seed, authority_id):
    return hashlib.sha256(f"{seed}\x1f{authority_id}".encode()).digest()


def build(facts_path, snapshot_path, limit=200, seed="bamid-review-v1", decisions=None):
    if limit < 1:
        raise ValueError("limit must be positive")
    facts_sha, targets, index = _facts(facts_path)
    with open(snapshot_path, "rb") as handle:
        snapshot = handle.read()
    rows = list(maturity_taiwan.records(snapshot))
    if decisions is None:
        decisions, _digest = maturity_decisions.load()

    counts = {"sourceRows": len(rows), "ratedRows": 0, "unknownRatingRows": 0,
              "missingIdentityRows": 0,
              "alreadyDecidedRows": 0,
              "uniqueExactTitleRows": 0, "ambiguousExactTitleRows": 0,
              "noExactTitleRows": 0, "queuedRows": 0}
    eligible = []
    for row in rows:
        if row["age"] is None:
            counts["unknownRatingRows"] += 1
            continue
        counts["ratedRows"] += 1
        if not row.get("authorityId"):
            counts["missingIdentityRows"] += 1
            continue
        decision_key = ("BAMID", row["authorityId"])
        if decision_key in decisions:
            counts["alreadyDecidedRows"] += 1
            continue
        source_titles = {title for raw in row["titles"] if (title := maturity.normalized(raw))}
        candidate_keys = set().union(*(index.get(title, set()) for title in source_titles)) if source_titles else set()
        if not candidate_keys:
            counts["noExactTitleRows"] += 1
            continue
        if len(candidate_keys) != 1:
            counts["ambiguousExactTitleRows"] += 1
            continue
        counts["uniqueExactTitleRows"] += 1
        target_key = next(iter(candidate_keys))
        target = targets[target_key]
        target_titles = {title for raw in target["titles"] if (title := maturity.normalized(raw))}
        exact = sorted(source_titles & target_titles)
        source = {name: row[name] for name in ("authorityId", "titles", "raw", "age", "runtimeMinutes")
                  if row.get(name) is not None}
        candidate = {"source": source, "target": target, "exactTitles": exact,
                     "decision": {"authority": "BAMID", "authorityId": row["authorityId"],
                                  "key": target_key}}
        eligible.append((_score(seed, row["authorityId"]), row["authorityId"], candidate))

    selected = [candidate for _score_value, _authority_id, candidate in sorted(eligible)[:limit]]
    selected.sort(key=lambda row: row["source"]["authorityId"])
    counts["queuedRows"] = len(selected)
    return {"schema": 1, "authority": "BAMID", "factsSha256": facts_sha,
            "sourceSha256": hashlib.sha256(snapshot).hexdigest(), "seed": seed, "limit": limit,
            "summary": counts, "candidates": selected}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--facts", required=True)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--seed", default="bamid-review-v1")
    args = parser.parse_args(argv)
    try:
        report = build(args.facts, args.snapshot, args.limit, args.seed)
    except (OSError, ValueError, StageError) as refusal:
        parser.error(str(refusal))
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    cache.write_atomically(args.out, (json.dumps(report, ensure_ascii=False, indent=2,
                                                 sort_keys=True) + "\n").encode())
    print(json.dumps(report["summary"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
