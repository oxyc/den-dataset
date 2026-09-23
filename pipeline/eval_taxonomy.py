#!/usr/bin/env python3
"""Score the published labels against the hand-labelled golden set, and gate a publish on it.

    pipeline/eval_taxonomy.py out-repass/labels-t02.json            # report
    pipeline/eval_taxonomy.py out-repass/labels-t02.json --gate     # …and exit 1 below baseline - tolerance
    pipeline/eval_taxonomy.py out-repass/labels-t02.json --record   # make these scores the baseline
    pipeline/eval_taxonomy.py data/genres-moods-curated.json --gate # the committed genres & moods

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

## A blank golden entry is no evidence, not a verdict of "no labels apply"

23% of the golden set's entries carry no subgenres or themes (604 of 2,623), and 23% carry no moods (599).
Those blanks are not judgements that nothing applies — the golden set was written from the labeller's own
knowledge, and for those titles it simply left the family empty. Scoring a prediction against an empty
expected set can only ever record false positives, so every label the producer adds there is wrong by
construction: The Big Valley (tv:11577), a 1965 Western family saga, is marked down for `Historical/Period
Drama` because the golden list is empty.

So a title is scored in a family only where the golden set has labels in that family. It still counts in
the families the golden set does answer: a title with moods but no subgenres scores in `mood` and is
skipped in `subgenre`. `primaryGenre` is never blank and is always scored.

It is worth 0.031 of subgenre micro F1 and 0.059 of mood micro F1 on the committed genres & moods — a
penalty that was never about the labels.

This removes a penalty, so it is checked against being hollowed out the same way the overall coverage is:
each family must still score `--min-coverage` of the golden set, and `titlesScored` is in the report.

## The baseline is a ratchet, the tolerance is a band around it

`data/eval/quality-floors.json` records a **baseline** — the scores of the labels that currently ship, with
the date, the sha256 of the labels file they were measured on, and the conditions they were measured under
— and a **tolerance**. `--gate` refuses a family scoring below `baseline - tolerance`.

The baseline moves only under `--record`, so the scores cannot walk downhill. A tolerance compared against
the *last recorded* scores would let exactly that happen: every run a little worse, every run re-recording
its own slightly worse ruler, and nothing ever fails. A band around a baseline that an operator has to move
deliberately absorbs ordinary movement without ever moving the reference point on its own. `--record`
refuses to lower the baseline unless `--accept-drop` says so, so a drop is a decision with a diff.

The tolerance is 0.005 — see `TOLERANCE` for how it was measured.

The baseline is exact, not rounded: unchanged labels score identically, and a rounded number would put a
second, invisible tolerance on top of the recorded one.

It used to be a hard floor, and the floors before that were constants carried over from
`ShippedDatasetEvalTests`, a few points under the scores they were set against. The labels that shipped
after that scored under two of them (mood micro .639 vs .640, macro .564 vs .580), so the publisher could
only run the gate as a report — enforcing it would have refused the next publish of unchanged labels, over
a regression that had already shipped. A ratchet starts from what is actually out there.

A baseline measured on another golden set, support threshold, confidence floor or scoring rule does not
describe these scores, so `--gate` refuses to compare against one.

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
#: The families a golden entry can leave blank, and whose blanks are therefore skipped rather than scored.
BLANKABLE = ("subgenre", "mood")
FLOORS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "eval", "quality-floors.json")
MIN_SUPPORT = 10

# How far under the baseline a family may score before `--gate` refuses.
#
# Measured, not guessed. Three probes over `data/genres-moods-curated.json` and the genres & moods the
# stage derives from it, worst movement across all six family/metric pairs:
#
#   * nudging every per-label threshold in `data/genres-moods-rule.json` by ±0.02 to ±0.05, which changes
#     the labels kept for 141 to 413 derived titles — **0.0004**;
#   * filling the 2,285 curated titles that had neither subgenres nor moods (the ask #56 bought) — 0.0022
#     as the scores used to be measured, **0.0009** as they are measured now;
#   * dropping the weakest label from a random 1% of every title, three seeds — **0.0042**.
#
# And what a real regression costs, same probe: dropping a label from 5% of titles moves 0.0080 to 0.0144.
# Two tests hold that end down — a shuffled mood in `pipeline/eval_taxonomy_test.py` and a derive rule that
# keeps the wrong labels in `pipeline/genres_moods_test.py` — and both are refused at this band.
#
# 0.005 is above everything an ordinary producer change costs and below the cheapest regression measured.
#
# It is written into the recorded file, not read from here, so the band the gate used is in git next to
# the baseline it was applied to; this constant is only the default a fresh `--record` starts from.
TOLERANCE = 0.005

#: Bumped when the scoring maths changes. A baseline recorded under other maths is refused rather than
#: compared: the same labels score differently, so the comparison would be about the change of ruler.
SCORING_RULE = "blank-golden-families-skipped"

#: `--record`'s exit code when these scores are under the baseline and `--accept-drop` was not given. Its
#: own code, not a plain 1: a caller that records the ratchet after a passing gate (`genres_moods_merge`)
#: has to tell "the baseline stays where it is" from "the file could not be written".
WOULD_LOWER = 3

# The golden set must still overlap the corpus. A big fall means the corpus shrank or the golden drifted
# off it, either of which quietly hollows the gate out rather than failing it — every remaining title could
# score perfectly while the check covered almost nothing.
#
# A RATIO, not a count. The Swift original asserted `evaluated > 1_900` against a 2,623-title golden, which
# is 72%; as an absolute it weakens every time the golden set grows, and it means nothing at all against a
# golden set of another size. 2,162 of 2,623 scored when this moved — 82%.
MIN_COVERAGE = 0.70


def labels_by_key(path):
    """`(mediaType, tmdbId)` → its record, from a labels artifact or from `data/genres-moods-curated.json`,
    whose `titles` are keyed `mediaType:tmdbId`."""
    with open(path, encoding="utf-8") as fh:
        blob = json.load(fh)
    if isinstance(blob, dict) and isinstance(blob.get("titles"), dict):
        return blob.get("taxonomyVersion"), {(k.split(":")[0], int(k.split(":")[1])): r
                                             for k, r in blob["titles"].items()}
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


def evaluate(golden, labels, floor=0.0, score_blank_golden=False):
    """Per-family, per-label true/false positives and negatives, how many titles were scored, and how many
    of them each family scored.

    A golden entry that names no labels in a blankable family gives no evidence there, so that title is
    skipped in that family — not scored against an empty expected set, where every prediction can only be a
    false positive. `score_blank_golden` restores the older behaviour, to measure what the rule moves.
    """
    counts = {family: defaultdict(lambda: [0, 0, 0]) for family in FAMILIES}
    scored = dict.fromkeys(FAMILIES, 0)
    evaluated = missing = 0

    def accumulate(family, expected, predicted):
        if not expected and family in BLANKABLE and not score_blank_golden:
            return
        scored[family] += 1
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
    return counts, evaluated, missing, scored


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


def mismatched_conditions(recorded, golden_sha, min_support, confidence_floor):
    """The one condition under which the recorded baseline does not describe this run, or None.

    A baseline measured under other conditions is refused rather than compared. The same labels score
    differently against another golden set, support threshold, confidence floor or scoring rule, so
    comparing across them would pass or fail on the change of ruler, not on the labels.
    """
    for key, now in (("goldenSha256", golden_sha), ("minSupport", min_support),
                     ("confidenceFloor", confidence_floor), ("scoringRule", SCORING_RULE)):
        if recorded.get(key) != now:
            return (f"the baseline was measured with {key} {recorded.get(key)!r}, this run uses {now!r} — "
                    f"re-record it against the new one (--record) and commit")
    return None


def against_baseline(scores, recorded):
    """(refusals, movements) against the recorded baseline and its tolerance.

    A refusal is a family more than `tolerance` under its baseline. A movement is any family under its
    baseline at all, refused or not, so a drop inside the band is still reported rather than silent.
    """
    tolerance = recorded.get("tolerance", 0.0)
    refusals, movements = [], []
    for family in FAMILIES:
        for metric in ("microF1", "macroF1"):
            base = recorded["baseline"][family][metric]
            now = scores[family][metric]
            if now >= base:
                continue
            movements.append(f"{family} {metric} {now:.4f} is {base - now:.4f} under the baseline "
                             f"{base:.4f}")
            if now < base - tolerance:
                refusals.append(f"{family} {metric} {now:.4f} < baseline {base:.4f} - tolerance "
                                f"{tolerance:g}")
    return refusals, movements


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("labels", help="labels-t02.json as published")
    ap.add_argument("--golden", default="data/eval/golden-large.json")
    ap.add_argument("--min-support", type=int, default=MIN_SUPPORT)
    ap.add_argument("--confidence-floor", type=float, default=0.0)
    ap.add_argument("--min-coverage", type=float, default=MIN_COVERAGE,
                    help="the share of the golden set that must be in the labels")
    ap.add_argument("--floors", default=FLOORS,
                    help="the recorded baseline (default: data/eval/quality-floors.json)")
    ap.add_argument("--gate", action="store_true",
                    help="exit 1 when a family scores more than the tolerance under its baseline")
    ap.add_argument("--record", action="store_true",
                    help="write these scores to --floors as the new baseline, with today's date and the "
                         "labels' sha256")
    ap.add_argument("--accept-drop", action="store_true",
                    help="with --record: lower the baseline where these scores are under it")
    args = ap.parse_args()

    with open(args.golden, encoding="utf-8") as fh:
        golden = json.load(fh)
    version, labels = labels_by_key(args.labels)
    if version and golden.get("taxonomyVersion") != version:
        sys.exit(f"eval: golden is {golden.get('taxonomyVersion')} but the labels are {version} — "
                 f"scoring one taxonomy's predictions against another's vocabulary measures nothing")

    counts, evaluated, missing, titles_scored = evaluate(golden, labels, args.confidence_floor)
    failures = []
    scores = {}
    report = {"taxonomyVersion": version, "evaluated": evaluated, "missingFromLabels": missing,
              "families": {}}
    for family in FAMILIES:
        micro, macro, scored = family_scores(counts[family], args.min_support)
        scores[family] = {"microF1": micro, "macroF1": macro}
        report["families"][family] = {"microF1": round(micro, 4), "macroF1": round(macro, 4),
                                      "labelsScored": scored, "titlesScored": titles_scored[family]}
        # A family that scored NO labels fails, deliberately: with nothing to score both F1s are vacuously
        # 1.0, so the gate would certify a family that had disappeared.
        if scored == 0:
            failures.append(f"{family}: no labels had {args.min_support}+ golden positives to score")
        # Skipping the titles the golden set leaves blank in this family is the same hollowing-out risk as
        # a corpus that shrank away from the golden set, and is guarded the same way: a golden set that
        # went mostly blank here would leave a handful of titles scoring perfectly for a whole family.
        family_coverage = titles_scored[family] / max(1, len(golden["titles"]))
        if family_coverage < args.min_coverage:
            failures.append(f"{family} scored only {titles_scored[family]} of {len(golden['titles'])} "
                            f"golden titles ({family_coverage:.0%}, want {args.min_coverage:.0%}+) — most "
                            f"of the golden set says nothing about this family")
    coverage = evaluated / max(1, len(golden["titles"]))
    report["coverage"] = round(coverage, 4)
    if coverage < args.min_coverage:
        failures.append(f"only {evaluated} of {len(golden['titles'])} golden titles are in the labels "
                        f"({coverage:.0%}, want {args.min_coverage:.0%}+) — the gate is being hollowed "
                        f"out, not passed")
    print(json.dumps(report, indent=1))

    golden_sha = sha256(args.golden)
    previous = None
    if os.path.exists(args.floors):
        with open(args.floors, encoding="utf-8") as fh:
            previous = json.load(fh)

    if args.record:
        # A baseline taken from a vacuous or hollowed-out score would certify anything above nothing.
        if failures:
            print("\neval: refusing to record a baseline from a run that cannot be scored:", file=sys.stderr)
            for line in failures:
                print(f"       {line}", file=sys.stderr)
            return 1
        # Lowering the baseline is what makes a tolerance safe to have, so it is never a side effect of
        # recording an improvement elsewhere: a drop is stated and asked for.
        comparable = previous is not None and not mismatched_conditions(
            previous, golden_sha, args.min_support, args.confidence_floor)
        drops = against_baseline(scores, {**previous, "tolerance": 0.0})[0] if comparable else []
        if drops and not args.accept_drop:
            print("\neval: these scores are UNDER the baseline recorded on "
                  f"{previous.get('measuredOn')}, so recording them would lower it:", file=sys.stderr)
            for line in drops:
                print(f"       {line}", file=sys.stderr)
            print("       Recording a drop is a decision, not a side effect. If it is deliberate:\n"
                  f"         pipeline/eval_taxonomy.py {args.labels} --record --accept-drop", file=sys.stderr)
            return WOULD_LOWER
        recorded = {
            "measuredOn": datetime.datetime.now(datetime.timezone.utc).date().isoformat(),
            "labels": os.path.basename(args.labels),
            "labelsSha256": sha256(args.labels),
            "goldenSha256": golden_sha,
            "minSupport": args.min_support,
            "confidenceFloor": args.confidence_floor,
            "scoringRule": SCORING_RULE,
            # An operator who widened or narrowed the band keeps it; only a first record picks the default.
            "tolerance": previous.get("tolerance", TOLERANCE) if previous else TOLERANCE,
            "baseline": scores,
        }
        with open(args.floors, "w", encoding="utf-8") as fh:
            json.dump(recorded, fh, indent=1)
            fh.write("\n")
        print(f"\neval: recorded these scores as the baseline in {args.floors}", file=sys.stderr)
        return 0

    if previous is None:
        sys.exit(f"{args.floors}: no baseline recorded — measure one with --record")
    recorded = previous
    mismatch = mismatched_conditions(recorded, golden_sha, args.min_support, args.confidence_floor)
    if mismatch:
        failures.append(mismatch)
        movements = []
    else:
        refusals, movements = against_baseline(scores, recorded)
        failures += refusals
    if failures:
        print(f"\neval: the labels are below the baseline recorded on {recorded.get('measuredOn')} "
              f"(labels sha256 {recorded.get('labelsSha256', '')[:12]}…):", file=sys.stderr)
        for line in failures:
            print(f"       {line}", file=sys.stderr)
        print("       If the drop is deliberate, record these scores as the new baseline and commit them:\n"
              f"         pipeline/eval_taxonomy.py {args.labels} --record --accept-drop", file=sys.stderr)
        return 1 if args.gate else 0
    if movements:
        # Inside the tolerance, so not a refusal — but a drop nobody was told about is how a band turns
        # into a slide.
        print(f"\neval: within the {recorded.get('tolerance', 0.0):g} tolerance, but under the baseline:",
              file=sys.stderr)
        for line in movements:
            print(f"       {line}", file=sys.stderr)
    if any(scores[f][m] > recorded["baseline"][f][m] for f in FAMILIES for m in ("microF1", "macroF1")):
        # Nothing raises the baseline on its own; an improvement that is not recorded can be given back.
        print(f"\neval: above the baseline — once these labels ship, raise it: "
              f"pipeline/eval_taxonomy.py {args.labels} --record", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
