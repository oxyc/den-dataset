#!/usr/bin/env python3
"""Sweep the premise/plot fusion weight against BOTH rulers at once.

The two rulers disagree decisively and in opposite directions: the plot index wins co-rating
agreement ~3x, the premise index wins premise discrimination 0.740 to 0.647. Neither is
wrong; they measure different questions. So the interesting quantity is not "which index"
but **where on the line between them a fused rail should sit**, and whether there is a
weighting that gives up little on either.

Score is `premise + w * plot`, both as cosine in their own space, over a premise-first
candidate pool. w=0 is premise-only; large w approaches plot-only.

This deliberately reports both metrics for every w rather than a blended score. A single
blended number is exactly the trade DT-G's gate exists to prevent — it lets a big win on one
axis pay for a collapse on the other, and the whole point here is to see the shape of that
trade rather than to hide it behind a weighted sum.

Caveats it does not hide: this is the isolated fusion, not the shipped rail — `RelatedRows`
keeps TMDB behavioural recommendations as the spine and applies tonal/animated gating, and
those are the most co-rating-aligned inputs the real rail has. And the premise numbers come
from provisional (generator-proposed) triplets until the blind judging completes.
"""
import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from index_io import load_plot_index, load_premise_v1_index  # noqa: E402
from reco_metrics import evaluate  # noqa: E402
from split import half  # noqa: E402

V2 = '/Users/cindy/Projects/Personal/den-dataset/out-t02/v2'


def provisional_triplets():
    trips = []
    for i in range(150):
        path = os.path.join(V2, 'ruler', 'gen', 'out', f'batch-{i:04d}.json')
        if not os.path.exists(path):
            continue
        with open(path, encoding='utf-8') as fh:
            for r in json.load(fh):
                if r.get('positive') and r.get('negative'):
                    trips.append((r['key'], r['positive']['key'], r['negative']['key']))
    return trips


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--weights', default='0,0.25,0.5,1,2,4,8')
    ap.add_argument('--pool', type=int, default=60, help='premise neighbours reranked per seed')
    ap.add_argument('--k', type=int, default=10)
    ap.add_argument('--out', default=os.path.join(V2, 'eval', 'fusion-sweep.json'))
    args = ap.parse_args()

    weights = [float(w) for w in args.weights.split(',')]
    plot = load_plot_index()
    prem = load_premise_v1_index()

    with open(os.path.join(V2, 'eval', 'reco-cases.json'), encoding='utf-8') as fh:
        cases = [c for c in json.load(fh)['cases'] if half(c['seed']) == 'dev']
    with open(os.path.join(V2, 'eval', 'reco-popularity.json'), encoding='utf-8') as fh:
        popularity = {k: float(v) for k, v in json.load(fh).items()}
    judgeable = set(popularity)

    mask = np.array([k in judgeable for k in prem.keys])
    seeds = [c['seed'] for c in cases if c['seed'] in prem.pos]
    wide = prem.topk_many(seeds, args.pool, restrict_mask=mask)

    trips = provisional_triplets()
    pvecs = {k: prem.vectors[i] for i, k in enumerate(prem.keys)}

    rows = []
    for w in weights:
        # --- co-rating ---
        recs = []
        for c in cases:
            scored = []
            seed_plot = plot.pos.get(c['seed'])
            for key, ps in wide.get(c['seed'], []):
                bonus = 0.0
                if seed_plot is not None and key in plot.pos:
                    bonus = float(plot.vectors[plot.pos[key]] @ plot.vectors[seed_plot])
                scored.append((key, ps + w * bonus))
            scored.sort(key=lambda kv: -kv[1])
            recs.append({'seed': c['seed'], 'recommended': [k for k, _ in scored[:args.k]],
                         'relevant': c['relevant']})
        m = evaluate(recs, k=args.k, vectors_by_key=pvecs, popularity=popularity,
                     catalog_size=len(judgeable))

        # --- premise discrimination, same fused score on the triplet's two candidates ---
        wins = scored_n = 0
        for a, p, n in trips:
            if a not in prem.pos or p not in prem.pos or n not in prem.pos:
                continue
            av = prem.vectors[prem.pos[a]]
            sp = float(av @ prem.vectors[prem.pos[p]])
            sn = float(av @ prem.vectors[prem.pos[n]])
            if a in plot.pos:
                pv = plot.vectors[plot.pos[a]]
                if p in plot.pos:
                    sp += w * float(pv @ plot.vectors[plot.pos[p]])
                if n in plot.pos:
                    sn += w * float(pv @ plot.vectors[plot.pos[n]])
            scored_n += 1
            wins += sp > sn
        rows.append({'w': w, 'ndcg': round(m['ndcg'], 4), 'map': round(m['map'], 4),
                     'diversity': round(m['diversity'], 3) if m['diversity'] else None,
                     'coverage': round(m['coverage'], 4) if m['coverage'] else None,
                     'tripletAccuracy': round(wins / scored_n, 4) if scored_n else None,
                     'tripletsScored': scored_n})
        print(f"w={w:<5} ndcg {rows[-1]['ndcg']:.4f}  triplet {rows[-1]['tripletAccuracy']:.3f}"
              f"  div {rows[-1]['diversity']}  cov {rows[-1]['coverage']}", flush=True)

    with open(args.out, 'w', encoding='utf-8') as fh:
        json.dump({'pool': args.pool, 'k': args.k,
                   'note': 'isolated premise+w*plot fusion; NOT the shipped rail, which keeps '
                           'TMDB behavioural recs as the spine and applies tonal gating. '
                           'Triplets are provisional (generator-proposed, pre-blind-judging).',
                   'rows': rows}, fh, indent=2)


if __name__ == '__main__':
    main()
