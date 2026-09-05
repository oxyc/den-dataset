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

# passes, and the batch size that model can actually finish.
#
# Batch size is a property of the MODEL, not of the task. 40 titles x ~15 tags fits inside
# Haiku's 64,000-token output cap; Sonnet writes more per tag and overran the cap on its
# first bake-off batch, which does not degrade gracefully — it produces an empty file, so the
# arm looks unrun rather than over-asked. 25 leaves Sonnet the headroom. The sizes differ per
# arm on purpose, and because batch size decides the id numbering, an arm's size must not be
# changed once it has outputs on disk: use --arm to rebuild one arm without renumbering the
# others out from under their completed work.
ARMS = {'haiku-n3': {'passes': 3, 'perBatch': 40},
        'sonnet-n3': {'passes': 3, 'perBatch': 25},
        'sonnet-n1': {'passes': 1, 'perBatch': 25}}
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
    ap.add_argument('--arm', action='append', default=[], choices=sorted(ARMS),
                    help='rebuild only these arms (default: all)')
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
    wanted = args.arm or sorted(ARMS)
    built = {}
    for arm in wanted:
        spec = ARMS[arm]
        per = spec['perBatch']
        batches = [items[i:i + per] for i in range(0, len(items), per)]
        manifest = {'titles': len(items), 'batches': len(batches), 'perBatch': per,
                    'cases': len(chosen), 'half': 'dev', 'arm': arm,
                    'provenance': 'every record has hasWikiPlot == true; no TMDB overview prose',
                    'ids': keys}
        for p in range(1, spec['passes'] + 1):
            pdir = os.path.join(args.out_dir, arm, f'pass{p}')
            # Refuse to renumber a pass that already holds answers. Rewriting in/ at a
            # different batch size leaves out/batch-0003.json describing different titles
            # than in/batch-0003.json, and every coverage check then compares the wrong two
            # files — silent corruption rather than a failure.
            outdir = os.path.join(pdir, 'out')
            if os.path.isdir(outdir) and os.listdir(outdir):
                old = os.path.join(pdir, 'manifest.json')
                if os.path.exists(old):
                    with open(old, encoding='utf-8') as fh:
                        if json.load(fh).get('perBatch') != per:
                            sys.exit(f'{arm}/pass{p} already has outputs at a different batch '
                                     f'size — move them aside before rebuilding at {per}')
            os.makedirs(os.path.join(pdir, 'in'), exist_ok=True)
            os.makedirs(outdir, exist_ok=True)
            for i, b in enumerate(batches):
                with open(os.path.join(pdir, 'in', f'batch-{i:04d}.json'), 'w', encoding='utf-8') as fh:
                    # indent=1 so a batch fits one Read call; minified it exceeds the limit
                    # and every worker has to pretty-print a copy first.
                    json.dump(b, fh, ensure_ascii=False, indent=1)
            with open(os.path.join(pdir, 'manifest.json'), 'w', encoding='utf-8') as fh:
                json.dump(manifest, fh)
        built[arm] = {'batches': len(batches), 'perBatch': per, 'passes': spec['passes']}

    with open(os.path.join(args.out_dir, 'cases.json'), 'w', encoding='utf-8') as fh:
        json.dump({'cases': chosen}, fh)

    print(json.dumps({'arms': built, 'titles': len(items), 'cases': len(chosen),
                      'subagentCalls': sum(b['batches'] * b['passes'] for b in built.values())},
                     indent=2))


if __name__ == '__main__':
    main()
