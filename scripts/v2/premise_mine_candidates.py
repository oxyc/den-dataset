#!/usr/bin/env python3
"""Mine candidate pools for the premise ruler, and write the LLM batch inputs.

The premise ruler needs triplets (anchor, true-similar, hard-negative). An LLM cannot
propose a similar film out of 38,460 it has not seen, so candidates are mined here and the
LLM only *judges* — reading the plots, never any tag.

**What the mining must not do is grade v1's homework.** Candidates are proposed from TMDB
keyword overlap, which is a third-party signal independent of both the plot index and the
v1 premise tags, so neither index is favoured in which pairs get considered. The label
itself comes from the judge reading two plots.

Two candidate slots per anchor:
  - `near`  — top IDF-weighted keyword cosine. Where a true premise twin, if one exists in
              the corpus, is most likely to be.
  - `decoy` — same primary genre, era within +/-15 years, mid-band keyword similarity.
              Plausible enough that a naive ranker confuses it; the judge decides whether
              it actually shares a premise (often it does not, which is the point).

Sequels and remakes are excluded: they share a premise trivially and would make the ruler
measure franchise detection. The test is title-token overlap, since the enriched records
carry no `belongs_to_collection`.

Plots are excerpted (head + tail) to bound the judge's context; the excerpt policy is
recorded in the batch file so a re-run is comparable.
"""
import argparse
import json
import math
import os
import random
import re
import sys
from collections import defaultdict

ROOT = '/Users/cindy/Projects/Personal/den-dataset/out-t02'
V2 = os.path.join(ROOT, 'v2')
CORPUS = os.path.join(V2, 'corpus', 'wikiplot-corpus.jsonl')
ENRICHED = os.path.join(ROOT, 'enriched')
LABELS = os.path.join(ROOT, 'labels-t02.json')

HEAD_CHARS = 1400
TAIL_CHARS = 500
NEAR_N = 5
DECOY_N = 5
STOP_TITLE_TOKENS = {'the', 'a', 'an', 'of', 'and', 'in', 'to', 'part', 'ii', 'iii', 'iv', '2', '3', '4'}


def excerpt(plot):
    """Head + tail. Premise is established in the first act, but a third-act reveal is
    sometimes the premise itself (a twin-swap, a time loop closing), so the tail is kept."""
    if len(plot) <= HEAD_CHARS + TAIL_CHARS + 40:
        return plot
    return plot[:HEAD_CHARS].rstrip() + '\n[…]\n' + plot[-TAIL_CHARS:].lstrip()


def title_tokens(title):
    return {t for t in re.findall(r'[a-z0-9]+', (title or '').lower()) if t not in STOP_TITLE_TOKENS}


