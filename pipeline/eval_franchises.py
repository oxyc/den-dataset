#!/usr/bin/env python3
"""The franchises' quality gate: a derived `franchises.json` against `data/franchise-golden.json`.

  pipeline/eval_franchises.py out/franchises.json [--gate]

The golden set is the hand-checked table of oxyc/den-atlas#92, as corpus keys. Each case is one franchise:

- `together`: titles that are one franchise. **Recall** is the share of their pairs the derive puts in
  one franchise.
- `eras`: lists of titles that are one era each. **Era agreement** is the share of pairs, over titles the
  derive put in the case's franchise, that it puts in the same era exactly when the golden set does.

and across cases:

- `apart`: pairs that must not share a franchise: Sherlock Holmes adaptations, Hammer and Universal's
  Dracula, the films of Studio Ghibli. Any one shared is a **violation**.
- `none`: titles that are no franchise at all. Any one given a franchise is a violation.

`--gate` exits 1 when a figure is below its floor in the golden file's `floors`; the stage calls
`evaluate` and `below_floors` itself. `--record` writes this run's figures as the floors: the step that
raises the bar after a pass that did better, never done by the stage on its own.
"""
import argparse
import itertools
import json
import sys


def present(golden, corpus):
    """The golden set cut down to the titles `corpus` holds: a case with fewer than two left is dropped. A
    delta or a fixture out-dir holds few or none of them, and a pair it cannot hold is no miss."""
    def keep(keys):
        return [k for k in keys if k in corpus]
    cases = []
    for case in golden["cases"]:
        together = keep(case["together"])
        if len(together) >= 2:
            cases.append({**case, "together": together,
                          "eras": [kept for kept in (keep(e) for e in case.get("eras") or []) if kept]})
    return {**golden, "cases": cases, "apart": [p for p in golden.get("apart") or [] if len(keep(p)) == 2],
            "none": keep(golden.get("none") or [])}


def evaluate(derived, golden, corpus=None):
    """The figures, over the keys the golden set names that `corpus` holds (every one, without it)."""
    if corpus is not None:
        golden = present(golden, corpus)
    of = derived.get("titles") or {}
    era = {m["key"]: m.get("era") for f in (derived.get("franchises") or {}).values() for m in f["members"]}
    pairs = hits = 0
    era_pairs = era_hits = 0
    for case in golden["cases"]:
        together = case["together"]
        for a, b in itertools.combinations(together, 2):
            pairs += 1
            hits += bool(of.get(a)) and of.get(a) == of.get(b)
        era_of = {k: i for i, keys in enumerate(case.get("eras") or []) for k in keys}
        placed = [k for k in era_of if of.get(k) and of.get(k) == of.get(together[0])]
        for a, b in itertools.combinations(placed, 2):
            era_pairs += 1
            era_hits += (era_of[a] == era_of[b]) == (era.get(a) == era.get(b))
    apart = [(a, b) for a, b in golden.get("apart") or [] if of.get(a) and of.get(a) == of.get(b)]
    none = [k for k in golden.get("none") or [] if of.get(k)]
    return {"togetherRecall": round(hits / pairs, 4) if pairs else 1.0, "togetherPairs": pairs,
            "eraAgreement": round(era_hits / era_pairs, 4) if era_pairs else 1.0, "eraPairs": era_pairs,
            "apartViolations": len(apart), "noneViolations": len(none),
            "violations": [f"{a} + {b}" for a, b in apart] + none}


def below_floors(result, floors):
    """What is below its floor, in words; empty when the result clears them all."""
    out = []
    for name in ("togetherRecall", "eraAgreement"):
        if result[name] < floors[name]:
            out.append(f"{name} {result[name]} < floor {floors[name]}")
    for name in ("apartViolations", "noneViolations"):
        if result[name] > floors[name]:
            out.append(f"{name} {result[name]} > floor {floors[name]}: {', '.join(result['violations'][:8])}")
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("franchises")
    parser.add_argument("--golden", default="data/franchise-golden.json")
    parser.add_argument("--gate", action="store_true")
    parser.add_argument("--facts", help="score only the golden titles this facts file holds")
    parser.add_argument("--record", action="store_true", help="write this run's figures as the floors")
    args = parser.parse_args(argv)
    with open(args.franchises, encoding="utf-8") as fh:
        derived = json.load(fh)
    with open(args.golden, encoding="utf-8") as fh:
        golden = json.load(fh)
    corpus = None
    if args.facts:
        with open(args.facts, encoding="utf-8") as fh:
            corpus = {f"{r['mediaType']}:{r['tmdbId']}" for r in json.load(fh)["records"]}
    result = evaluate(derived, golden, corpus)
    print(json.dumps(result, indent=2))
    if args.record:
        golden["floors"] = {name: result[name] for name in golden["floors"]}
        with open(args.golden, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(golden, ensure_ascii=False, indent=1) + "\n")
        return 0
    failures = below_floors(result, golden["floors"])
    for failure in failures:
        print(f"BELOW FLOOR: {failure}", file=sys.stderr)
    return 1 if args.gate and failures else 0


if __name__ == "__main__":
    sys.exit(main())
