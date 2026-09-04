#!/usr/bin/env python3
"""Score index arms against the MovieLens co-rating ruler. Phase 1 step 4: the baseline.

Arms:
  plot        — the shipped whole-plot index
  premise-v1  — the shipped DT-H premise index
  fused       — premise-first with a plot-agreement bonus, the shape DetailModel ships
  popularity  — a control: return the most-liked titles, ignoring the seed entirely

The control matters more than it looks. If `popularity` scores near the real arms, the
ruler is measuring fame rather than taste agreement and no comparison built on it means
anything. It is reported every run for exactly that reason.

Retrieval is restricted to the pool the ruler can actually judge (shipped movies that join
MovieLens). Scoring an unrestricted retrieval would blend "ranks badly" with "returned a
title the ruler has no opinion about".
"""
import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from index_io import load_plot_index, load_premise_v1_index  # noqa: E402
from reco_metrics import evaluate, compare  # noqa: E402
from split import half  # noqa: E402

V2 = '/Users/cindy/Projects/Personal/den-dataset/out-t02/v2'


def arm_recommendations(index, seeds, k, mask):
    return index.topk_many(seeds, k, restrict_mask=mask)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cases', default=os.path.join(V2, 'eval', 'reco-cases.json'))
    ap.add_argument('--half', choices=['dev', 'test', 'all'], default='dev')
    ap.add_argument('--k', type=int, default=10)
    ap.add_argument('--out', default=None)
    args = ap.parse_args()

    if args.half == 'test':
        print('!! scoring the SEALED TEST half — this is a gate run, not a tuning run', file=sys.stderr)

    with open(args.cases, encoding='utf-8') as fh:
        blob = json.load(fh)
    cases = [c for c in blob['cases'] if args.half == 'all' or half(c['seed']) == args.half]
    with open(os.path.join(V2, 'eval', 'reco-popularity.json'), encoding='utf-8') as fh:
        popularity = {k: float(v) for k, v in json.load(fh).items()}

    plot = load_plot_index()
    prem = load_premise_v1_index()

    judgeable = set(popularity)
    seeds = [c['seed'] for c in cases]

    results = {}
    per_arm_recs = {}

    for name, index in (('plot', plot), ('premise-v1', prem)):
        mask = np.array([k in judgeable for k in index.keys])
        present = [s for s in seeds if s in index.pos]
        recs = arm_recommendations(index, present, args.k, mask)
        rows = [{'seed': c['seed'], 'recommended': [k for k, _ in recs.get(c['seed'], [])],
                 'relevant': c['relevant']} for c in cases]
        per_arm_recs[name] = rows
        vecs = {k: index.vectors[i] for i, k in enumerate(index.keys)}
        results[name] = evaluate(rows, k=args.k, vectors_by_key=vecs,
                                 popularity=popularity, catalog_size=len(judgeable))
        results[name]['seedsInIndex'] = len(present)

    # Fused: premise-first, plot agreement as a bonus — the ordering DetailModel ships.
    fused_rows = []
    pmask = np.array([k in judgeable for k in prem.keys])
    wide = prem.topk_many([s for s in seeds if s in prem.pos], 40, restrict_mask=pmask)
    for c in cases:
        cands = wide.get(c['seed'], [])
        seed_plot = plot.pos.get(c['seed'])
        scored = []
        for key, ps in cands:
            bonus = 0.0
            if seed_plot is not None and key in plot.pos:
                bonus = float(plot.vectors[plot.pos[key]] @ plot.vectors[seed_plot])
            scored.append((key, ps + 0.25 * bonus))
        scored.sort(key=lambda kv: -kv[1])
        fused_rows.append({'seed': c['seed'], 'recommended': [k for k, _ in scored[:args.k]],
                           'relevant': c['relevant']})
    per_arm_recs['fused'] = fused_rows
    pvecs = {k: prem.vectors[i] for i, k in enumerate(prem.keys)}
    results['fused'] = evaluate(fused_rows, k=args.k, vectors_by_key=pvecs,
                                popularity=popularity, catalog_size=len(judgeable))

    # Control.
    top_pop = [k for k, _ in sorted(popularity.items(), key=lambda kv: -kv[1])[:args.k + 1]]
    pop_rows = [{'seed': c['seed'],
                 'recommended': [k for k in top_pop if k != c['seed']][:args.k],
                 'relevant': c['relevant']} for c in cases]
    results['popularity-control'] = evaluate(pop_rows, k=args.k, vectors_by_key=pvecs,
                                             popularity=popularity, catalog_size=len(judgeable))

    report = {
        'half': args.half, 'k': args.k, 'cases': len(cases),
        'ruler': blob['meta']['method'], 'canonShare': blob['meta'].get('canonShare'),
        'arms': results,
        'gates': {
            'premise-v1 vs plot': compare(results['premise-v1'], results['plot']),
            'fused vs plot': compare(results['fused'], results['plot']),
        },
    }
    out = args.out or os.path.join(V2, 'eval', f'reco-baseline-{args.half}.json')
    with open(out, 'w', encoding='utf-8') as fh:
        json.dump(report, fh, indent=2)
    with open(out.replace('.json', '-recs.json'), 'w', encoding='utf-8') as fh:
        json.dump(per_arm_recs, fh)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
