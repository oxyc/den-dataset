#!/usr/bin/env python3
"""Un-blind the three judging passes and keep the triplets two of three confirm.

A pass **confirms** a triplet when, judging blind, it called the proposed positive a `twin`
AND the proposed negative `unrelated`. Anything less is not a usable test case: a triplet
whose negative is actually `related` does not distinguish a good index from a bad one, and
one whose positive is only `related` punishes an index for being right.

Reported alongside:

- **disagreementRate** — triplets the three passes did not agree on unanimously. This is the
  ruler's own noise floor. A ruler with a 40% disagreement rate cannot adjudicate a 5%
  difference between two indexes, and saying so is more useful than a confident number.
- **verdictAxisViolations** — cases where a judge's stated verdict does not follow from the
  axes it wrote. The thresholds are mechanical, so a violation means the judge overruled
  its own evidence; those judgements are dropped, not repaired.
- **positionBias** — confirm rate split by which slot held the positive. The slots are
  shuffled per case, so a gap here is the judge favouring a position rather than a film,
  which would quietly inflate everything downstream.
"""
import argparse
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llm_phase import load_manifest, read_output, expected_ids  # noqa: E402
from split import half  # noqa: E402

V2 = '/Users/cindy/Projects/Personal/den-dataset/out-t02/v2'
JUDGE = os.path.join(V2, 'ruler', 'judge')


def verdict_from_axes(axes):
    if not isinstance(axes, dict):
        return None
    try:
        true_count = sum(1 for k in ('situation', 'engine', 'goal', 'relationship',
                                     'obstacle', 'device') if axes[k] is True)
    except KeyError:
        return None
    if true_count >= 4 and (axes['situation'] is True or axes['device'] is True):
        return 'twin'
    if true_count >= 2:
        return 'related'
    return 'unrelated'


def load_pass(p, judge_dir=JUDGE):
    phase = os.path.join(judge_dir, f'pass{p}')
    manifest = load_manifest(phase)
    rows = {}
    problems = {}
    violations = 0
    for i in range(manifest['batches']):
        data, err = read_output(phase, i)
        if err:
            if err != 'missing':
                problems[i] = err
            continue
        want = set(expected_ids(phase, i))
        for r in data:
            cid = r.get('id')
            if cid not in want:
                problems[i] = f'row id {cid!r} not in this batch'
                break
            entry = {}
            bad = False
            for slot in ('a', 'b'):
                side = r.get(slot) or {}
                derived = verdict_from_axes(side.get('axes'))
                if derived is None:
                    bad = True
                    break
                if side.get('verdict') != derived:
                    # The judge overruled its own axes. Trust neither; drop the case.
                    violations += 1
                    bad = True
                    break
                entry[slot] = {'verdict': derived, 'axes': side['axes'],
                               'reason': side.get('reason')}
            if not bad:
                rows[cid] = entry
    return rows, problems, violations


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--passes', type=int, default=3)
    ap.add_argument('--min-confirm', type=int, default=2)
    # Overridable so the consolidation can be dry-run against the smoke-test directory before
    # the real judging budget is spent. A bug found here after paying for judging is expensive.
    ap.add_argument('--judge-dir', default=JUDGE)
    ap.add_argument('--out', default=os.path.join(V2, 'ruler', 'triplets-final.json'))
    args = ap.parse_args()

    with open(os.path.join(args.judge_dir, 'truth.json'), encoding='utf-8') as fh:
        truth = json.load(fh)
    # Which mining slot proposed each positive. `near` candidates come from keyword
    # similarity, which correlates with plot-surface similarity, so an index result that
    # holds only on `near` positives is partly the miner talking. Carried through so the
    # scorer can split on it.
    prov_path = os.path.join(V2, 'ruler', 'gen', 'provenance.json')
    provenance = {}
    if os.path.exists(prov_path):
        with open(prov_path, encoding='utf-8') as fh:
            provenance = json.load(fh)

    passes, problems, violations = [], {}, {}
    for p in range(1, args.passes + 1):
        rows, probs, viol = load_pass(p, args.judge_dir)
        passes.append(rows)
        problems[f'pass{p}'] = len(probs)
        violations[f'pass{p}'] = viol
        print(f'pass{p}: {len(rows)} judged cases, {len(probs)} problem batches, '
              f'{viol} verdict/axis violations', file=sys.stderr)

    judged_by_all = set(passes[0])
    for rows in passes[1:]:
        judged_by_all &= set(rows)

    kept, confirms_hist = [], collections.Counter()
    disagreements = 0
    by_slot = collections.Counter()
    by_slot_total = collections.Counter()
    for cid in sorted(judged_by_all):
        t = truth[cid]
        pos_slot = t['positiveSlot']
        neg_slot = 'b' if pos_slot == 'a' else 'a'
        votes = []
        for rows in passes:
            r = rows[cid]
            votes.append(r[pos_slot]['verdict'] == 'twin' and r[neg_slot]['verdict'] == 'unrelated')
        n = sum(votes)
        confirms_hist[n] += 1
        if 0 < n < args.passes:
            disagreements += 1
        by_slot_total[pos_slot] += 1
        if n >= args.min_confirm:
            by_slot[pos_slot] += 1
            prov = provenance.get(t['anchor'], {})
            if t['positive'] in prov.get('near', ()):
                source = 'near'
            elif t['positive'] in prov.get('decoy', ()):
                source = 'decoy'
            else:
                source = None
            kept.append({
                'id': cid, 'anchor': t['anchor'],
                'positive': t['positive'], 'negative': t['negative'],
                'positiveSource': source,
                'confirms': n,
                'half': half(t['anchor']),
                'reasons': [rows[cid][pos_slot]['reason'] for rows in passes],
            })

    meta = {
        'casesJudgedByAllPasses': len(judged_by_all),
        'kept': len(kept),
        'keepRate': round(len(kept) / max(len(judged_by_all), 1), 4),
        'confirmHistogram': {str(k): v for k, v in sorted(confirms_hist.items())},
        'disagreementRate': round(disagreements / max(len(judged_by_all), 1), 4),
        'unanimousRate': round((confirms_hist[0] + confirms_hist[args.passes])
                               / max(len(judged_by_all), 1), 4),
        'verdictAxisViolations': violations,
        'problemBatches': problems,
        'positionBias': {
            slot: round(by_slot[slot] / by_slot_total[slot], 4) if by_slot_total[slot] else None
            for slot in ('a', 'b')
        },
        'minConfirm': args.min_confirm,
        'passes': args.passes,
    }
    meta['split'] = dict(collections.Counter(t['half'] for t in kept))
    meta['positiveSource'] = dict(collections.Counter(str(t['positiveSource']) for t in kept))

    with open(args.out, 'w', encoding='utf-8') as fh:
        json.dump({'meta': meta, 'triplets': kept}, fh)
    print(json.dumps(meta, indent=2))


if __name__ == '__main__':
    main()
