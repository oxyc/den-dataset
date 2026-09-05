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
from index_io import load_plot_index, load_premise_v1_index, Index, read_blob  # noqa: E402
from reco_metrics import evaluate, compare  # noqa: E402
from split import half  # noqa: E402

V2 = '/Users/cindy/Projects/Personal/den-dataset/out-t02/v2'


def load_extra(spec):
    """--arm name=vectors.bin:keys.json — score a v2 or cutoff-variant blob alongside the
    shipped arms without touching them."""
    name, paths = spec.split('=', 1)
    vec_path, keys_path = paths.split(':', 1)
    with open(keys_path, encoding='utf-8') as fh:
        keys = json.load(fh)
    if isinstance(keys, dict):
        keys = keys.get('keys') or keys.get('ids')
    vectors, count, _ = read_blob(vec_path)
    if count != len(keys):
        raise SystemExit(f'{name}: blob has {count} rows, key file has {len(keys)}')
    return Index(keys, vectors, name)


def arm_recommendations(index, seeds, k, mask):
    return index.topk_many(seeds, k, restrict_mask=mask)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cases', default=os.path.join(V2, 'eval', 'reco-cases.json'))
    ap.add_argument('--half', choices=['dev', 'test', 'all'], default='dev')
    ap.add_argument('--k', type=int, default=10)
    ap.add_argument('--arm', action='append', default=[],
                    help='name=vectors.bin:keys.json — adds an arm beside plot and premise-v1')
    # A partial index CANNOT be compared to a full one on this ruler without it.
    #
    # Every metric here is retrieval over a catalogue. A 457-row arm draws its ten
    # recommendations from 457 candidates while premise-v1 draws from 37,314, so it scores
    # nDCG 0.001 against 0.030 and coverage 0.010 against 0.506 — not because its vectors are
    # worse but because the relevant titles are almost never in its index at all. Run without
    # this flag, a bake-off arm looks catastrophically bad and the number means nothing.
    #
    # Restricting every arm to one shared key set makes the haystack identical, which is the
    # only form in which the brief's "does not regress co-rating" is answerable before a
    # full-corpus v2 exists.
    ap.add_argument('--restrict-to', default=None,
                    help='intersect every arm to one shared key set before retrieving; '
                         '"smallest" uses the smallest arm\'s own keys')
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

    extra = [load_extra(spec) for spec in args.arm]
    all_arms = [('plot', plot), ('premise-v1', prem)] + [(i.name, i) for i in extra]

    if args.restrict_to:
        from score_triplets import restrict  # noqa: E402  (same helper, same reason)
        if args.restrict_to == 'smallest':
            pool = min((i for _, i in all_arms), key=lambda i: len(i.keys)).keys
        else:
            with open(args.restrict_to, encoding='utf-8') as fh:
                pool = json.load(fh)
            if isinstance(pool, dict):
                pool = pool.get('keys') or pool.get('ids')
        shared = set(pool)
        for _, i in all_arms:
            shared &= set(i.keys)
        all_arms = [(n, restrict(i, [k for k in pool if k in shared])) for n, i in all_arms]
        plot = dict(all_arms)['plot']
        prem = dict(all_arms)['premise-v1']
        # The catalogue shrinks with the arms, or coverage and novelty are computed against a
        # universe no arm can reach.
        judgeable &= shared
        cases = [c for c in cases if c['seed'] in shared]
        for c in cases:
            c['relevant'] = [r for r in c['relevant'] if r in shared]
        cases = [c for c in cases if c['relevant']]
        seeds = [c['seed'] for c in cases]
        print(f'restricted to {len(shared)} shared keys; {len(cases)} cases still have a '
              f'reachable relevant title', file=sys.stderr)
        if not cases:
            sys.exit('no co-rating case survives the restriction — this ruler cannot judge '
                     'a pool this small, which is itself the answer')

    for name, index in all_arms:
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
            **{f'{i.name} vs premise-v1': compare(results[i.name], results['premise-v1'])
               for i in extra},
            **{f'{i.name} vs plot': compare(results[i.name], results['plot']) for i in extra},
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
