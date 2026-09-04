#!/usr/bin/env python3
"""Build the Phase-1b model bake-off: the same titles tagged three ways.

Arms: `haiku-n3`, `sonnet-n3`, `sonnet-n1`. If Haiku n=3 matches Sonnet n=3 on the premise
rubric, Phase 2 costs a fraction of what it otherwise would — that is the decision this
exists to make, and it is made on the numbers, before the 38,460-title run.

**The title set is chosen by the ruler, not sampled independently.** Scoring an arm means
comparing sim(anchor, positive) against sim(anchor, negative), which needs a vector for all
three titles *in that arm's own tag space*. A random 300 titles would cover almost no
complete triplet (a 300/38,460 subset covers ~0.0005% of them), so the bake-off would have
nothing to score. Selecting DEV triplets and taking their titles gives 100% coverage for a
similar amount of tagging.

DEV only. The bake-off picks a model, which is tuning, and tuning never touches TEST.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from split import half  # noqa: E402

V2 = '/Users/cindy/Projects/Personal/den-dataset/out-t02/v2'
CORPUS = os.path.join(V2, 'corpus', 'wikiplot-corpus.jsonl')

ARMS = {'haiku-n3': 3, 'sonnet-n3': 3, 'sonnet-n1': 1}
MAX_HEAD = 6000
MAX_TAIL = 1200


def clamp(plot):
    if len(plot) <= MAX_HEAD + MAX_TAIL + 40:
        return plot
    return plot[:MAX_HEAD].rstrip() + '\n[…]\n' + plot[-MAX_TAIL:].lstrip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--triplets', default=os.path.join(V2, 'ruler', 'triplets-final.json'))
    ap.add_argument('--cases', type=int, default=200, help='DEV triplets the bake-off is scored on')
    ap.add_argument('--per-batch', type=int, default=40)
    ap.add_argument('--out-dir', default=os.path.join(V2, 'bakeoff'))
    args = ap.parse_args()

    with open(args.triplets, encoding='utf-8') as fh:
        triplets = [t for t in json.load(fh)['triplets'] if half(t['anchor']) == 'dev']
    if not triplets:
        sys.exit('no DEV triplets — run the ruler first')
    # Prefer unanimously-confirmed triplets: the bake-off is a model comparison, and a noisy
    # case set widens both arms' error bars rather than separating them.
    triplets.sort(key=lambda t: -t['confirms'])
    chosen = triplets[:args.cases]

    keys = []
    seen = set()
    for t in chosen:
        for k in (t['anchor'], t['positive'], t['negative']):
            if k not in seen:
                seen.add(k)
                keys.append(k)

    corpus = {}
    with open(CORPUS, encoding='utf-8') as fh:
        for line in fh:
            r = json.loads(line)
            if r['key'] in seen:
                corpus[r['key']] = r
    missing = seen - set(corpus)
    if missing:
        sys.exit(f'{len(missing)} bake-off titles are not in the wiki-plot corpus, e.g. '
                 f'{sorted(missing)[:3]} — a title without a wiki plot must never be tagged')

    items = [{'key': k, 'title': corpus[k]['title'], 'year': corpus[k]['year'],
              'mediaType': corpus[k]['mediaType'], 'plot': clamp(corpus[k]['plot'])}
             for k in keys]
    batches = [items[i:i + args.per_batch] for i in range(0, len(items), args.per_batch)]

    manifest = {'titles': len(items), 'batches': len(batches), 'perBatch': args.per_batch,
                'cases': len(chosen), 'half': 'dev',
                'provenance': 'every record has hasWikiPlot == true; no TMDB overview prose',
                'ids': keys}

    for arm, passes in ARMS.items():
        for p in range(1, passes + 1):
            pdir = os.path.join(args.out_dir, arm, f'pass{p}')
            os.makedirs(os.path.join(pdir, 'in'), exist_ok=True)
            os.makedirs(os.path.join(pdir, 'out'), exist_ok=True)
            for i, b in enumerate(batches):
                with open(os.path.join(pdir, 'in', f'batch-{i:04d}.json'), 'w', encoding='utf-8') as fh:
                    # indent=1 so a batch fits one Read call; minified it exceeds the limit
                    # and every worker has to pretty-print a copy first.
                    json.dump(b, fh, ensure_ascii=False, indent=1)
            with open(os.path.join(pdir, 'manifest.json'), 'w', encoding='utf-8') as fh:
                json.dump(manifest, fh)

    with open(os.path.join(args.out_dir, 'cases.json'), 'w', encoding='utf-8') as fh:
        json.dump({'cases': chosen}, fh)

    print(json.dumps({'arms': list(ARMS), 'titles': len(items), 'batches': len(batches),
                      'cases': len(chosen),
                      'subagentCalls': sum(len(batches) * p for p in ARMS.values())}, indent=2))


if __name__ == '__main__':
    main()
