#!/usr/bin/env python3
"""Score the published labels against the hand-labelled golden set, and gate a publish on it.

    scripts/eval-taxonomy.py out-repass/labels-t02.json            # report
    scripts/eval-taxonomy.py out-repass/labels-t02.json --gate     # …and exit 1 below the floors
    scripts/eval-taxonomy.py out-repass/labels-t02.json --record   # make these scores the floors

This is the ONLY check in the pipeline that measures label QUALITY rather than plumbing. Everything else
asks whether the right number of records arrived in the right shape; this asks whether they are *correct*.

## Why it lives here now

It was `ShippedDatasetEvalTests` in the tvOS app, scoring the 45.8 MB index that app bundled. Phase 2 of
oxyc/den#113 deletes that bundle, and the test would have died with it — the one quality signal in the
whole system, lost as a side effect of a delivery change.

Here it runs against the labels as published, in the repo that produces them, so a classifier or prompt
regression fails the repo that caused it instead of surfacing months later as bad rows on a TV.

## Micro vs macro, and why both

Micro pools every prediction, so it is dominated by the common labels and answers "how right is this
overall". Macro averages per-label F1, so a sparse label counts as much as a common one and answers "is any
label broken". A gate on micro alone passes a build that destroyed every rare thematic; a gate on macro
alone is noisy when support is tiny. Both are reported and both are gated.

## The floors are a ratchet

The floors are the scores of the labels that currently ship, recorded in `data/eval/quality-floors.json`
with the date and the sha256 of the labels file they were measured on. `--gate` refuses a score below its
floor, so the labels can hold still or improve, never quietly get worse.

They used to be constants carried over from `ShippedDatasetEvalTests`, a few points under the scores they
were set against. The labels that shipped after that scored under two of them (mood micro .639 vs .640,
macro .564 vs .580), so the publisher could only run the gate as a report — enforcing it would have refused
the next publish of unchanged labels, over a regression that had already shipped. A ratchet starts from
what is actually out there and still refuses the next step down.

The floors are exact, not rounded: unchanged labels score identically, and a rounded floor would let a
drop smaller than the rounding through.

Raising or lowering them is `--record` and a commit, so every change to what counts as good enough is in
git with the labels it was measured on. A floor measured on another golden set, support threshold or
confidence floor does not describe these scores, so `--gate` refuses to compare against one.

## `--min-support`

Labels with fewer than this many golden positives are dropped before scoring, mirroring the producer's own
flag. Two reasons: a 3-title label swings a macro mean, and a label the golden set predates has zero
positives so it can only ever register false positives — gating on it would fail a publish for using newer
vocabulary than the golden set knows.
"""
import argparse
import datetime
import hashlib
import json
import math
import os
import sys
from collections import defaultdict

FAMILIES = ("primaryGenre", "subgenre", "mood")
FLOORS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "eval", "quality-floors.json")
MIN_SUPPORT = 10

# The golden set must still overlap the corpus. A big fall means the corpus shrank or the golden drifted
# off it, either of which quietly hollows the gate out rather than failing it — every remaining title could
# score perfectly while the check covered almost nothing.
#
# A RATIO, not a count. The Swift original asserted `evaluated > 1_900` against a 2,623-title golden, which
# is 72%; as an absolute it weakens every time the golden set grows, and it means nothing at all against a
# golden set of another size. 2,162 of 2,623 scored when this moved — 82%.
MIN_COVERAGE = 0.70


def labels_by_key(path):
    """`(mediaType, tmdbId)` → its record, from a labels artifact."""
    with open(path, encoding="utf-8") as fh:
        blob = json.load(fh)
    rows = blob.get("records") if isinstance(blob, dict) else blob
    if not isinstance(rows, list):
        sys.exit(f"{path}: expected a records array")
    return blob.get("taxonomyVersion"), {(r["mediaType"], r["tmdbId"]): r for r in rows
                                         if isinstance(r, dict) and "tmdbId" in r}


