#!/usr/bin/env python3
"""Score index arms on the premise ruler (triplets), on one half of the split.

Metrics, and why each is here:

- **tripletAccuracy** — fraction of triplets where sim(anchor, positive) > sim(anchor,
  negative). The primary premise-discrimination number. Chance is 0.5, and it is
  scale-invariant by construction: it compares two fixed titles, so it does not harden as
  the corpus grows. DT-H's "/12" absolute rank did harden — a subset rank-10 is rank-246 at
  full scale — which is why that form is not reused.
- **positivePercentile** — where the positive lands in the anchor's full-corpus ranking.
  DT-H's success criterion is the top 0.1%; this is the number that answers it.
- **negativeLeak** — the negative beats the positive *and* sits in the anchor's top 10. A
  ranker can have decent triplet accuracy and still surface confusable wrong answers where
  a user sees them.
- **margin** — mean sim(a,pos) - sim(a,neg). Reported because accuracy alone hides whether
  a win is robust or a coin-flip.

Arms with no vector for a title skip that triplet, and the skip count is reported: an arm
that quietly scores fewer cases is not comparable to one that scored all of them.
"""
import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from index_io import load_plot_index, load_premise_v1_index, Index, read_blob  # noqa: E402
from split import half  # noqa: E402

V2 = '/Users/cindy/Projects/Personal/den-dataset/out-t02/v2'


def load_extra(spec):
    """--arm name=vectors.bin:keys.json — for scoring a v2 blob without touching v1."""
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


def score(index, triplets):
    wins = 0
    scored = 0
    skipped = 0
    margins = []
    percentiles = []
    leaks = 0
    for t in triplets:
        a, p, n = t['anchor'], t['positive'], t['negative']
        if a not in index.pos or p not in index.pos or n not in index.pos:
            skipped += 1
            continue
        av = index.vectors[index.pos[a]]
        sp = float(av @ index.vectors[index.pos[p]])
        sn = float(av @ index.vectors[index.pos[n]])
        scored += 1
        margins.append(sp - sn)
        if sp > sn:
            wins += 1
        sims = index.vectors @ av
        sims[index.pos[a]] = -2.0
        rank = int((sims > sp).sum())
        percentiles.append(rank / (len(index.keys) - 1))
        if sn > sp and int((sims > sn).sum()) < 10:
            leaks += 1
    percentiles.sort()
    return {
        'tripletAccuracy': round(wins / scored, 4) if scored else None,
        'margin': round(float(np.mean(margins)), 4) if margins else None,
        'positivePercentileMedian': round(percentiles[len(percentiles) // 2], 5) if percentiles else None,
        'positiveInTop0_1pct': round(sum(1 for x in percentiles if x <= 0.001) / len(percentiles), 4)
        if percentiles else None,
        'positiveInTop1pct': round(sum(1 for x in percentiles if x <= 0.01) / len(percentiles), 4)
        if percentiles else None,
        'negativeLeak': round(leaks / scored, 4) if scored else None,
        'scored': scored, 'skipped': skipped,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--triplets', default=os.path.join(V2, 'ruler', 'triplets-final.json'))
    ap.add_argument('--half', choices=['dev', 'test', 'all'], default='dev')
    ap.add_argument('--arm', action='append', default=[],
                    help='name=vectors.bin:keys.json — adds an arm beside plot and premise-v1')
    ap.add_argument('--out', default=None)
    args = ap.parse_args()

    if args.half == 'test':
        print('!! scoring the SEALED TEST half — this is a gate run, not a tuning run', file=sys.stderr)

    with open(args.triplets, encoding='utf-8') as fh:
        blob = json.load(fh)
    triplets = [t for t in blob['triplets']
                if args.half == 'all' or half(t['anchor']) == args.half]

    arms = {'plot': load_plot_index(), 'premise-v1': load_premise_v1_index()}
    for spec in args.arm:
        idx = load_extra(spec)
        arms[idx.name] = idx

    results = {name: score(index, triplets) for name, index in arms.items()}

    # Split by which mining slot proposed the positive. `near` positives come from keyword
    # similarity, which tilts toward the plot index (measured: 1.23 sd of lift vs the premise
    # index's 0.98). If an arm's advantage exists only on `near`, it is the miner's, not the
    # index's — so the two are always reported side by side rather than pooled.
    by_source = {}
    for source in ('near', 'decoy'):
        subset = [t for t in triplets if t.get('positiveSource') == source]
        if subset:
            by_source[source] = {'triplets': len(subset),
                                 'arms': {n: score(i, subset) for n, i in arms.items()}}

    # Split by the anchor's media type. This is the ONLY measurement in the whole v2
    # programme that says anything about series: MovieLens ml-32m carries no TV ids, so the
    # co-rating ruler is structurally blind to the ~10% of the index that is television.
    # Reporting it separately is the difference between "we measured the index" and "we
    # measured the movie part of the index and hoped".
    by_media = {}
    for media in ('movie', 'tv'):
        subset = [t for t in triplets if t['anchor'].startswith(media + ':')]
        if subset:
            by_media[media] = {'triplets': len(subset),
                               'arms': {n: score(i, subset) for n, i in arms.items()}}

    report = {'half': args.half, 'triplets': len(triplets),
              'rulerAgreement': blob.get('meta', {}), 'arms': results,
              'byPositiveSource': by_source, 'byMedia': by_media}
    out = args.out or os.path.join(V2, 'ruler', f'triplet-scores-{args.half}.json')
    with open(out, 'w', encoding='utf-8') as fh:
        json.dump(report, fh, indent=2)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
