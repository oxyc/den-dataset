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
import sys

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
    # 40 is at the edge, and which side of it you land on depends on the MODEL.
    #
    # A 40-title batch emits 40 x ~15 tags, each with a salience and a kind. Haiku fits that
    # inside the 64,000-token output cap comfortably; Sonnet, which writes more per tag and
    # narrates more around it, exceeded the cap on a bake-off batch and wrote nothing at all.
    # So the safe batch size is not a property of the task, it is a property of the model
    # running it: keep 40 for Haiku, drop to ~25 for Sonnet, and remember that an over-large
    # batch does not degrade — it produces an empty file.
    ap.add_argument('--per-batch', type=int, default=40)
    ap.add_argument('--passes', type=int, default=3)
    ap.add_argument('--scope', choices=['all', 'shipped', 'ruler', 'uncovered'], default='all')
    ap.add_argument('--triplets', default=os.path.join(V2, 'ruler', 'triplets-final.json'))
    ap.add_argument('--out-dir', default=os.path.join(V2, 'tags-v2'))
    args = ap.parse_args()

    # `ruler` scope: only the titles the premise ruler actually grades. Scoring a triplet
    # needs a vector for all three of its titles in the arm's own tag space, so a v2 index
    # covering a random 10% of the corpus would grade almost no complete triplet (0.1% of
    # them) and answer nothing. Tagging the ruler's own titles buys 100% triplet coverage
    # for a fraction of the run, which is what makes a v1-vs-v2 verdict reachable at all.
    # It is NOT a shippable index — that still needs the full 38,460 — and the percentile
    # metrics must then be computed against a matched subset for both arms, since a smaller
    # distractor pool flatters every rank.
    # `uncovered` scope: the 219 shipped titles that have a Wikipedia plot but no premise row,
    # so More Like This silently serves them the plot-only fallback. Among them are The Dark
    # Knight, Fight Club and The Godfather — 3% of the top 100 by vote count, which is to say
    # the gap is concentrated on exactly the titles people open.
    #
    # This is the one v2 tagging run the evidence supports. It is not a v1-vs-v2 comparison:
    # these titles have nothing to compare against, and the alternative on screen today is the
    # plot index, which both halves of the ruler put ~11 pp behind premise. Measured cost of
    # the style mismatch: a title's v2 row sits at cosine 0.764 from its own v1 row, closer
    # than the mean nearest within-v1 neighbour at 0.739, so a v2-style row lands in the right
    # neighbourhood of a v1-style index — though for 24% of titles some other film's row is
    # nearer than its own, so this improves coverage rather than being seamless.
    wanted = None          # an explicit id-set every one of whose members must be tagged
    already_covered = None  # keys to EXCLUDE, which is a filter rather than a required set
    if args.scope == 'uncovered':
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from index_io import load_premise_v1_index
        already_covered = set(load_premise_v1_index().keys)
    elif args.scope == 'ruler':
        with open(args.triplets, encoding='utf-8') as fh:
            wanted = set()
            for t in json.load(fh)['triplets']:
                wanted.update((t['anchor'], t['positive'], t['negative']))

    rows = []
    with open(CORPUS, encoding='utf-8') as fh:
        for line in fh:
            r = json.loads(line)
            if args.scope in ('shipped', 'uncovered') and not r['shipped']:
                continue
            if already_covered is not None and r['key'] in already_covered:
                continue
            if wanted is not None and r['key'] not in wanted:
                continue
            rows.append(r)
    if wanted is not None and len(rows) != len(wanted):
        sys.exit(f'{len(wanted) - len(rows)} ruler titles are not in the wiki-plot corpus — '
                 'a title without a wiki plot must never be tagged')
    if not rows:
        sys.exit(f'scope {args.scope!r} selected no titles')
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
                # indent=1 so a batch fits one Read call; minified it exceeds the limit
                # and every worker has to pretty-print a copy first.
                json.dump(b, fh, ensure_ascii=False, indent=1)
        with open(os.path.join(pdir, 'manifest.json'), 'w', encoding='utf-8') as fh:
            json.dump(manifest, fh)

    print(json.dumps({k: v for k, v in manifest.items() if k != 'ids'}, indent=2))


if __name__ == '__main__':
    main()
