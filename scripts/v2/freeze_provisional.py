#!/usr/bin/env python3
"""Freeze the generated triplets into a scoreable set, without blind judging.

Blind judging is what turns a proposal into a ruler, and it did not run — the session hit
its model limit. This writes the same shape `score_triplets.py` consumes so every number
already reported is reproducible from a file rather than from a loop in a transcript, and
marks the set **provisional** in its own metadata so nothing downstream can mistake it for
the confirmed ruler.

The single-pass blind-judge smoke test (25 cases) agreed with the proposer on 84% of
positives and 84% of negatives, so a provisional set is not worthless — but it is the
proposer grading itself, which is exactly the weakness the 2-of-3 design exists to remove.
Treat every triplet number derived from this file as provisional.
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
GEN = os.path.join(V2, 'ruler', 'gen')
AXES = ('situation', 'engine', 'goal', 'relationship', 'obstacle', 'device')


def derived(axes):
    n = sum(1 for k in AXES if axes.get(k) is True)
    if n >= 4 and (axes.get('situation') is True or axes.get('device') is True):
        return 'twin'
    return 'related' if n >= 2 else 'unrelated'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=os.path.join(V2, 'ruler', 'triplets-provisional.json'))
    args = ap.parse_args()

    prov_path = os.path.join(GEN, 'provenance.json')
    provenance = {}
    if os.path.exists(prov_path):
        with open(prov_path, encoding='utf-8') as fh:
            provenance = json.load(fh)

    manifest = load_manifest(GEN)
    kept, problems = [], {}
    anchors = batches_done = violations = 0
    for i in range(manifest['batches']):
        rows, err = read_output(GEN, i)
        if err:
            if err != 'missing':
                problems[i] = err
            continue
        batches_done += 1
        want = set(expected_ids(GEN, i))
        for r in rows:
            key = r.get('key')
            if key not in want:
                problems[i] = f'row key {key!r} not in this batch'
                break
            anchors += 1
            pos, neg = r.get('positive'), r.get('negative')
            if not pos or not neg:
                continue
            # The generator's verdict must follow from its own axes, or the case is dropped —
            # the same rule the blind judge would have applied.
            if derived(pos['axes']) != pos['verdict'] or derived(neg['axes']) != neg['verdict']:
                violations += 1
                continue
            if pos['verdict'] != 'twin' or neg['verdict'] != 'unrelated':
                continue
            prov = provenance.get(key, {})
            source = ('near' if pos['key'] in prov.get('near', ())
                      else 'decoy' if pos['key'] in prov.get('decoy', ()) else None)
            kept.append({'id': key, 'anchor': key, 'positive': pos['key'],
                         'negative': neg['key'], 'positiveSource': source,
                         'half': half(key), 'confirms': None,
                         'reasons': [pos.get('reason')]})

    meta = {
        'PROVISIONAL': True,
        'why': 'generator-proposed only; the 3-pass blind judging that would confirm these did '
               'not run (session model limit). A single-pass blind smoke test over 25 cases '
               'agreed with the proposer on 84% of positives and 84% of negatives.',
        'batchesComplete': batches_done,
        'batchesInScope': manifest['batches'],
        'anchorsSeen': anchors,
        'triplets': len(kept),
        'yield': round(len(kept) / max(anchors, 1), 4),
        'verdictAxisViolationsDropped': violations,
        'problemBatches': len(problems),
        'split': dict(collections.Counter(t['half'] for t in kept)),
        'media': dict(collections.Counter(t['anchor'].split(':')[0] for t in kept)),
        'positiveSource': dict(collections.Counter(str(t['positiveSource']) for t in kept)),
    }
    with open(args.out, 'w', encoding='utf-8') as fh:
        json.dump({'meta': meta, 'triplets': kept}, fh)
    print(json.dumps(meta, indent=2))


if __name__ == '__main__':
    main()
