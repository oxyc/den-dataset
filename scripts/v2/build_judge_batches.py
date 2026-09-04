#!/usr/bin/env python3
"""Turn generated triplets into blind judging batches, three independent passes.

The judge is not told which candidate is the proposed positive: `a` and `b` are shuffled
per case, deterministically from the case id. A judge that can see which slot is which will
confirm the proposer, and three passes of that would measure nothing but the proposer's own
confidence. Shuffling is what makes 2-of-3 agreement mean something.

Only triplets with BOTH a positive and a negative are judged — an anchor whose pool held no
twin has nothing to adjudicate.

Reads the generation phase strictly (`llm_phase.read_output`), so a truncated or malformed
batch is reported rather than silently contributing nothing.
"""
import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llm_phase import load_manifest, read_output, expected_ids  # noqa: E402

V2 = '/Users/cindy/Projects/Personal/den-dataset/out-t02/v2'
GEN = os.path.join(V2, 'ruler', 'gen')
CORPUS = os.path.join(V2, 'corpus', 'wikiplot-corpus.jsonl')

HEAD_CHARS = 1400
TAIL_CHARS = 500


def excerpt(plot):
    if len(plot) <= HEAD_CHARS + TAIL_CHARS + 40:
        return plot
    return plot[:HEAD_CHARS].rstrip() + '\n[…]\n' + plot[-TAIL_CHARS:].lstrip()


def flip(case_id):
    """Deterministic per-case shuffle: reproducible, and not correlated with input order."""
    return hashlib.sha256(('flip|' + case_id).encode('utf-8')).digest()[0] & 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--per-batch', type=int, default=25)
    ap.add_argument('--passes', type=int, default=3)
    ap.add_argument('--out-dir', default=os.path.join(V2, 'ruler', 'judge'))
    args = ap.parse_args()

    corpus = {}
    with open(CORPUS, encoding='utf-8') as fh:
        for line in fh:
            r = json.loads(line)
            corpus[r['key']] = r

    manifest = load_manifest(GEN)
    triplets = []
    problems = {}
    generated = 0
    for i in range(manifest['batches']):
        rows, err = read_output(GEN, i)
        if err:
            if err != 'missing':
                problems[i] = err
            continue
        want = set(expected_ids(GEN, i))
        for r in rows:
            key = r.get('key')
            if key not in want:
                problems[i] = f'row key {key!r} not in this batch'
                break
            generated += 1
            pos, neg = r.get('positive'), r.get('negative')
            if not pos or not neg:
                continue
            if pos['key'] == neg['key'] or key in (pos['key'], neg['key']):
                problems.setdefault(i, 'a triplet reuses a key across slots')
                continue
            if pos['key'] not in corpus or neg['key'] not in corpus:
                problems.setdefault(i, 'a triplet names a key absent from the corpus')
                continue
            triplets.append({'id': f'{key}#{len(triplets)}', 'anchor': key,
                             'positive': pos['key'], 'negative': neg['key'],
                             'genPositive': pos, 'genNegative': neg})

    if problems:
        print(f'!! {len(problems)} generation batches have problems:', file=sys.stderr)
        for i, why in sorted(problems.items())[:20]:
            print(f'   batch-{i:04d}: {why}', file=sys.stderr)

    cases = []
    for t in triplets:
        first, second = (t['negative'], t['positive']) if flip(t['id']) else (t['positive'], t['negative'])
        cases.append({
            'id': t['id'],
            'anchor': {'title': corpus[t['anchor']]['title'], 'year': corpus[t['anchor']]['year'],
                       'plot': excerpt(corpus[t['anchor']]['plot'])},
            'a': {'title': corpus[first]['title'], 'year': corpus[first]['year'],
                  'plot': excerpt(corpus[first]['plot'])},
            'b': {'title': corpus[second]['title'], 'year': corpus[second]['year'],
                  'plot': excerpt(corpus[second]['plot'])},
        })

    batches = [cases[i:i + args.per_batch] for i in range(0, len(cases), args.per_batch)]
    key_manifest = {
        'anchorsGenerated': generated,
        'triplets': len(triplets),
        'batches': len(batches),
        'perBatch': args.per_batch,
        'passes': args.passes,
        'blind': True,
        'ids': [c['id'] for c in cases],
    }
    for p in range(1, args.passes + 1):
        pdir = os.path.join(args.out_dir, f'pass{p}')
        os.makedirs(os.path.join(pdir, 'in'), exist_ok=True)
        os.makedirs(os.path.join(pdir, 'out'), exist_ok=True)
        for i, b in enumerate(batches):
            with open(os.path.join(pdir, 'in', f'batch-{i:04d}.json'), 'w', encoding='utf-8') as fh:
                json.dump(b, fh, ensure_ascii=False)
        with open(os.path.join(pdir, 'manifest.json'), 'w', encoding='utf-8') as fh:
            json.dump(key_manifest, fh)

    # The key the scorer needs to un-blind: which slot held the proposed positive.
    with open(os.path.join(args.out_dir, 'truth.json'), 'w', encoding='utf-8') as fh:
        json.dump({t['id']: {'anchor': t['anchor'], 'positive': t['positive'],
                             'negative': t['negative'],
                             'positiveSlot': 'b' if flip(t['id']) else 'a',
                             'gen': {'positive': t['genPositive'], 'negative': t['genNegative']}}
                   for t in triplets}, fh)

    print(json.dumps({k: v for k, v in key_manifest.items() if k != 'ids'}, indent=2))
    print(f'generation batches with problems: {len(problems)}')


if __name__ == '__main__':
    main()
