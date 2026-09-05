#!/usr/bin/env python3
"""Decide whether one arm actually beats another on the triplet ruler, or just looks like it.

`score_triplets.py` reports each arm's accuracy independently. That is not enough to adopt
an index: at 152 scored triplets one flipped case moves accuracy by 0.66 points, so two arms
can differ by a percentage point and be the same ranker. The brief's gate — adopt only if v2
beats v1, a tie FAILS — needs a paired test, not two separate rates.

McNemar's test is the right one because the arms are scored on the *same* triplets. The
concordant cases (both right, both wrong) carry no information about which arm is better and
are discarded; only the discordant pairs count. With the continuity correction,

    chi2 = (|b - c| - 1)^2 / (b + c)

where b = triplets only arm A got right and c = triplets only arm B got right. chi2 > 3.841
is p < 0.05 on 1 df. Below ~10 discordant pairs the normal approximation is unreliable, so an
exact binomial is reported alongside and is the one to quote there.

Arms are intersected to a shared pool and a shared scored set before anything is compared, for
the same reason `score_triplets --restrict-to` exists: an arm that skipped the hard triplets
would otherwise show a better rate for having answered fewer questions.
"""
import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from index_io import load_plot_index, load_premise_v1_index  # noqa: E402
from score_triplets import load_extra, restrict  # noqa: E402
from split import half  # noqa: E402

V2 = '/Users/cindy/Projects/Personal/den-dataset/out-t02/v2'


def outcomes(index, triplets):
    """Per-triplet win/loss, keyed by anchor|positive|negative. None where the arm has no
    vector for one of the three — those triplets are dropped from the pairing, not scored 0."""
    out = {}
    for t in triplets:
        a, p, n = t['anchor'], t['positive'], t['negative']
        cid = f"{a}|{p}|{n}"
        if a not in index.pos or p not in index.pos or n not in index.pos:
            out[cid] = None
            continue
        av = index.vectors[index.pos[a]]
        sp = float(av @ index.vectors[index.pos[p]])
        sn = float(av @ index.vectors[index.pos[n]])
        out[cid] = sp > sn
    return out


def binom_two_sided(b, n):
    """Exact two-sided binomial p for b successes in n trials at p=0.5."""
    if n == 0:
        return 1.0
    def pmf(k):
        return math.comb(n, k) * 0.5 ** n
    target = pmf(b)
    # Sum every outcome at least as extreme as the observed one. Floating-point ties at the
    # symmetric partner are counted, so a fudge keeps k and n-k both in.
    return min(1.0, sum(pmf(k) for k in range(n + 1) if pmf(k) <= target * (1 + 1e-9)))


def mcnemar(b, c):
    n = b + c
    if n == 0:
        return {'discordant': 0, 'chi2': None, 'p': None, 'exactP': 1.0,
                'note': 'the arms agreed on every triplet'}
    chi2 = (abs(b - c) - 1) ** 2 / n if n else 0.0
    # Survival of chi-square with 1 df is erfc(sqrt(chi2/2)) — no scipy needed.
    p = math.erfc(math.sqrt(chi2 / 2)) if chi2 >= 0 else 1.0
    return {'discordant': n, 'chi2': round(chi2, 4), 'p': round(p, 5),
            'exactP': round(binom_two_sided(b, n), 5),
            'note': 'fewer than 10 discordant pairs — quote exactP, not chi2' if n < 10 else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--triplets', default=os.path.join(V2, 'ruler', 'triplets-final.json'))
    ap.add_argument('--half', choices=['dev', 'test', 'all'], default='dev')
    ap.add_argument('--arm', action='append', default=[],
                    help='name=vectors.bin:keys.json (repeatable); plot and premise-v1 are '
                         'always available by those names')
    ap.add_argument('--a', required=True, help='baseline arm name')
    ap.add_argument('--b', required=True, help='challenger arm name')
    ap.add_argument('--restrict-to', default='smallest')
    ap.add_argument('--out', default=None)
    args = ap.parse_args()

    if args.half == 'test':
        print('!! the SEALED TEST half — a gate run, not a tuning run', file=sys.stderr)

    with open(args.triplets, encoding='utf-8') as fh:
        blob = json.load(fh)
    triplets = [t for t in blob['triplets']
                if args.half == 'all' or half(t['anchor']) == args.half]

    arms = {'plot': load_plot_index(), 'premise-v1': load_premise_v1_index()}
    for spec in args.arm:
        idx = load_extra(spec)
        arms[idx.name] = idx
    for name in (args.a, args.b):
        if name not in arms:
            sys.exit(f'no arm named {name!r} — have {sorted(arms)}')
    arms = {name: arms[name] for name in (args.a, args.b)}

    if args.restrict_to:
        if args.restrict_to == 'smallest':
            pool = min(arms.values(), key=lambda i: len(i.keys)).keys
        else:
            with open(args.restrict_to, encoding='utf-8') as fh:
                pool = json.load(fh)
            if isinstance(pool, dict):
                pool = pool.get('keys') or pool.get('ids')
        shared = set(pool)
        for idx in arms.values():
            shared &= set(idx.keys)
        arms = {n: restrict(i, [k for k in pool if k in shared]) for n, i in arms.items()}
        print(f'both arms restricted to {len(shared)} shared keys', file=sys.stderr)

    oa = outcomes(arms[args.a], triplets)
    ob = outcomes(arms[args.b], triplets)
    paired = [cid for cid in oa if oa[cid] is not None and ob[cid] is not None]
    dropped = len(oa) - len(paired)

    both = sum(1 for c in paired if oa[c] and ob[c])
    only_a = sum(1 for c in paired if oa[c] and not ob[c])
    only_b = sum(1 for c in paired if ob[c] and not oa[c])
    neither = sum(1 for c in paired if not oa[c] and not ob[c])

    test = mcnemar(only_a, only_b)
    winner = None
    if test['discordant'] and (test['p'] or 1.0) < 0.05:
        winner = args.b if only_b > only_a else args.a

    report = {
        'half': args.half, 'pairedTriplets': len(paired), 'droppedForMissingVectors': dropped,
        'a': args.a, 'b': args.b,
        'accuracy': {args.a: round((both + only_a) / len(paired), 4) if paired else None,
                     args.b: round((both + only_b) / len(paired), 4) if paired else None},
        'table': {'bothRight': both, f'only {args.a}': only_a,
                  f'only {args.b}': only_b, 'bothWrong': neither},
        'mcnemar': test,
        # The brief's gate is explicit that a tie fails, so the verdict says so in words
        # rather than leaving a reader to infer adoption from a hair's-breadth lead.
        'verdict': (f'{winner} wins (p={test["p"]})' if winner
                    else f'no significant difference — {args.b} does NOT beat {args.a}, '
                         'which under a tie-fails gate means it is not adopted'),
    }
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as fh:
            json.dump(report, fh, indent=2)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
