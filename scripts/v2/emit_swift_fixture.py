#!/usr/bin/env python3
"""Emit the cross-check fixture that `RecoEvalCrossCheckTests` reads.

The Python metrics exist for speed; DenKit's `RecoEval` is authoritative. This writes the
cases plus the numbers Python computed for them, and the Swift test asserts it reproduces
them. Divergence means the published v2 figures are wrong.

Run after `score_reco.py`, then:
  DEN_V2_EVAL_FIXTURE=<path> swift test --filter RecoEvalCrossCheck
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reco_metrics import evaluate  # noqa: E402

V2 = '/Users/cindy/Projects/Personal/den-dataset/out-t02/v2'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--recs', default=os.path.join(V2, 'eval', 'reco-baseline-dev-recs.json'))
    ap.add_argument('--k', type=int, default=10)
    ap.add_argument('--limit', type=int, default=400,
                    help='cases per arm; the fixture is read whole by a unit test')
    ap.add_argument('--out', default=os.path.join(V2, 'eval', 'swift-crosscheck.json'))
    args = ap.parse_args()

    with open(args.recs, encoding='utf-8') as fh:
        per_arm = json.load(fh)
    with open(os.path.join(V2, 'eval', 'reco-popularity.json'), encoding='utf-8') as fh:
        popularity_all = {k: float(v) for k, v in json.load(fh).items()}

    arms = {}
    used_keys = set()
    for name, rows in per_arm.items():
        subset = rows[:args.limit]
        for r in subset:
            used_keys.add(r['seed'])
            used_keys.update(r['recommended'])
            used_keys.update(r['relevant'])
        arms[name] = {'cases': subset}

    # Keep the fixture small: only the popularity entries these cases touch. Novelty is a share
    # of the TOTAL, so trimming the map changes the number — both sides must see the same map,
    # which is why it is written into the fixture rather than recomputed on either side.
    popularity = {k: v for k, v in popularity_all.items() if k in used_keys}
    catalog_size = len(popularity_all)

    for name, payload in arms.items():
        m = evaluate(payload['cases'], k=args.k, vectors_by_key=None,
                     popularity=popularity, catalog_size=catalog_size)
        payload['expected'] = {'ndcg': m['ndcg'], 'map': m['map'], 'novelty': m['novelty'],
                               'coverage': m['coverage'], 'scoredCases': m['scoredCases'],
                               'totalCases': m['totalCases']}

    with open(args.out, 'w', encoding='utf-8') as fh:
        json.dump({'k': args.k, 'catalogSize': catalog_size,
                   'popularity': popularity, 'arms': arms}, fh)
    print(json.dumps({'arms': {n: p['expected'] for n, p in arms.items()},
                      'popularityEntries': len(popularity),
                      'catalogSize': catalog_size, 'fixture': args.out}, indent=2))


if __name__ == '__main__':
    main()
