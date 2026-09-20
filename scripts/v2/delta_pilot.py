#!/usr/bin/env python3
"""Gate the delta corpus pass on a 500-title pilot.

  scripts/v2/delta_pilot.py --pilot out-delta-pilot/answers.jsonl --combined out-repass/combined-v1-r2.jsonl

Exit 1 if any gate fails, so it can stand between the pilot and a ~$20 corpus run. Every gate below exists
because a specific, cheap failure would otherwise be discovered only after paying for the state.

The pilot itself is drawn with `split.half`, so the titles land in the same half as every other ruler and a
later evaluation cannot be tuned on them.
"""
import argparse
import json
import math
import statistics
import sys

# The pair this pass exists for: The Wire and Oz are the same kind of show to a human and NO feature on
# disk connects them. If the pilot does not separate them from Angel, the pass has no justification.
WIRE, OZ, ANGEL = "tv:1438", "tv:3322", "tv:2426"
INSTITUTIONAL = ["critique__institution", "critique__policing", "critique__justice-system"]

# `setting = institution` covers 1,064 of 7,529 tv titles — 14% — and its members include Night Court and
# Are You Being Served?. If `critique__institution` fires at a similar rate it has rebuilt that field.
SETTING_INSTITUTION_RATE = 0.14
PREVALENCE_CEILING = 0.20
# Above this, a new axis is the existing "serious vs light" factor (PC1 = 61.9%) wearing new clothes.
MAX_SCORE_CORRELATION = 0.80
# A Noul that moves this much between identical runs cannot support a threshold downstream.
MAX_RETEST_DELTA = 0.15


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return 0.0
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return num / (dx * dy) if dx and dy else 0.0


def load(path, key_field="answers"):
    out = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            out[f"{r['mediaType']}:{r['tmdbId']}"] = r
    return out


ap = argparse.ArgumentParser()
ap.add_argument("--pilot", required=True, help="the delta pilot's answers, JSONL")
ap.add_argument("--combined", required=True, help="combined-v1-r2.jsonl, for the existing Scores")
ap.add_argument("--retest", help="a second run over the same titles, JSONL")
args = ap.parse_args()

pilot = load(args.pilot)
noul = lambda rec, q: float((rec.get("answers", {}).get(q) or {}).get("noul") or 0.0)
failures, report = [], {}

# ---- Gate 1: has it rebuilt `setting = institution`? ------------------------------------------------
rates = {}
for q in sorted({q for r in pilot.values() for q in r.get("answers", {}) if q.startswith("critique__")}):
    fired = sum(1 for r in pilot.values() if noul(r, q) >= 0.70)
    rates[q] = fired / max(len(pilot), 1)
report["prevalence"] = {q: round(v, 3) for q, v in sorted(rates.items(), key=lambda kv: -kv[1])}
hot = {q: v for q, v in rates.items() if v > PREVALENCE_CEILING}
if hot:
    failures.append(
        f"these fire on more than {PREVALENCE_CEILING:.0%} of the pilot and have probably reproduced "
        f"`setting = institution` ({SETTING_INSTITUTION_RATE:.0%} of tv): "
        + ", ".join(f"{q} {v:.0%}" for q, v in sorted(hot.items(), key=lambda kv: -kv[1]))
    )

# ---- Gate 2: does it separate the pair it exists for? -----------------------------------------------
present = [k for k in (WIRE, OZ, ANGEL) if k in pilot]
if len(present) < 3:
    failures.append(f"the pilot must contain {WIRE}, {OZ} and {ANGEL}; missing {set([WIRE, OZ, ANGEL]) - set(present)}")
else:
    inst = {k: max(noul(pilot[k], q) for q in INSTITUTIONAL) for k in (WIRE, OZ, ANGEL)}
    report["institutionalCritique"] = {k: round(v, 3) for k, v in inst.items()}
    if not (inst[WIRE] >= 0.70 and inst[OZ] >= 0.70):
        failures.append(f"The Wire and Oz must both reach 0.70 on an institutional critique; got {inst}")
    if inst[ANGEL] >= 0.50:
        failures.append(f"Angel must stay below 0.50; got {inst[ANGEL]:.2f}")

# ---- Gate 3: is it the prestige axis again? ---------------------------------------------------------
combined = load(args.combined)
score = lambda rec, s: float((rec.get("answers", {}).get(s) or {}).get("score") or 0.0)
scores = ["score__intensity", "score__humour", "score__emotional_weight", "score__complexity"]
shared = [k for k in pilot if k in combined]
worst = {}
for q in rates:
    xs = [noul(pilot[k], q) for k in shared]
    for s in scores:
        r = abs(pearson(xs, [score(combined[k], s) for k in shared]))
        if r > worst.get(q, (0.0, ""))[0]:
            worst[q] = (r, s)
report["maxScoreCorrelation"] = {q: [round(r, 2), s] for q, (r, s) in sorted(worst.items(), key=lambda kv: -kv[1][0])[:8]}
collapsed = {q: v for q, v in worst.items() if v[0] > MAX_SCORE_CORRELATION}
if collapsed:
    failures.append(
        "these correlate above "
        f"{MAX_SCORE_CORRELATION} with an existing Score and are the serious-vs-light factor again: "
        + ", ".join(f"{q}~{s} {r:.2f}" for q, (r, s) in collapsed.items())
    )

# ---- Gate 4: is it measuring Wikipedia rather than the work? ----------------------------------------
lengths = [float(combined[k].get("articleChars") or 0) for k in shared]
by_len = {}
for q in rates:
    by_len[q] = abs(pearson([noul(pilot[k], q) for k in shared], lengths))
report["maxArticleLengthCorrelation"] = {q: round(v, 2) for q, v in sorted(by_len.items(), key=lambda kv: -kv[1])[:5]}
long_q = {q: v for q, v in by_len.items() if v > 0.50}
if long_q:
    failures.append(
        "these track article length more than 0.50 and are measuring editor diligence: "
        + ", ".join(f"{q} {v:.2f}" for q, v in long_q.items())
    )

# ---- Gate 5: is it stable? --------------------------------------------------------------------------
if args.retest:
    retest = load(args.retest)
    both = [k for k in pilot if k in retest]
    deltas = [abs(noul(pilot[k], q) - noul(retest[k], q)) for k in both for q in rates]
    if deltas:
        mean_delta = statistics.fmean(deltas)
        report["retestMeanDelta"] = round(mean_delta, 3)
        if mean_delta > MAX_RETEST_DELTA:
            failures.append(f"test-retest mean |delta| is {mean_delta:.3f}, above {MAX_RETEST_DELTA}")
else:
    report["retestMeanDelta"] = "not run (pass --retest)"

print(json.dumps({"titles": len(pilot), **report}, indent=1))
if failures:
    print("\nGATES FAILED — do not run the corpus pass:", file=sys.stderr)
    for f in failures:
        print(f"  - {f}", file=sys.stderr)
    sys.exit(1)
print("\nall gates passed")
