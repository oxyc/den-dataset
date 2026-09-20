#!/usr/bin/env python3
"""Build `rail-facets-<version>.json` — the per-title signals den-atlas's More Like This scorer reads.

  scripts/v2/build_rail_facets.py --combined out-repass/combined-v1-r2.jsonl \
      --delta out-repass/delta-v1.jsonl --out out-repass/rail-facets-<version>.json

Four groups per title, each answering something the vectors and the shipped labels cannot:

  the 12 facet choices  — era, setting, scope, ending, pacing, chronology, continuity, conflict, ensemble,
                          tone, timespan, archetype, with the model's confidence. `continuity` is what
                          separates a serialised show from monster-of-the-week, which no label expresses.
  `__world`             — distance from a realist world, the max of twelve fantastical markers. The Wire
                          0.04, Angel 0.97. Scored as a DISTANCE and never as agreement: sharing "not
                          fantastical" is the corpus default and evidence of nothing.
  `__nouls`             — the 75 taxonomy nouls. labels-t02.json carries only a thresholded top three, so
                          the rail could never see the rest. Against The Wire the shipped `tone` term gives
                          Oz, Bates Motel, Generation Kill and The Deuce an identical 0.257; these separate
                          them.
  `__critique`          — the 17 axes of what a work ARGUES ABOUT. The only signal that links The Wire and
                          Oz, which agree on justice-system, institution and the-state while every vector
                          space and label family puts them far apart.

## Thresholds

Values below the cutoffs are dropped and probabilities are rounded to two places, which takes the blob
from 66 MB to 50 MB. Verified: the rail's mean same-genre share (47%) and single-subgenre share (15%) over
19 anchors are unchanged by the trim, and Homicide still lands at 21 on The Wire.

A `does-not-apply` choice is dropped rather than stored: it is the model declining, and a scorer that
counted it as agreement would pair every title that declined with every other.
"""
import argparse
import json

FACET_AXES = [
    "era", "setting", "scope", "ending", "pacing", "chronology",
    "continuity", "conflict", "ensemble", "tone", "timespan", "archetype",
]

# Markers of a non-realist world. Deliberately broad — a superhero show and a vampire show are both far
# from The Wire, and the axis only ever measures distance.
FANTASTICAL = [f"tax__theme__{k}" for k in (
    "vampire", "werewolf_monster", "zombie", "superhero", "time_travel", "cyberpunk",
    "dystopian_post_apocalyptic", "folk_horror",
)] + [f"tax__subgenre__{k}" for k in (
    "supernatural_horror", "sci_fi_horror", "sci_fi_action", "fantasy_adventure",
)]

NOUL_FLOOR = 0.20
CRITIQUE_FLOOR = 0.10
WORLD_FLOOR = 0.05


def answers(paths):
    """Every record across one or more shards of a pass."""
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                yield f"{record['mediaType']}:{record['tmdbId']}", record.get("answers") or {}


def main():
    ap = argparse.ArgumentParser()
    # The combined pass is a BUNDLE, not a file: oversized articles run in separately-manifested
    # capacity shards (`combined-v1-r2-token-fallback*.jsonl`), and FACETS-V2.md says completeness is
    # a bundle property to be validated over the disjoint union "before deriving or publishing
    # anything". Passed one shard, this built a blob of 47,518 of 47,529 titles and shipped it —
    # House of the Dragon and Moon Knight among the eleven it dropped, absent from every rail that
    # reads facets, nouls or world. Repeat --combined once per shard.
    ap.add_argument("--combined", required=True, action="append",
                    help="a shard of the corpus pass (facets, nouls, scores); repeat for each")
    ap.add_argument("--delta", required=True, action="append",
                    help="a shard of the delta pass (critique, depiction, technique); repeat for each")
    ap.add_argument("--expect", type=int, default=None,
                    help="required title count; fails rather than shipping a short blob")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    out = {}
    seen = set()
    for key, a in answers(args.combined):
        if key in seen:
            raise SystemExit(f"duplicate key across combined shards: {key}")
        seen.add(key)
        record = {}
        for axis in FACET_AXES:
            value = a.get(axis)
            if isinstance(value, dict) and value.get("choice") and value["choice"] != "does-not-apply":
                record[axis] = [value["choice"], round(float(value.get("confidence") or 0.0), 2)]
        world = max([float((a.get(k) or {}).get("noul") or 0.0) for k in FANTASTICAL] or [0.0])
        record["__world"] = round(world, 2) if world >= WORLD_FLOOR else 0
        nouls = {
            k[5:]: round(float(v["noul"]), 2)
            for k, v in a.items()
            if k.startswith("tax__") and isinstance(v, dict) and (v.get("noul") or 0.0) >= NOUL_FLOOR
        }
        if nouls:
            record["__nouls"] = nouls
        out[key] = record

    critiqued = 0
    for key, a in answers(args.delta):
        if key not in out:
            continue
        critique = {
            k[10:]: round(float(v["noul"]), 2)
            for k, v in a.items()
            if k.startswith("critique__") and isinstance(v, dict) and (v.get("noul") or 0.0) >= CRITIQUE_FLOOR
        }
        if critique:
            out[key]["__critique"] = critique
            critiqued += 1

    # A short blob is silent: every downstream rail just never returns the missing titles. Say the
    # number out loud and refuse to write one that does not match what the caller expects.
    if args.expect is not None and len(out) != args.expect:
        raise SystemExit(f"expected {args.expect} titles, built {len(out)} — a shard is missing")

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print(json.dumps({"titles": len(out), "withCritique": critiqued,
                      "combinedShards": len(args.combined), "deltaShards": len(args.delta),
                      "out": args.out}, indent=1))


if __name__ == "__main__":
    main()
