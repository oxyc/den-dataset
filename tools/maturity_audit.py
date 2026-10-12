#!/usr/bin/env python3
"""Build a reproducible human-audit queue from verified public maturity observations.

Every title whose official observations disagree is included. The remaining queue is a deterministic
hash sample per authority, so rerunning the same artifact and seed selects the same records. This tool is
public-baseline only: it performs no network requests and must never be given private TMDB runtime data.
"""
import argparse
import collections
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import cache  # noqa: E402

SAFE_OBSERVATION_FIELDS = ("authority", "authorityId", "raw", "age", "matchedBy", "assessed",
                           "runtimeMinutes")


def load(path):
    with open(path, "rb") as handle:
        body = handle.read()
    try:
        root = json.loads(body.decode("utf-8"))
        rows = root["records"]
    except (UnicodeDecodeError, ValueError, KeyError, TypeError):
        raise ValueError(f"{path}: expected a maturity records list") from None
    if not isinstance(rows, list):
        raise ValueError(f"{path}: expected a maturity records list")
    found = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"{path}: malformed maturity record")
        media, ident, minimum = row.get("mediaType"), row.get("tmdbId"), row.get("minimumAge")
        observations = row.get("observations")
        if (media not in ("movie", "tv") or isinstance(ident, bool)
                or not isinstance(ident, int) or ident <= 0 or isinstance(minimum, bool)
                or (minimum is not None and (not isinstance(minimum, int) or not 0 <= minimum <= 18))
                or not isinstance(observations, list) or not observations):
            raise ValueError(f"{path}: malformed maturity record")
        key = f"{media}:{ident}"
        if key in found:
            raise ValueError(f"{path}: duplicate maturity record {key}")
        safe = []
        for observation in observations:
            if not isinstance(observation, dict):
                raise ValueError(f"{path}: malformed maturity observation for {key}")
            authority, age, method = (observation.get(name) for name in
                                      ("authority", "age", "matchedBy"))
            if (not isinstance(authority, str) or not authority or isinstance(age, bool)
                    or (age is not None and (not isinstance(age, int) or not 0 <= age <= 18))
                    or not isinstance(method, str) or not method):
                raise ValueError(f"{path}: malformed maturity observation for {key}")
            safe.append({name: observation[name] for name in SAFE_OBSERVATION_FIELDS
                         if name == "age" or observation.get(name) is not None})
        ages = {observation["age"] for observation in safe if observation["age"] is not None}
        if minimum != (max(ages) if ages else None):
            raise ValueError(f"{path}: minimumAge is not the highest observation for {key}")
        found[key] = {"mediaType": media, "tmdbId": ident, "minimumAge": minimum,
                      "observations": sorted(safe, key=lambda value: (
                          value["authority"], value["age"] is None, value["age"] or 0,
                          str(value.get("authorityId", ""))))}
    return root.get("datasetVersion"), hashlib.sha256(body).hexdigest(), found


def _score(seed, authority, key):
    return hashlib.sha256(f"{seed}\x1f{authority}\x1f{key}".encode()).digest()


def build(path, per_authority=50, seed="maturity-audit-v1"):
    if per_authority < 1:
        raise ValueError("per_authority must be positive")
    version, digest, records = load(path)
    conflicts, eligible = [], collections.defaultdict(list)
    authority_titles = collections.Counter()
    method_titles = collections.Counter()
    for key, record in records.items():
        authorities = {row["authority"] for row in record["observations"]}
        methods = {(row["authority"], row["matchedBy"]) for row in record["observations"]}
        for authority in authorities:
            authority_titles[authority] += 1
        for pair in methods:
            method_titles[pair] += 1
        if len({row["age"] for row in record["observations"] if row["age"] is not None}) > 1:
            conflicts.append(record)
            continue
        for authority in authorities:
            eligible[authority].append((_score(seed, authority, key), key))

    selected = collections.defaultdict(set)
    for authority, candidates in eligible.items():
        for _score_value, key in sorted(candidates)[:per_authority]:
            selected[key].add(authority)
    samples = [{**records[key], "sampledFor": sorted(authorities)}
               for key, authorities in sorted(selected.items())]
    return {
        "schema": 1,
        "datasetVersion": version,
        "sourceSha256": digest,
        "seed": seed,
        "perAuthority": per_authority,
        "summary": {
            "matchedTitles": len(records),
            "conflictingTitles": len(conflicts),
            "sampledTitles": len(samples),
            "authorityTitles": dict(sorted(authority_titles.items())),
            "matchMethodTitles": {f"{authority}:{method}": count
                                  for (authority, method), count in sorted(method_titles.items())},
            "sampledForAuthority": dict(sorted(collections.Counter(
                authority for values in selected.values() for authority in values).items())),
        },
        "conflicts": sorted(conflicts, key=lambda row: (row["mediaType"], row["tmdbId"])),
        "samples": samples,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--maturity", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--per-authority", type=int, default=50)
    parser.add_argument("--seed", default="maturity-audit-v1")
    args = parser.parse_args(argv)
    try:
        report = build(args.maturity, args.per_authority, args.seed)
    except ValueError as refusal:
        parser.error(str(refusal))
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    cache.write_atomically(args.out, (json.dumps(report, ensure_ascii=False, indent=2,
                                                 sort_keys=True) + "\n").encode())
    print(json.dumps(report["summary"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
