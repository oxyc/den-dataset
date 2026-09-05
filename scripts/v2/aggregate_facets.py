#!/usr/bin/env python3
"""Aggregate the Phase-3 facet passes by majority vote, per axis.

Facets differ from premise tags in the one way that matters for aggregation: their
vocabularies are CLOSED. Premise tags needed fuzzy idea-clustering because three passes name
one concept three ways — measured at 17.6% pairwise agreement, which is what made the brief's
n=3 vote destroy richness rather than filter noise. A closed vocabulary has no such problem:
two passes either picked `bittersweet` or they did not, so a plain majority is exact, and the
per-axis agreement rate is a real measurement of how well-defined each axis is rather than an
artefact of naming.

That makes disagreement informative. An axis where three independent readings of the same plot
rarely agree is an axis whose values are not decidable from a plot summary, and shipping it as
a filter chip would make the filter lie. `--min-agreement` drops those axes wholesale, and the
report names them, so a bad axis is removed rather than shipped at low quality.

A `null` is a vote like any other: three passes agreeing that a plot does not say how it ends
is a confident `unknown`, not a missing answer.
"""
import argparse
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llm_phase import load_manifest, read_output, expected_ids  # noqa: E402

V2 = '/Users/cindy/Projects/Personal/den-dataset/out-t02/v2'

# Must match prompts/facets-v1.md exactly. Kept here rather than parsed out of the prompt so
# a drifted prompt fails loudly against this list instead of silently widening the vocabulary.
VOCAB = {
    'era': ['prehistoric', 'ancient', 'medieval', 'early-modern', '19th-century',
            'early-20th-century', 'mid-20th-century', 'late-20th-century', 'contemporary',
            'near-future', 'far-future', 'timeless'],
    'setting': ['urban', 'suburban', 'small-town', 'rural', 'wilderness', 'sea', 'space',
                'underground', 'institution', 'domestic', 'road', 'virtual'],
    'scope': ['single-location', 'single-city', 'regional', 'national', 'global', 'cosmic'],
    'ending': ['happy', 'bittersweet', 'tragic', 'ambiguous', 'open', 'cyclical', 'unknown'],
    'pacing': ['slow-burn', 'steady', 'propulsive', 'episodic', 'frantic'],
    'structure': ['linear', 'nonlinear', 'framed', 'parallel-strands', 'anthology',
                  'single-day'],
    'conflict': ['person-vs-person', 'person-vs-self', 'person-vs-society',
                 'person-vs-nature', 'person-vs-system', 'person-vs-unknown'],
    'ensemble': ['solo', 'duo', 'small-group', 'large-ensemble'],
    'tone': ['earnest', 'comic', 'satirical', 'bleak', 'melancholy', 'pulpy', 'dreamlike',
             'clinical'],
}
CONFIDENCE = {'high', 'medium', 'low'}


def load_pass(phase_dir):
    manifest = load_manifest(phase_dir)
    rows, problems = {}, {}
    stats = collections.Counter()
    for i in range(manifest['batches']):
        data, err = read_output(phase_dir, i)
        if err:
            if err != 'missing':
                problems[i] = err
            continue
        want = set(expected_ids(phase_dir, i))
        for r in data:
            key = r.get('key')
            if key not in want:
                problems[i] = f'row key {key!r} not in this batch'
                break
            clean = {}
            for axis, allowed in VOCAB.items():
                cell = (r.get('facets') or {}).get(axis)
                if not isinstance(cell, dict):
                    stats[f'{axis}.missing'] += 1
                    clean[axis] = None
                    continue
                value = cell.get('value')
                if value is None:
                    clean[axis] = None
                    stats[f'{axis}.null'] += 1
                    continue
                if value not in allowed:
                    # Off-vocabulary is a failure, not a value. Counted per axis so a prompt
                    # that has drifted on one axis is visible rather than averaged away.
                    stats[f'{axis}.offVocab'] += 1
                    clean[axis] = None
                    continue
                conf = cell.get('confidence')
                if conf not in CONFIDENCE:
                    stats[f'{axis}.badConfidence'] += 1
                    conf = None
                clean[axis] = (value, conf)
            rows[key] = clean
            stats['titles'] += 1
    return rows, problems, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', default=os.path.join(V2, 'facets'))
    ap.add_argument('--passes', type=int, default=3)
    ap.add_argument('--min-votes', type=int, default=2)
    ap.add_argument('--min-agreement', type=float, default=0.5,
                    help='drop an axis whose titles reach a majority less often than this — '
                         'an axis this undecidable would make a filter chip lie')
    ap.add_argument('--out', default=None)
    args = ap.parse_args()

    passes, all_problems, all_stats = [], {}, collections.Counter()
    for p in range(1, args.passes + 1):
        rows, problems, stats = load_pass(os.path.join(args.dir, f'pass{p}'))
        passes.append(rows)
        all_problems[f'pass{p}'] = problems
        for k, v in stats.items():
            all_stats[f'pass{p}.{k}'] = v
        print(f'pass{p}: {len(rows)} titles, {len(problems)} problem batches', file=sys.stderr)

    complete = set(passes[0])
    for rows in passes[1:]:
        complete &= set(rows)

    out = {}
    agreed = collections.Counter()
    unanimous = collections.Counter()
    escalate = []
    for key in sorted(complete):
        record = {}
        weak = 0
        for axis in VOCAB:
            votes = collections.Counter()
            confs = collections.defaultdict(list)
            for rows in passes:
                cell = rows[key][axis]
                value = cell[0] if cell else None
                votes[value] += 1
                if cell and cell[1]:
                    confs[value].append(cell[1])
            top, n = votes.most_common(1)[0]
            if n == args.passes:
                unanimous[axis] += 1
            if n >= args.min_votes:
                agreed[axis] += 1
                record[axis] = {
                    'value': top,
                    'votes': n,
                    # The shipped confidence is the weakest any agreeing pass gave it: an axis
                    # two passes agreed on but one of them called `low` is a low-confidence
                    # facet, and rounding that up would be inventing certainty.
                    'confidence': (min(confs[top], key=['high', 'medium', 'low'].index)
                                   if confs[top] else None),
                }
            else:
                record[axis] = None      # no majority — the passes genuinely disagreed
                weak += 1
        out[key] = record
        if weak:
            escalate.append({'key': key, 'axesWithoutMajority': weak})

    total = max(len(complete), 1)
    per_axis = {axis: {'majorityRate': round(agreed[axis] / total, 4),
                       'unanimousRate': round(unanimous[axis] / total, 4)}
                for axis in VOCAB}
    dropped = [a for a, s in per_axis.items() if s['majorityRate'] < args.min_agreement]
    for key in out:
        for axis in dropped:
            out[key].pop(axis, None)

    dest = args.out or os.path.join(args.dir, 'facets-aggregated.json')
    with open(dest, 'w', encoding='utf-8') as fh:
        json.dump({'titles': len(out), 'axes': [a for a in VOCAB if a not in dropped],
                   'records': out}, fh)
    with open(os.path.join(args.dir, 'facet-escalate.json'), 'w', encoding='utf-8') as fh:
        json.dump({'count': len(escalate), 'titles': escalate}, fh)

    print(json.dumps({
        'titlesAggregated': len(out),
        'perAxis': per_axis,
        'axesDropped': dropped,
        'minAgreement': args.min_agreement,
        'titlesWithAnyUndecidedAxis': len(escalate),
        'validation': dict(all_stats),
        'problemBatches': {p: len(v) for p, v in all_problems.items()},
    }, indent=2))


if __name__ == '__main__':
    main()