def same_franchise(a, b):
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / min(len(ta), len(tb)) >= 0.6


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--anchors', type=int, default=10000)
    ap.add_argument('--per-batch', type=int, default=20)
    ap.add_argument('--rng', type=int, default=20260904)
    ap.add_argument('--out-dir', default=os.path.join(V2, 'ruler', 'gen'))
    ap.add_argument('--provenance-only', action='store_true',
                    help='rewrite provenance.json without touching in/ — safe while workers read it')
    args = ap.parse_args()

    corpus = {}
    with open(CORPUS, encoding='utf-8') as fh:
        for line in fh:
            r = json.loads(line)
            corpus[r['key']] = r
    shipped = [k for k, r in corpus.items() if r['shipped']]

    # Keywords come from the enriched records; they are TMDB *ids*, never TMDB prose, so
    # nothing derived from `overview` on a no-plot title can leak in here.
    keywords = {}
    for name in sorted(os.listdir(ENRICHED)):
        if not (name.startswith('batch-') and name.endswith('.json')):
            continue
        with open(os.path.join(ENRICHED, name), encoding='utf-8') as fh:
            for rec in json.load(fh):
                if rec.get('hasWikiPlot') is not True:
                    continue
                keywords[f"{rec['mediaType']}:{rec['tmdbId']}"] = set(rec.get('keywordIDs') or [])

    with open(LABELS, encoding='utf-8') as fh:
        labels = {}
        for rec in json.load(fh)['records']:
            labels[f"{rec.get('mediaType', 'movie')}:{rec['tmdbId']}"] = rec

    # IDF over keyword ids: a keyword on 8,000 films says nothing, one on 40 says a lot.
    df = defaultdict(int)
    for k in shipped:
        for kw in keywords.get(k, ()):
            df[kw] += 1
    n = len(shipped)
    idf = {kw: math.log(n / (1 + c)) for kw, c in df.items()}

    inverted = defaultdict(list)
    for k in shipped:
        for kw in keywords.get(k, ()):
            if df[kw] <= 4000:            # a keyword this common is a genre label, not a hook
                inverted[kw].append(k)

    norm = {}
    for k in shipped:
        kws = [kw for kw in keywords.get(k, ()) if kw in idf]
        norm[k] = math.sqrt(sum(idf[kw] ** 2 for kw in kws)) or 1.0

    rng = random.Random(args.rng)
    # Stratify anchors by primary genre so the ruler is not 40% Drama, and require enough
    # keywords for the mining to have anything to work with.
    by_genre = defaultdict(list)
    for k in shipped:
        if len(keywords.get(k, ())) < 4:
            continue
        by_genre[labels.get(k, {}).get('primaryGenre', '?')].append(k)
    for g in by_genre:
        by_genre[g].sort()
        rng.shuffle(by_genre[g])

    genres = sorted(by_genre)
    anchors = []
    idx = {g: 0 for g in genres}
    while len(anchors) < args.anchors:
        progressed = False
        for g in genres:
            if idx[g] < len(by_genre[g]) and len(anchors) < args.anchors:
                anchors.append(by_genre[g][idx[g]])
                idx[g] += 1
                progressed = True
        if not progressed:
            break
    print(f'anchors: {len(anchors)} over {len(genres)} primary genres', flush=True)

    def score_candidates(k):
        kws = [kw for kw in keywords.get(k, ()) if kw in idf and df[kw] <= 4000]
        acc = defaultdict(float)
        for kw in kws:
            w = idf[kw] ** 2
            for other in inverted[kw]:
                if other != k:
                    acc[other] += w
        for other in acc:
            acc[other] /= (norm[k] * norm[other])
        return acc

    os.makedirs(args.out_dir, exist_ok=True)
    batches = []
    current = []
    dropped = 0
    provenance = {}
    for k in anchors:
        rec = corpus[k]
        lab = labels.get(k, {})
        acc = score_candidates(k)
        if not acc:
            dropped += 1
            continue
        ranked = sorted(acc.items(), key=lambda kv: -kv[1])
        ranked = [(o, s) for o, s in ranked if not same_franchise(rec['title'], corpus[o]['title'])]
        near = [o for o, _ in ranked[:NEAR_N]]

        genre = lab.get('primaryGenre')
        year = rec.get('year') or 0
        mid = [o for o, s in ranked[NEAR_N:NEAR_N + 400]
               if labels.get(o, {}).get('primaryGenre') == genre
               and abs((corpus[o].get('year') or 0) - year) <= 15]
        rng.shuffle(mid)
        decoy = mid[:DECOY_N]
        if len(decoy) < DECOY_N:
            pool = [o for o, s in ranked[NEAR_N:NEAR_N + 400] if o not in near and o not in decoy]
            rng.shuffle(pool)
            decoy += pool[:DECOY_N - len(decoy)]

        cands = near + decoy
        if len(cands) < 6:
            dropped += 1
            continue
        rng.shuffle(cands)

        # Which slot proposed each candidate. Not shown to the judge — it is only used
        # afterwards, to report triplet accuracy split by slot. `near` candidates are mined
        # by keyword similarity, which correlates with plot-surface similarity (measured:
        # they lift plot-index similarity 1.23 sd above random against the premise index's
        # 0.98 sd), so a result that holds only on `near` positives is partly an artefact of
        # the mining rather than a property of the index.
        provenance[k] = {'near': list(near), 'decoy': list(decoy)}

        current.append({
            'anchor': {'key': k, 'title': rec['title'], 'year': rec.get('year'),
                       'plot': excerpt(rec['plot'])},
            'candidates': [{'key': o, 'title': corpus[o]['title'], 'year': corpus[o].get('year'),
                            'plot': excerpt(corpus[o]['plot'])} for o in cands],
        })
        if len(current) == args.per_batch:
            batches.append(current)
            current = []
    if current:
        batches.append(current)

    manifest = {
        'anchors': sum(len(b) for b in batches),
        'dropped': dropped,
        'batches': len(batches),
        'perBatch': args.per_batch,
        'excerpt': {'headChars': HEAD_CHARS, 'tailChars': TAIL_CHARS},
        'nearN': NEAR_N, 'decoyN': DECOY_N, 'rng': args.rng,
        'candidateSource': 'IDF-weighted TMDB keyword-id cosine (df<=4000), franchise-filtered by title tokens',
    }
    with open(os.path.join(args.out_dir, 'provenance.json'), 'w', encoding='utf-8') as fh:
        json.dump(provenance, fh)
    if args.provenance_only:
        print(json.dumps({'provenanceAnchors': len(provenance),
                          'note': 'in/ and manifest.json left untouched'}, indent=2))
        return

    for i, b in enumerate(batches):
        with open(os.path.join(args.out_dir, f'batch-{i:04d}.json'), 'w', encoding='utf-8') as fh:
            # indent=1, not minified. A 340 KB batch on one line exceeds the Read tool's
            # per-call limit, so every worker had to shell out to `jq` to pretty-print a copy
            # before it could read its own input — a wasted round-trip on every batch. At
            # indent=1 the same batch is ~1,400 lines and reads in a single call.
            json.dump(b, fh, ensure_ascii=False, indent=1)
    with open(os.path.join(args.out_dir, 'manifest.json'), 'w', encoding='utf-8') as fh:
        json.dump({**manifest, 'anchorKeys': [a['anchor']['key'] for b in batches for a in b]}, fh)
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
