#!/usr/bin/env python3
"""RecoEval's metrics, in Python, for fast iteration.

These mirror `den/Sources/DenKit/Reco/RecoEval.swift` line for line, including the parts
that look like quirks and are not:

- nDCG's ideal is capped at min(|relevant|, k), so a case with one relevant item scores 1.0
  when that item is placed first, instead of being unreachable.
- average precision divides by min(|relevant|, |recommended|) for the same reason.
- diversity is nil (None) below two vectors — "not measurable" is not "maximally repetitive".
- an item absent from the popularity map is skipped for novelty, so a ranker that returns
  unknown ids cannot look adventurous.
- cases with empty ground truth are excluded from accuracy but still count for
  diversity/novelty/coverage.

`compare` reproduces the DT-E AC2 gate, including its rule that a tie FAILS.

The Swift implementation stays authoritative; `score_v1_baseline.py --emit-swift-fixture`
writes the same cases out for a run through it.
"""
import math


def _top(k, items):
    return items[:k]


def _mean(values):
    return sum(values) / len(values) if values else 0.0


def ndcg(recommended, relevant, k):
    if not relevant:
        return 0.0
    gains = sum(1 / math.log2(i + 2) for i, item in enumerate(recommended) if item in relevant)
    ideal = sum(1 / math.log2(i + 2) for i in range(min(len(relevant), max(k, 1))))
    return gains / ideal if ideal else 0.0


def average_precision(recommended, relevant):
    if not relevant:
        return 0.0
    hits = 0
    total = 0.0
    for position, item in enumerate(recommended):
        if item in relevant:
            hits += 1
            total += hits / (position + 1)
    return total / min(len(relevant), max(len(recommended), 1))


def diversity(recommended, vectors_by_key):
    if vectors_by_key is None:
        return None
    vecs = [vectors_by_key[i] for i in recommended if i in vectors_by_key]
    if len(vecs) < 2:
        return None
    total = 0.0
    pairs = 0
    for i in range(len(vecs)):
        for j in range(i + 1, len(vecs)):
            total += float(vecs[i] @ vecs[j])
            pairs += 1
    return None if pairs == 0 else 1 - total / pairs


def novelty(recommended, popularity):
    if not popularity:
        return None
    total = sum(popularity.values())
    if total <= 0:
        return None
    scores = []
    for item in recommended:
        share = popularity.get(item)
        if share is None or share <= 0:
            continue
        scores.append(-math.log2(share / total))
    return _mean(scores) if scores else None


def evaluate(cases, k=10, vectors_by_key=None, popularity=None, catalog_size=0):
    """`cases` = [{'seed', 'recommended', 'relevant'}]."""
    popularity = popularity or {}
    scored = [c for c in cases if c['relevant']]
    ndcgs = [ndcg(_top(k, c['recommended']), set(c['relevant']), k) for c in scored]
    maps = [average_precision(_top(k, c['recommended']), set(c['relevant'])) for c in scored]

    divs = [d for d in (diversity(_top(k, c['recommended']), vectors_by_key) for c in cases) if d is not None]
    novs = [n for n in (novelty(_top(k, c['recommended']), popularity) for c in cases) if n is not None]

    distinct = set()
    for c in cases:
        distinct.update(_top(k, c['recommended']))
    coverage = len(distinct) / catalog_size if catalog_size > 0 else None

    return {
        'ndcg': _mean(ndcgs),
        'map': _mean(maps),
        'diversity': _mean(divs) if divs else None,
        'novelty': _mean(novs) if novs else None,
        'coverage': coverage,
        'scoredCases': len(scored),
        'totalCases': len(cases),
    }


def compare(candidate, baseline, tolerance=0.02):
    """The DT-E AC2 gate. A tie fails: replacing a working system carries its own risk."""
    reasons = []

    def check(name, new, old):
        if new is None or old is None:
            return
        floor = old * (1 - tolerance) if old > 0 else -math.inf
        if new < floor:
            reasons.append(f'{name} regressed {old:.3f} -> {new:.3f}')

    check('ndcg', candidate['ndcg'], baseline['ndcg'])
    check('map', candidate['map'], baseline['map'])
    check('diversity', candidate['diversity'], baseline['diversity'])
    check('novelty', candidate['novelty'], baseline['novelty'])
    check('coverage', candidate['coverage'], baseline['coverage'])
    if not reasons and candidate['ndcg'] <= baseline['ndcg']:
        reasons.append(f"no accuracy gain (ndcg {candidate['ndcg']:.3f} vs {baseline['ndcg']:.3f})")
    return {'passes': not reasons, 'reasons': reasons}
