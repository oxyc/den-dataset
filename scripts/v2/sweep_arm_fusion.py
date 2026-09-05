#!/usr/bin/env python3
"""Ask whether two tag arms are worth combining, given that neither wins alone.

The paired test on DEV found v1 and v2 tags tied at 0.763 / 0.770 while disagreeing on 49
of 152 triplets — 92 both right, 11 both wrong. Between them they answer 141 of 152, so the
ceiling of any combination is 0.928 against 0.770 for the better arm alone. That gap is the
only reason to run this: two arms that agreed everywhere could not be fused into anything.

Fusion here is a weighted sum of cosines in the two tag spaces, `sim_a + w * sim_b`, which
is what a rail can actually compute — no retraining, no third embedding, just both blobs.
w=0 is arm A alone and large w approaches arm B alone, so the endpoints of the sweep
reproduce the single-arm numbers and act as a check that nothing is wired backwards.

The sweep is TUNING. It runs on DEV, it picks w on DEV, and the winning w is then a fixed
constant to be tested once on the sealed half — never swept there.
"""
import argparse
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from index_io import load_plot_index, load_premise_v1_index  # noqa: E402
from paired_triplets import mcnemar  # noqa: E402
from score_triplets import load_extra, restrict  # noqa: E402
from split import half  # noqa: E402

V2 = '/Users/cindy/Projects/Personal/den-dataset/out-t02/v2'


def sims(index, triplets):
    """(sim(a,pos), sim(a,neg)) per triplet, or None where a vector is missing."""
    out = {}
    for t in triplets:
        a, p, n = t['anchor'], t['positive'], t['negative']
        cid = f"{a}|{p}|{n}"
        if a not in index.pos or p not in index.pos or n not in index.pos:
            out[cid] = None
            continue
        av = index.vectors[index.pos[a]]
        out[cid] = (float(av @ index.vectors[index.pos[p]]),
                    float(av @ index.vectors[index.pos[n]]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--triplets', default=os.path.join(V2, 'ruler', 'triplets-final.json'))
    ap.add_argument('--half', choices=['dev', 'test', 'all'], default='dev')
    ap.add_argument('--arm', action='append', default=[])
    ap.add_argument('--a', required=True)
    ap.add_argument('--b', required=True)
    ap.add_argument('--weights', default='0,0.125,0.25,0.375,0.5,0.625,0.75,1,1.5,2,4,1000')
    ap.add_argument('--nested-folds', type=int, default=0,
                    help='honest estimate: repeatedly split these triplets, pick w on one half '
                         'and score it on the other, so the reported gain excludes the '
                         'advantage of having chosen w on the cases being scored')
    ap.add_argument('--seed', type=int, default=20260905)
    ap.add_argument('--out', default=None)
    args = ap.parse_args()

    if args.half == 'test':
        sys.exit('refusing to sweep on the sealed half — pick w on dev, then test one w once')

    with open(args.triplets, encoding='utf-8') as fh:
        triplets = [t for t in json.load(fh)['triplets']
                    if args.half == 'all' or half(t['anchor']) == args.half]

    arms = {'plot': load_plot_index(), 'premise-v1': load_premise_v1_index()}
    for spec in args.arm:
        idx = load_extra(spec)
        arms[idx.name] = idx
    for name in (args.a, args.b):
        if name not in arms:
            sys.exit(f'no arm named {name!r} — have {sorted(arms)}')

    pair = {n: arms[n] for n in (args.a, args.b)}
    shared = set(min(pair.values(), key=lambda i: len(i.keys)).keys)
    for idx in pair.values():
        shared &= set(idx.keys)
    pair = {n: restrict(i, [k for k in i.keys if k in shared]) for n, i in pair.items()}

    sa = sims(pair[args.a], triplets)
    sb = sims(pair[args.b], triplets)
    cids = [c for c in sa if sa[c] is not None and sb[c] is not None]

    # The ceiling: triplets at least one arm gets right. Fusion cannot exceed it, and how
    # far a weight falls short of it is the honest measure of how much the combination
    # recovers rather than how good the number looks on its own.
    either = sum(1 for c in cids
                 if sa[c][0] > sa[c][1] or sb[c][0] > sb[c][1])

    rows = []
    for w in [float(x) for x in args.weights.split(',')]:
        wins = 0
        per_case = {}
        for c in cids:
            # A very large w is the arm-B-only endpoint without a special case: the A term
            # becomes negligible rather than being switched off.
            sp = sa[c][0] + w * sb[c][0]
            sn = sa[c][1] + w * sb[c][1]
            per_case[c] = sp > sn
            wins += sp > sn
        rows.append({'w': w, 'accuracy': round(wins / len(cids), 4), 'cases': per_case})

    base = {c: sa[c][0] > sa[c][1] for c in cids}
    best = max(rows, key=lambda r: r['accuracy'])
    b_only = sum(1 for c in cids if base[c] and not best['cases'][c])
    c_only = sum(1 for c in cids if best['cases'][c] and not base[c])
    test = mcnemar(b_only, c_only)

    # The DEV argmax is optimistic by construction: w was chosen on the same triplets it is
    # scored on. Splitting repeatedly — pick w on one half, score it on the other — costs
    # nothing and answers how much of the gain survives honest selection, which is exactly
    # the question the sealed half will be asked and the one this can answer first.
    nested = None
    if args.nested_folds:
        rng = random.Random(args.seed)
        held, picks = [], []
        for _ in range(args.nested_folds):
            order = cids[:]
            rng.shuffle(order)
            mid = len(order) // 2
            for tune, hold in ((order[:mid], order[mid:]), (order[mid:], order[:mid])):
                best_w = max(rows, key=lambda r: sum(r['cases'][c] for c in tune))['w']
                row = next(r for r in rows if r['w'] == best_w)
                held.append(sum(row['cases'][c] for c in hold) / len(hold))
                picks.append(best_w)
        base_acc = sum(base[c] for c in cids) / len(cids)
        nested = {
            'folds': len(held),
            'meanHeldOutAccuracy': round(sum(held) / len(held), 4),
            'minHeldOutAccuracy': round(min(held), 4),
            'baselineAccuracy': round(base_acc, 4),
            'meanGainOverBaseline': round(sum(held) / len(held) - base_acc, 4),
            'weightsPicked': sorted(set(picks)),
        }

    report = {
        'half': args.half, 'pairedTriplets': len(cids),
        'a': args.a, 'b': args.b,
        'ceilingEitherArmRight': round(either / len(cids), 4),
        'sweep': [{'w': r['w'], 'accuracy': r['accuracy']} for r in rows],
        'best': {'w': best['w'], 'accuracy': best['accuracy']},
        'nestedSelection': nested,
        'bestVsA': {'table': {f'only {args.a}': b_only, 'only fused': c_only}, 'mcnemar': test},
        'verdict': ('fusion beats the baseline arm' if (test['p'] or 1.0) < 0.05 and c_only > b_only
                    else 'fusion does not significantly beat the baseline arm'),
    }
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as fh:
            json.dump(report, fh, indent=2)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
