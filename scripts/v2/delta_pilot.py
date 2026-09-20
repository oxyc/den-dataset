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
# Judged on the SHAPE of the critique profile, not on one axis.
#
# The first version took max(institution, policing, justice-system) and failed Angel at 0.55 — which was the
# statistic's fault, not the data's. Angel's profile is `the-self 0.85, religion 0.81`: a vampire-with-a-soul
# show about damnation, sharing one moderate axis with The Wire and none of its top three. The Wire's is
# policing 0.98 / class 0.98 / the-state 0.97. A single max cannot see that; cosine over the whole profile
# can, and it is also what the rescorer would actually use.
# Centered on the pilot's per-axis mean before comparing. Raw cosine over 17 mostly-low values is dominated
# by a shared baseline — it scored Oz 0.885 and Angel 0.792, which ranks them correctly and separates them
# by almost nothing. Subtracting the mean measures DISTINCTIVE agreement and the same pair goes to +0.700
# and +0.274.
MIN_PAIR_COSINE = 0.60   # The Wire and Oz are the pair this pass exists for.
MAX_ANGEL_COSINE = 0.45  # And Angel is the title it must keep away from them.

# `setting = institution` covers 1,064 of 7,529 tv titles — 14% — and its members include Night Court and
# Are You Being Served?. If `critique__institution` fires at a similar rate it has rebuilt that field.
SETTING_INSTITUTION_RATE = 0.14
PREVALENCE_CEILING = 0.20
# And a floor, because the first pilot passed the ceiling by firing on nothing: `institution` reached 0.70
# on 1 title in 500. A gate with no floor cannot tell "correctly selective" from "too cold to answer".
PREVALENCE_FLOOR = 0.01
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
cold = [q for q, v in rates.items() if v < PREVALENCE_FLOOR]
if len(cold) > len(rates) / 2:
    failures.append(
        f"{len(cold)} of {len(rates)} critique axes fire on under {PREVALENCE_FLOOR:.0%} of the pilot — the "
        f"wording is too cold to answer, not selective"
    )
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
    axes = sorted(rates)
    corpus_mean = [statistics.fmean([noul(r, q) for r in pilot.values()]) for q in axes]
    profile = lambda k: [noul(pilot[k], q) - m for q, m in zip(axes, corpus_mean)]

    def cosine(a, b):
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        return dot / (na * nb) if na and nb else 0.0

    wire = profile(WIRE)
    oz_cos, angel_cos = cosine(wire, profile(OZ)), cosine(wire, profile(ANGEL))
    report["critiqueProfileCosine"] = {"wire~oz": round(oz_cos, 3), "wire~angel": round(angel_cos, 3)}
    report["topAxes"] = {
        k: [q.replace("critique__", "") for _, q in
            sorted(((noul(pilot[k], q), q) for q in axes), reverse=True)[:3]]
        for k in (WIRE, OZ, ANGEL)
    }
    if oz_cos < MIN_PAIR_COSINE:
        failures.append(
            f"The Wire and Oz must agree at cosine >= {MIN_PAIR_COSINE} on the critique profile; got "
            f"{oz_cos:.2f}. This pair is the whole justification for the pass."
        )
    if angel_cos > MAX_ANGEL_COSINE:
        failures.append(f"Angel must stay below cosine {MAX_ANGEL_COSINE} to The Wire; got {angel_cos:.2f}")
    if oz_cos <= angel_cos:
        failures.append(f"Oz must be closer to The Wire than Angel is; got Oz {oz_cos:.2f}, Angel {angel_cos:.2f}")

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
