#!/usr/bin/env python3
"""Check the Python metrics against RecoEval's documented behaviour.

These are not "does it run" tests. Each one pins a decision RecoEval's source calls out as
deliberate, because those are exactly the places a reimplementation drifts without failing:
the capped ideal, the min() in average precision, nil-not-zero diversity, skipping unknown
ids for novelty, and the gate's rule that a tie fails.

Run: out-t02/v2/.venv/bin/python scripts/v2/test_reco_metrics.py
"""
import math
import sys

from reco_metrics import average_precision, compare, diversity, evaluate, ndcg, novelty

FAILURES = []


def check(name, got, want, tol=1e-9):
    ok = (got is None and want is None) or (
        got is not None and want is not None and abs(got - want) <= tol)
    if not ok:
        FAILURES.append(f'{name}: got {got!r}, want {want!r}')


# nDCG's ideal is capped at min(|relevant|, k): one relevant item placed first IS perfect,
# rather than being unreachable in a row of ten.
check('ndcg one relevant placed first', ndcg(['a', 'b', 'c'], {'a'}, 10), 1.0)
check('ndcg one relevant placed second', ndcg(['b', 'a', 'c'], {'a'}, 10), (1 / math.log2(3)) / 1.0)
check('ndcg no hits', ndcg(['x', 'y'], {'a'}, 10), 0.0)
# Empty ground truth scores 0, and evaluate() must exclude such cases rather than average
# the zero in — the difference between the two is the whole point of `scoredCases`.
check('ndcg empty relevant', ndcg(['a'], set(), 10), 0.0)

# Average precision divides by min(|relevant|, |recommended|) for the same reason.
check('ap both hits at top', average_precision(['a', 'b'], {'a', 'b'}), 1.0)
check('ap hit at position 2', average_precision(['x', 'a'], {'a'}), 0.5)

# Diversity is None below two vectors: "not measurable" must not read as "maximally
# repetitive", which a 0.0 would.
check('diversity with no index', diversity(['a', 'b'], None), None)
check('diversity with one vector', diversity(['a'], {'a': [1.0, 0.0]}), None)


class V(list):
    def __matmul__(self, other):
        return sum(x * y for x, y in zip(self, other))


check('diversity orthogonal pair', diversity(['a', 'b'], {'a': V([1.0, 0.0]), 'b': V([0.0, 1.0])}), 1.0)
check('diversity identical pair', diversity(['a', 'b'], {'a': V([1.0, 0.0]), 'b': V([1.0, 0.0])}), 0.0)

# An item with no popularity entry is skipped, so a ranker returning unknown ids cannot look
# adventurous by inventing them.
pop = {'a': 50.0, 'b': 50.0}
check('novelty ignores unknown ids', novelty(['a', 'zzz'], pop), -math.log2(0.5))
check('novelty with no popularity map', novelty(['a'], {}), None)

# Cases with no ground truth still count for coverage but not for accuracy.
cases = [
    {'seed': 's1', 'recommended': ['a', 'b'], 'relevant': ['a']},
    {'seed': 's2', 'recommended': ['c', 'd'], 'relevant': []},
]
m = evaluate(cases, k=10, catalog_size=8)
check('evaluate scoredCases', float(m['scoredCases']), 1.0)
check('evaluate totalCases', float(m['totalCases']), 2.0)
check('evaluate ndcg over scored only', m['ndcg'], 1.0)
check('evaluate coverage counts every case', m['coverage'], 4 / 8)

# The gate: a tie fails, because replacing a working system carries its own risk.
base = {'ndcg': 0.10, 'map': 0.05, 'diversity': 0.4, 'novelty': 10.0, 'coverage': 0.5}
tie = dict(base)
verdict = compare(tie, base)
if verdict['passes']:
    FAILURES.append('gate: a tie passed, but a tie must fail')

better = dict(base, ndcg=0.12)
if not compare(better, base)['passes']:
    FAILURES.append(f"gate: a clean accuracy gain failed — {compare(better, base)['reasons']}")

# A big accuracy win must not pay for a diversity collapse.
traded = dict(base, ndcg=0.30, diversity=0.2)
v = compare(traded, base)
if v['passes']:
    FAILURES.append('gate: an accuracy win paid for a diversity collapse')

# 2% relative slack: offline metrics on a finite sample are not precise to 0.1%.
within = dict(base, ndcg=0.12, diversity=0.4 * 0.99)
if not compare(within, base)['passes']:
    FAILURES.append('gate: a 1% diversity dip inside tolerance was treated as a regression')

if FAILURES:
    print(f'{len(FAILURES)} FAILED:')
    for f in FAILURES:
        print('  ' + f)
    sys.exit(1)
print('all metric conformance checks pass')