def named(entries, floor):
    """The label names from a published label list, at or above `floor`.

    The shape is `{"label": …, "confidence": …}`. Two other shapes are accepted because the artifact has
    worn them: a `[name, confidence]` pair, and a bare string.

    Getting this wrong is not loud. Reading the dict shape as a pair yields the string `"confidence"` as
    the label for every entry, which matches nothing in the golden set and scores **0.000** across the
    whole family — indistinguishable, from the gate's output alone, from a classifier that has completely
    collapsed. It did exactly that on the first run here.
    """
    out = set()
    for entry in entries or []:
        if isinstance(entry, dict):
            label, confidence = entry.get("label"), entry.get("confidence")
            if label and (confidence is None or float(confidence) >= floor):
                out.add(str(label))
        elif isinstance(entry, (list, tuple)) and entry:
            if len(entry) < 2 or float(entry[1]) >= floor:
                out.add(str(entry[0]))
        elif isinstance(entry, str):
            out.add(entry)
    return out


def evaluate(golden, labels, floor=0.0):
    """Per-family, per-label true/false positives and negatives, plus how many titles were scored."""
    counts = {family: defaultdict(lambda: [0, 0, 0]) for family in ("primaryGenre", "subgenre", "mood")}
    evaluated = missing = 0

    def accumulate(family, expected, predicted):
        for label in expected | predicted:
            tally = counts[family][label]
            if label in expected and label in predicted:
                tally[0] += 1
            elif label in predicted:
                tally[1] += 1
            else:
                tally[2] += 1

    for title in golden["titles"]:
        # A series and a film may share a tmdbId; the key is the pair.
        record = labels.get((title["mediaType"], title["tmdbId"]))
        if record is None:
            missing += 1
            continue
        evaluated += 1
        accumulate("primaryGenre", {title["primaryGenre"]}, {record.get("primaryGenre") or ""})
        # Themes and subgenres are ONE predicted family: the published taxonomy folds themes into
        # subgenres, and there is no `themes` field on a record.
        accumulate("subgenre",
                   set(title.get("subgenres") or []) | set(title.get("themes") or []),
                   named(record.get("subgenres"), floor))
        accumulate("mood", set(title.get("moods") or []), named(record.get("moods"), floor))

    if not evaluated:
        sys.exit("eval: the golden set and the labels do not overlap at all")
    return counts, evaluated, missing


