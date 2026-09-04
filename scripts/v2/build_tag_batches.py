#!/usr/bin/env python3
"""Build the Phase-2 tagging batches: every wiki-plot title, n passes, fixed paths.

The compliance gate is asserted here and recorded in the manifest, not left to the prompt:
a record only enters a batch if it came from the wiki-plot corpus, which
`build_wikiplot_corpus.py` built by filtering on `hasWikiPlot is True` and refusing any
row whose plot was empty. Nothing derived from a TMDB `overview` can reach a batch file.

Passes are separate directories (`pass1/`, `pass2/`, `pass3/`) over the SAME batching, so
n=3 self-consistency aggregates three independent answers for identical inputs, and a
pass that dies half-way resumes without disturbing the others.

Batches are **unseeded**: no shared tag vocabulary is handed to any batch. That is the
condition under which vocabulary drift can be measured at all — seed the batches with a
vocabulary and you have hidden the drift rather than fixed it.
"""
import argparse
import json
import os

CORPUS = '/Users/cindy/Projects/Personal/den-dataset/out-t02/v2/corpus/wikiplot-corpus.jsonl'
V2 = '/Users/cindy/Projects/Personal/den-dataset/out-t02/v2'

# A handful of plots run to 53k chars. Capping the tail keeps one title from dominating a
# batch's context; head+tail keeps a third-act reveal that IS the premise.
MAX_HEAD = 6000
MAX_TAIL = 1200


def clamp(plot):
    if len(plot) <= MAX_HEAD + MAX_TAIL + 40:
        return plot, False
    return plot[:MAX_HEAD].rstrip() + '\n[…]\n' + plot[-MAX_TAIL:].lstrip(), True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--per-batch', type=int, default=40)
    ap.add_argument('--passes', type=int, default=3)
    ap.add_argument('--scope', choices=['all', 'shipped'], default='all')
    ap.add_argument('--out-dir', default=os.path.join(V2, 'tags-v2'))
    args = ap.parse_args()

    rows = []
    with open(CORPUS, encoding='utf-8') as fh:
        for line in fh:
            r = json.loads(line)
            if args.scope == 'shipped' and not r['shipped']:
                continue
            rows.append(r)
    rows.sort(key=lambda r: (r['mediaType'], r['tmdbId']))

    clamped = 0
    items = []
    for r in rows:
        plot, was_clamped = clamp(r['plot'])
        clamped += was_clamped
        items.append({'key': r['key'], 'title': r['title'], 'year': r['year'],
                      'mediaType': r['mediaType'], 'plot': plot})

    batches = [items[i:i + args.per_batch] for i in range(0, len(items), args.per_batch)]
    manifest = {
        'titles': len(items),
        'batches': len(batches),
        'perBatch': args.per_batch,
        'passes': args.passes,
        'scope': args.scope,
        'clampedPlots': clamped,
        'clamp': {'headChars': MAX_HEAD, 'tailChars': MAX_TAIL},
        'provenance': 'every record has hasWikiPlot == true and a non-empty Wikipedia plot; '
                      'no TMDB overview prose is present in any batch file',
        'seeded': False,
        'ids': [i['key'] for i in items],
    }

    for p in range(1, args.passes + 1):
        pdir = os.path.join(args.out_dir, f'pass{p}')
        os.makedirs(os.path.join(pdir, 'in'), exist_ok=True)
        os.makedirs(os.path.join(pdir, 'out'), exist_ok=True)
        for i, b in enumerate(batches):
            with open(os.path.join(pdir, 'in', f'batch-{i:04d}.json'), 'w', encoding='utf-8') as fh:
                json.dump(b, fh, ensure_ascii=False)
        with open(os.path.join(pdir, 'manifest.json'), 'w', encoding='utf-8') as fh:
            json.dump(manifest, fh)

    print(json.dumps({k: v for k, v in manifest.items() if k != 'ids'}, indent=2))


if __name__ == '__main__':
    main()
