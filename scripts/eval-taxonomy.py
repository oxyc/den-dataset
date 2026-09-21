#!/usr/bin/env python3
"""Score the published labels against the hand-labelled golden set, and gate a publish on it.

    scripts/eval-taxonomy.py out-repass/labels-t02.json            # report
    scripts/eval-taxonomy.py out-repass/labels-t02.json --gate     # …and exit 1 below the floors

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

## `--min-support`

Labels with fewer than this many golden positives are dropped before scoring, mirroring the producer's own
flag. Two reasons: a 3-title label swings a macro mean, and a label the golden set predates has zero
positives so it can only ever register false positives — gating on it would fail a publish for using newer
vocabulary than the golden set knows.
"""
import argparse
import json
import sys
from collections import defaultdict

# The floors, carried over from `ShippedDatasetEvalTests` with the values they were set against:
# primary .787/.765, subgenre .786/.790, mood .686/.632 at min-support 10. Each floor sits a few points
# under the measured value — headroom for a deliberate small change, not for noise.
MIN_MICRO_F1 = {"primaryGenre": 0.75, "subgenre": 0.74, "mood": 0.64}
MIN_MACRO_F1 = {"primaryGenre": 0.72, "subgenre": 0.74, "mood": 0.58}
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
    macro = sum(f1(*t) for t in kept.values()) / len(kept)
    return f1(tp, fp, fn), macro, len(kept)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("labels", help="labels-t02.json as published")
    ap.add_argument("--golden", default="data/eval/golden-large.json")
    ap.add_argument("--min-support", type=int, default=MIN_SUPPORT)
    ap.add_argument("--confidence-floor", type=float, default=0.0)
    ap.add_argument("--min-coverage", type=float, default=MIN_COVERAGE,
                    help="the share of the golden set that must be in the labels")
    ap.add_argument("--gate", action="store_true", help="exit 1 when a family is under its floor")
    args = ap.parse_args()

    with open(args.golden, encoding="utf-8") as fh:
        golden = json.load(fh)
    version, labels = labels_by_key(args.labels)
    if version and golden.get("taxonomyVersion") != version:
        sys.exit(f"eval: golden is {golden.get('taxonomyVersion')} but the labels are {version} — "
                 f"scoring one taxonomy's predictions against another's vocabulary measures nothing")

    counts, evaluated, missing = evaluate(golden, labels, args.confidence_floor)
    failures = []
    report = {"taxonomyVersion": version, "evaluated": evaluated, "missingFromLabels": missing,
              "families": {}}
    for family in ("primaryGenre", "subgenre", "mood"):
        micro, macro, scored = family_scores(counts[family], args.min_support)
        report["families"][family] = {"microF1": round(micro, 4), "macroF1": round(macro, 4),
                                      "labelsScored": scored}
        # A family you set a floor on that scored NO labels fails, deliberately: with nothing to score both
        # F1s are vacuously 1.0, so the gate would certify a family that had disappeared.
        if scored == 0:
            failures.append(f"{family}: no labels had {args.min_support}+ golden positives to score")
            continue
        if micro < MIN_MICRO_F1[family]:
            failures.append(f"{family} microF1 {micro:.3f} < required {MIN_MICRO_F1[family]:.3f}")
        if macro < MIN_MACRO_F1[family]:
            failures.append(f"{family} macroF1 {macro:.3f} < required {MIN_MACRO_F1[family]:.3f}")
    coverage = evaluated / max(1, len(golden["titles"]))
    report["coverage"] = round(coverage, 4)
    if coverage < args.min_coverage:
        failures.append(f"only {evaluated} of {len(golden['titles'])} golden titles are in the labels "
                        f"({coverage:.0%}, want {args.min_coverage:.0%}+) — the gate is being hollowed "
                        f"out, not passed")

    print(json.dumps(report, indent=1))
    if failures:
        print("\neval: the labels are below their quality floors:", file=sys.stderr)
        for line in failures:
            print(f"       {line}", file=sys.stderr)
        return 1 if args.gate else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