def f1(tp, fp, fn):
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def family_scores(tallies, min_support):
    """(microF1, macroF1, scored label count) over labels with enough golden positives.

    Support is `tp + fn` — how many titles genuinely carry the label — not `tp + fp`, which a broken
    classifier can inflate by predicting a label everywhere.
    """
    kept = {label: t for label, t in tallies.items() if t[0] + t[2] >= min_support}
    if not kept:
        return 0.0, 0.0, 0
    tp = sum(t[0] for t in kept.values())
    fp = sum(t[1] for t in kept.values())
    fn = sum(t[2] for t in kept.values())
    # fsum, because the labels arrive in set-iteration order, which changes with every process's hash seed.
    # A plain sum in that order can differ in the last bit between two runs over the same labels, and the
    # floors are compared exactly.
    macro = math.fsum(f1(*t) for t in kept.values()) / len(kept)
    return f1(tp, fp, fn), macro, len(kept)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def below_floors(scores, recorded, golden_sha, min_support, confidence_floor):
    """Why `scores` fail the recorded floors: one line per reason, empty when they hold.

    Floors measured under other conditions are refused rather than compared. The same labels score
    differently against another golden set, support threshold or confidence floor, so comparing across them
    would pass or fail on the change of ruler, not on the labels.
    """
    for key, now in (("goldenSha256", golden_sha), ("minSupport", min_support),
                     ("confidenceFloor", confidence_floor)):
        if recorded.get(key) != now:
            return [f"the floors were measured with {key} {recorded.get(key)!r}, this run uses {now!r} — "
                    f"re-record them against the new one (--record) and commit"]
    out = []
    for family in FAMILIES:
        for metric in ("microF1", "macroF1"):
            floor = recorded["floors"][family][metric]
            if scores[family][metric] < floor:
                out.append(f"{family} {metric} {scores[family][metric]:.4f} < floor {floor:.4f}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("labels", help="labels-t02.json as published")
    ap.add_argument("--golden", default="data/eval/golden-large.json")
    ap.add_argument("--min-support", type=int, default=MIN_SUPPORT)
    ap.add_argument("--confidence-floor", type=float, default=0.0)
    ap.add_argument("--min-coverage", type=float, default=MIN_COVERAGE,
                    help="the share of the golden set that must be in the labels")
    ap.add_argument("--floors", default=FLOORS, help="the recorded floors (default: data/eval/quality-floors.json)")
    ap.add_argument("--gate", action="store_true", help="exit 1 when a family is under its recorded floor")
    ap.add_argument("--record", action="store_true",
                    help="write these scores to --floors as the new floors, with today's date and the labels' sha256")
    args = ap.parse_args()

    with open(args.golden, encoding="utf-8") as fh:
        golden = json.load(fh)
    version, labels = labels_by_key(args.labels)
    if version and golden.get("taxonomyVersion") != version:
        sys.exit(f"eval: golden is {golden.get('taxonomyVersion')} but the labels are {version} — "
                 f"scoring one taxonomy's predictions against another's vocabulary measures nothing")

    counts, evaluated, missing = evaluate(golden, labels, args.confidence_floor)
    failures = []
    scores = {}
    report = {"taxonomyVersion": version, "evaluated": evaluated, "missingFromLabels": missing,
              "families": {}}
    for family in FAMILIES:
        micro, macro, scored = family_scores(counts[family], args.min_support)
        scores[family] = {"microF1": micro, "macroF1": macro}
        report["families"][family] = {"microF1": round(micro, 4), "macroF1": round(macro, 4),
                                      "labelsScored": scored}
        # A family that scored NO labels fails, deliberately: with nothing to score both F1s are vacuously
        # 1.0, so the gate would certify a family that had disappeared.
        if scored == 0:
            failures.append(f"{family}: no labels had {args.min_support}+ golden positives to score")
    coverage = evaluated / max(1, len(golden["titles"]))
    report["coverage"] = round(coverage, 4)
    if coverage < args.min_coverage:
        failures.append(f"only {evaluated} of {len(golden['titles'])} golden titles are in the labels "
                        f"({coverage:.0%}, want {args.min_coverage:.0%}+) — the gate is being hollowed "
                        f"out, not passed")
    print(json.dumps(report, indent=1))

    golden_sha = sha256(args.golden)
    if args.record:
        # Floors taken from a vacuous or hollowed-out score would certify anything above nothing.
        if failures:
            print("\neval: refusing to record floors from a run that cannot be scored:", file=sys.stderr)
            for line in failures:
                print(f"       {line}", file=sys.stderr)
            return 1
        recorded = {
            "measuredOn": datetime.datetime.now(datetime.timezone.utc).date().isoformat(),
            "labels": os.path.basename(args.labels),
            "labelsSha256": sha256(args.labels),
            "goldenSha256": golden_sha,
            "minSupport": args.min_support,
            "confidenceFloor": args.confidence_floor,
            "floors": scores,
        }
        with open(args.floors, "w", encoding="utf-8") as fh:
            json.dump(recorded, fh, indent=1)
            fh.write("\n")
        print(f"\neval: recorded these scores as the floors in {args.floors}", file=sys.stderr)
        return 0

    with open(args.floors, encoding="utf-8") as fh:
        recorded = json.load(fh)
    failures += below_floors(scores, recorded, golden_sha, args.min_support, args.confidence_floor)
    if failures:
        print(f"\neval: the labels are below the floors recorded on {recorded.get('measuredOn')} "
              f"(labels sha256 {recorded.get('labelsSha256', '')[:12]}…):", file=sys.stderr)
        for line in failures:
            print(f"       {line}", file=sys.stderr)
        print("       If the drop is deliberate, record these scores as the new floors and commit them:\n"
              f"         scripts/eval-taxonomy.py {args.labels} --record", file=sys.stderr)
        return 1 if args.gate else 0
    if any(scores[f][m] > recorded["floors"][f][m] for f in FAMILIES for m in ("microF1", "macroF1")):
        # Nothing raises the floors on its own; an improvement that is not recorded can be given back.
        print(f"\neval: above the floors — once these labels ship, raise them: "
              f"scripts/eval-taxonomy.py {args.labels} --record", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
