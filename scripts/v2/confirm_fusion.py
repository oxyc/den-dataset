#!/usr/bin/env python3
"""The gate run: score ONE pre-registered fusion weight on the sealed half, once.

Separate from `sweep_arm_fusion.py` on purpose. That script refuses `--half test` because a
sweep on the sealed data would spend the seal to answer a question DEV already answers. This
one takes a single `--w` and no grid, so there is nothing here to tune with: the only decision
it can express was made before it ran.

`--expect-w` is the pre-registration, and it is required. It must equal `--w`, so a run whose
weight drifted from what was committed fails instead of quietly reporting a different
experiment. The committed value for the v1 + Haiku-v2 fusion is **0.5** (den-dataset 6f2d9cf):
a round value inside the DEV plateau rather than the DEV argmax of 0.375, which is the most
overfit point on the curve.

Reports all three arms — A alone, B alone, and the fusion — because the brief asks for v1
against v2 on the sealed half and that is one read, not three.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from index_io import load_plot_index, load_premise_v1_index  # noqa: E402
from paired_triplets import mcnemar  # noqa: E402
from score_triplets import load_extra, restrict  # noqa: E402
from split import half  # noqa: E402
from sweep_arm_fusion import sims  # noqa: E402

V2 = '/Users/cindy/Projects/Personal/den-dataset/out-t02/v2'


def table(base, other, cids):
    b = sum(1 for c in cids if base[c] and not other[c])
    c = sum(1 for c in cids if other[c] and not base[c])
    return b, c, mcnemar(b, c)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--triplets', default=os.path.join(V2, 'ruler', 'triplets-final.json'))
    ap.add_argument('--half', choices=['dev', 'test'], default='test')
    ap.add_argument('--arm', action='append', default=[])
    ap.add_argument('--a', required=True, help='baseline arm (v1)')
    ap.add_argument('--b', required=True, help='challenger arm (v2)')
    ap.add_argument('--w', type=float, required=True)
    ap.add_argument('--expect-w', type=float, required=True,
                    help='the pre-registered weight; must equal --w')
    ap.add_argument('--out', default=None)
    args = ap.parse_args()

    if args.w != args.expect_w:
        sys.exit(f'--w {args.w} does not match the pre-registered --expect-w {args.expect_w}; '
                 'refusing to report a different experiment than the one committed')
    if args.half == 'test':
        print('!! SEALED TEST HALF — this is the gate run. It is spent after this.',
              file=sys.stderr)

    with open(args.triplets, encoding='utf-8') as fh:
        blob = json.load(fh)
    triplets = [t for t in blob['triplets'] if half(t['anchor']) == args.half]

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
    if not cids:
        sys.exit('no triplet has vectors in both arms — nothing to gate')

    a_win = {c: sa[c][0] > sa[c][1] for c in cids}
    b_win = {c: sb[c][0] > sb[c][1] for c in cids}
    f_win = {c: sa[c][0] + args.w * sb[c][0] > sa[c][1] + args.w * sb[c][1] for c in cids}

    acc = {n: round(sum(w[c] for c in cids) / len(cids), 4)
           for n, w in ((args.a, a_win), (args.b, b_win), (f'fused w={args.w}', f_win))}

    vb, cb, t_b = table(a_win, b_win, cids)
    vf, cf, t_f = table(a_win, f_win, cids)

    def verdict(only_a, only_x, test, name):
        if (test['p'] or 1.0) >= 0.05:
            return (f'{name} does NOT beat {args.a} (p={test["p"]}) — under a tie-fails gate, '
                    'not adopted')
        return (f'{name} beats {args.a} (p={test["p"]})' if only_x > only_a
                else f'{args.a} beats {name} (p={test["p"]})')

    report = {
        'half': args.half, 'gateRun': args.half == 'test',
        'weight': args.w, 'preRegistered': args.expect_w,
        'pairedTriplets': len(cids), 'pool': len(next(iter(pair.values())).keys),
        'accuracy': acc,
        'v2VsV1': {'table': {f'only {args.a}': vb, f'only {args.b}': cb}, 'mcnemar': t_b,
                   'verdict': verdict(vb, cb, t_b, args.b)},
        'fusedVsV1': {'table': {f'only {args.a}': vf, 'only fused': cf}, 'mcnemar': t_f,
                      'verdict': verdict(vf, cf, t_f, f'fusion at w={args.w}')},
    }
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as fh:
            json.dump(report, fh, indent=2)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
