#!/usr/bin/env python3
"""Build a deterministic, diagnosis-weighted article sample for a facet-vocabulary pilot.

The sample over-represents the two failures measured in the full Jev pass (ensemble ambiguity and pacing
on short articles), but keeps clean movie/TV controls and invalid articles. It writes ordinary article JSONL,
so `run_facets.py` can classify it without a special pilot path.
"""
import argparse
import hashlib
import json


def key(row):
    return row["mediaType"], row["tmdbId"]


def order(row, seed):
    raw = f"{seed}:{row['mediaType']}:{row['tmdbId']}".encode()
    return hashlib.sha256(raw).digest()


ap = argparse.ArgumentParser()
ap.add_argument("--articles", required=True)
ap.add_argument("--facets", required=True, help="completed v1 Jev facet JSONL")
ap.add_argument("--out", required=True)
ap.add_argument("--seed", default="facets-v2")
args = ap.parse_args()

articles = {key(row): row for row in map(json.loads, open(args.articles, encoding="utf-8"))}
facets = list(map(json.loads, open(args.facets, encoding="utf-8")))
selected = {}
counts = {}


def take(name, count, predicate):
    candidates = [row for row in facets if key(row) not in selected and predicate(row)]
    candidates.sort(key=lambda row: order(row, f"{args.seed}:{name}"))
    chosen = candidates[:count]
    for row in chosen:
        selected[key(row)] = articles[key(row)]
    counts[name] = len(chosen)


single = lambda row: row["facets"]["validity"]["value"] == "single-work"
dna = lambda row, axis: row["facets"][axis]["value"] == "does-not-apply"

take("ensemble-dna-movie", 50, lambda row: single(row) and row["mediaType"] == "movie" and dna(row, "ensemble"))
take("ensemble-dna-tv", 30, lambda row: single(row) and row["mediaType"] == "tv" and dna(row, "ensemble"))
take("pacing-dna-short", 40,
     lambda row: single(row) and dna(row, "pacing") and (row.get("articleChars") or 0) < 3_000)
take("pacing-dna-long", 20,
     lambda row: single(row) and dna(row, "pacing") and (row.get("articleChars") or 0) >= 3_000)
take("clean-movie", 40,
     lambda row: single(row) and row["mediaType"] == "movie"
     and not dna(row, "ensemble") and not dna(row, "pacing"))
take("clean-tv", 30,
     lambda row: single(row) and row["mediaType"] == "tv"
     and not dna(row, "ensemble") and not dna(row, "pacing"))
take("not-single-work", 20, lambda row: not single(row))

with open(args.out, "w", encoding="utf-8") as fh:
    for article in selected.values():
        fh.write(json.dumps(article, ensure_ascii=False) + "\n")

print(json.dumps({"out": args.out, "titles": len(selected), "strata": counts}, indent=2))
