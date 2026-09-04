#!/usr/bin/env python3
"""Measure vocabulary drift: one concept named differently across batches.

The batches are run **unseeded** — no shared vocabulary is handed to any of them — because
seeding hides drift rather than removing it. This measures what that costs.

Two numbers, and the second is the one that matters:

- **synonymMultiplicity** — mean distinct surface forms per idea cluster. Tells you how
  fragmented the vocabulary is overall, but a high value can be innocent: `time-loop` and
  `repeating-day` genuinely differ in nuance.
- **batchAttributableDrift** — for idea clusters that appear in many batches, the
  probability that two occurrences use *different* surface forms when they are in different
  batches, minus the same probability within one batch. Within-batch is the floor: a single
  worker naming one idea two ways is ordinary variation, not drift. Anything above that
  floor is caused by the batch boundary itself, which is the thing that could be fixed by
  seeding a vocabulary — so this is the number that decides whether seeding is worth it.

Clusters use the same `same_idea` token test as the aggregator, so a drift figure here and
a vote there are talking about the same notion of "the same tag".
"""
import argparse
import collections
import itertools
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aggregate_tags import same_idea  # noqa: E402
from llm_phase import load_manifest, read_output, expected_ids  # noqa: E402

V2 = '/Users/cindy/Projects/Personal/den-dataset/out-t02/v2'


def collect(phase_dir):
    """(tag, batch_index) occurrences across one pass."""
    manifest = load_manifest(phase_dir)
    occurrences = []
    for i in range(manifest['batches']):
        data, err = read_output(phase_dir, i)
        if err:
            continue
        want = set(expected_ids(phase_dir, i))
        for r in data:
            if r.get('key') not in want:
                continue
            for t in r.get('tags') or []:
                tag = (t.get('tag') or '').strip().lower()
                if tag:
                    occurrences.append((tag, i))
    return occurrences


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--phase', default=os.path.join(V2, 'tags-v2', 'pass1'))
    ap.add_argument('--min-occurrences', type=int, default=20,
                    help='cluster size floor — a rare idea has too few samples to measure')
    ap.add_argument('--max-vocab', type=int, default=4000,
                    help='cluster only the N most frequent surface forms; the long tail is '
                         'singletons by construction and would dominate the cost')
    ap.add_argument('--out', default=os.path.join(V2, 'tags-v2', 'drift.json'))
    args = ap.parse_args()

    occurrences = collect(args.phase)
    if not occurrences:
        sys.exit(f'no tag occurrences under {args.phase} — nothing to measure')
    counts = collections.Counter(t for t, _ in occurrences)
    vocab = [t for t, _ in counts.most_common(args.max_vocab)]
    print(f'{len(occurrences):,} occurrences · {len(counts):,} surface forms · '
          f'clustering top {len(vocab):,}', file=sys.stderr)

    # Greedy single-link clustering over the frequent vocabulary.
    clusters = []
    index_by_token = collections.defaultdict(list)
    for tag in vocab:
        target = None
        seen = set()
        for tok in tag.split('-'):
            for ci in index_by_token.get(tok, ()):
                if ci in seen:
                    continue
                seen.add(ci)
                if any(same_idea(tag, s) for s in clusters[ci]):
                    target = ci
                    break
            if target is not None:
                break
        if target is None:
            clusters.append([tag])
            target = len(clusters) - 1
        else:
            clusters[target].append(tag)
        for tok in tag.split('-'):
            index_by_token[tok].append(target)

    cluster_of = {}
    for ci, members in enumerate(clusters):
        for tag in members:
            cluster_of[tag] = ci

    by_cluster = collections.defaultdict(list)
    for tag, batch in occurrences:
        ci = cluster_of.get(tag)
        if ci is not None:
            by_cluster[ci].append((tag, batch))

    rng = random.Random(20260904)
    same_batch_diff = same_batch_total = 0
    cross_batch_diff = cross_batch_total = 0
    multi = []
    examples = []
    for ci, occ in by_cluster.items():
        if len(occ) < args.min_occurrences:
            continue
        forms = {t for t, _ in occ}
        multi.append(len(forms))
        if len(forms) > 1 and len(examples) < 40:
            examples.append(sorted(forms)[:6])
        # Sample pairs rather than enumerating: a popular cluster has O(n^2) pairs.
        pairs = min(4000, len(occ) * (len(occ) - 1) // 2)
        for _ in range(pairs):
            (t1, b1), (t2, b2) = rng.sample(occ, 2)
            if b1 == b2:
                same_batch_total += 1
                same_batch_diff += t1 != t2
            else:
                cross_batch_total += 1
                cross_batch_diff += t1 != t2

    within = same_batch_diff / same_batch_total if same_batch_total else None
    across = cross_batch_diff / cross_batch_total if cross_batch_total else None
    report = {
        'phase': args.phase,
        'occurrences': len(occurrences),
        'surfaceForms': len(counts),
        'formsUsedOnce': sum(1 for v in counts.values() if v == 1),
        'clustersMeasured': len(multi),
        'synonymMultiplicity': round(sum(multi) / len(multi), 3) if multi else None,
        'withinBatchDisagreement': round(within, 4) if within is not None else None,
        'crossBatchDisagreement': round(across, 4) if across is not None else None,
        'batchAttributableDrift': round(across - within, 4)
        if (within is not None and across is not None) else None,
        'note': 'batchAttributableDrift is cross minus within. Within-batch variation is the '
                'floor — one worker naming an idea two ways is ordinary. Only the excess is '
                'caused by the batch boundary, and only that excess could be fixed by seeding '
                'a shared vocabulary.',
        'sampleClusters': examples,
    }
    with open(args.out, 'w', encoding='utf-8') as fh:
        json.dump(report, fh, indent=2)
    print(json.dumps({k: v for k, v in report.items() if k != 'sampleClusters'}, indent=2))


if __name__ == '__main__':
    main()
