#!/usr/bin/env python3
"""Sweep the SHIPPED More Like This rail's constants, in the units the code actually uses.

`sweep_fusion.py` swept `premise + w * plot_cosine`. That is not what ships. The real scorer
(`DetailModel.premiseNeighbours`) is:

    candidates = premise.nearestNeighbors(k=40)
    drop where labels.animated != anchor.animated          # HARD tonal gate
    s = n.score
    if id in plot.nearestNeighbors(k=20):  s += n.score / 4   # plot-agreement bonus, MEMBERSHIP
    if labels.primaryGenre != anchor.primaryGenre: s -= s / 4  # cross-genre down-weight
    take top 20

Two differences that matter. The plot bonus is a **flat multiplier on membership of the plot
index's top-20**, not a graded function of plot similarity — so "set w=0.5" names no constant
in the code. And the animated gate plus the genre down-weight both reshape the candidate set
before any of that.

So this replicates the real thing and sweeps its actual knobs: the bonus fraction (`n.score/4`
→ `/8`, `/2`, `/1`), the plot-agreement pool size, and the premise pool size. What comes out is
a diff, not an abstraction.

Scored on both rulers, per media type, because the plot bonus was measured to help movies and
hurt series.
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
ROOT = '/Users/cindy/Projects/Personal/den-dataset/out-t02'


def load_labels():
    with open(os.path.join(ROOT, 'labels-t02.json'), encoding='utf-8') as fh:
        out = {}
        for r in json.load(fh)['records']:
            out[f"{r.get('mediaType', 'movie')}:{r['tmdbId']}"] = (
                bool(r.get('animated')), r.get('primaryGenre'))
    return out


def rail(anchor, premise, plot, labels, bonus_div, premise_k, plot_k, judgeable=None):
    """One anchor's ranked neighbours, replicating DetailModel.premiseNeighbours."""
    if anchor not in premise.pos:
        return []
    mine = labels.get(anchor)
    if mine is None:
        return []
    media = anchor.split(':')[0]

    plot_set = set()
    if anchor in plot.pos:
        for k, _ in plot.topk(anchor, plot_k, restrict_mask=plot.media_mask(media)):
            plot_set.add(k)

    out = []
    for key, score in premise.topk(anchor, premise_k, restrict_mask=premise.media_mask(media)):
        lab = labels.get(key)
        if lab is None or lab[0] != mine[0]:      # hard animated gate
            continue
        s = score
        if bonus_div and key in plot_set:
            s += score / bonus_div                 # plot-agreement bonus (membership)
        if lab[1] != mine[1]:
            s -= s / 4                             # cross-genre down-weight
        if judgeable is not None and key not in judgeable:
            continue
        out.append((key, s))
    out.sort(key=lambda kv: -kv[1])
    return [k for k, _ in out[:20]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--bonus-divisors', default='0,8,4,2,1',
                    help='0 means no plot bonus; 4 is what ships')
    ap.add_argument('--premise-k', type=int, default=40)
    ap.add_argument('--plot-k', type=int, default=20)
    ap.add_argument('--out', default=os.path.join(V2, 'eval', 'rail-sweep.json'))
    args = ap.parse_args()

    plot = load_plot_index()
    prem = load_premise_v1_index()
    labels = load_labels()

    # media_mask is cheap to attach here rather than complicating index_io.
    for idx in (plot, prem):
        cache = {}

        def mask(media, _idx=idx, _cache=cache):
            if media not in _cache:
                _cache[media] = np.array([k.startswith(media + ':') for k in _idx.keys])
            return _cache[media]
        idx.media_mask = mask

    with open(os.path.join(V2, 'eval', 'reco-cases.json'), encoding='utf-8') as fh:
        cases = [c for c in json.load(fh)['cases'] if half(c['seed']) == 'dev']
    with open(os.path.join(V2, 'eval', 'reco-popularity.json'), encoding='utf-8') as fh:
        popularity = {k: float(v) for k, v in json.load(fh).items()}
    judgeable = set(popularity)

    trips = []
    for i in range(150):
        p = os.path.join(V2, 'ruler', 'gen', 'out', f'batch-{i:04d}.json')
        if not os.path.exists(p):
            continue
        with open(p, encoding='utf-8') as fh:
            for r in json.load(fh):
                if r.get('positive') and r.get('negative'):
                    trips.append((r['key'], r['positive']['key'], r['negative']['key']))

    rows = []
    for div in [int(x) for x in args.bonus_divisors.split(',')]:
        recs = [{'seed': c['seed'],
                 'recommended': rail(c['seed'], prem, plot, labels, div,
                                     args.premise_k, args.plot_k, judgeable)[:10],
                 'relevant': c['relevant']} for c in cases]
        m = evaluate(recs, k=10, popularity=popularity, catalog_size=len(judgeable))

        # Triplet side: apply the same bonus to the two candidates directly.
        acc = {}
        for label in ('all', 'movie', 'tv'):
            wins = n = 0
            for a, p, q in trips:
                if label != 'all' and not a.startswith(label + ':'):
                    continue
                if a not in prem.pos or p not in prem.pos or q not in prem.pos:
                    continue
                av = prem.vectors[prem.pos[a]]
                sp = float(av @ prem.vectors[prem.pos[p]])
                sq = float(av @ prem.vectors[prem.pos[q]])
                if div and a in plot.pos:
                    media = a.split(':')[0]
                    ps = {k for k, _ in plot.topk(a, args.plot_k, restrict_mask=plot.media_mask(media))}
                    if p in ps:
                        sp += sp / div
                    if q in ps:
                        sq += sq / div
                n += 1
                wins += sp > sq
            acc[label] = round(wins / n, 4) if n else None
            acc[label + 'N'] = n
        rows.append({'bonusDivisor': div, 'ndcg': round(m['ndcg'], 4),
                     'map': round(m['map'], 4), 'triplet': acc})
        shipped = ' (SHIPPED)' if div == 4 else ''
        print(f"n.score/{div or '-':<2} ndcg {m['ndcg']:.4f}  triplet all {acc['all']}  "
              f"movie {acc['movie']}  tv {acc['tv']}{shipped}", flush=True)

    with open(args.out, 'w', encoding='utf-8') as fh:
        json.dump({'premiseK': args.premise_k, 'plotK': args.plot_k,
                   'note': 'replicates DetailModel.premiseNeighbours including the hard animated '
                           'gate, the membership-based plot bonus and the cross-genre down-weight. '
                           'Same-media only, as the shipped ANN is. Triplets are provisional.',
                   'rows': rows}, fh, indent=2)


if __name__ == '__main__':
    main()
