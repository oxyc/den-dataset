#!/usr/bin/env python3
"""Assemble the wiki-plot corpus that every v2 LLM phase draws from.

Only titles with hasWikiPlot==true belong here. The other enriched rows carry TMDB
prose in `overview`, and feeding that to an LLM is barred by TMDb §1.C, so the flag is
asserted per record on the way in rather than trusted downstream.

Keys are "movie:123" / "tv:123": the two TMDB id namespaces overlap (tv 95 is Buffy,
movie 95 is Armageddon), and a bare id has already silently dropped 940 series once.

Writes:
  v2/corpus/wikiplot-corpus.jsonl  one {key, tmdbId, mediaType, title, year, plot, plotSHA, ...} per line
  v2/corpus/corpus-ids.json        the id-set every coverage check is made against
"""
import hashlib
import json
import os
import sys

ENRICHED = '/Users/cindy/Projects/Personal/den-dataset/out-t02/enriched'
LABELS = '/Users/cindy/Projects/Personal/den-dataset/out-t02/labels-t02.json'
OUT_DIR = '/Users/cindy/Projects/Personal/den-dataset/out-t02/v2/corpus'

EXPECTED_ENRICHED = 57715
EXPECTED_WIKI = 38460


def key(media, tmdb_id):
    return f"{media}:{tmdb_id}"


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    files = sorted(
        os.path.join(ENRICHED, f)
        for f in os.listdir(ENRICHED)
        if f.startswith('batch-') and f.endswith('.json')
    )
    if not files:
        sys.exit(f"no enriched batches under {ENRICHED}")

    rows = {}
    total = 0
    for path in files:
        with open(path, encoding='utf-8') as fh:
            batch = json.load(fh)
        for rec in batch:
            total += 1
            if rec.get('hasWikiPlot') is not True:
                continue
            media = rec['mediaType']
            tmdb_id = rec['tmdbId']
            plot = rec.get('overview') or ''
            if not plot.strip():
                sys.exit(f"{key(media, tmdb_id)} claims hasWikiPlot but has an empty plot")
            k = key(media, tmdb_id)
            if k in rows:
                sys.exit(f"duplicate corpus key {k} — the enriched batches are not disjoint")
            rows[k] = {
                'key': k,
                'tmdbId': tmdb_id,
                'mediaType': media,
                'title': rec.get('title'),
                'year': rec.get('year'),
                'voteCount': rec.get('voteCount'),
                'genres': rec.get('genres') or [],
                'originalLanguage': rec.get('originalLanguage'),
                'plot': plot,
                'plotChars': len(plot),
                'plotSHA': hashlib.sha256(plot.encode('utf-8')).hexdigest()[:16],
            }

    if total != EXPECTED_ENRICHED:
        sys.exit(f"enriched record count {total} != documented {EXPECTED_ENRICHED}")
    if len(rows) != EXPECTED_WIKI:
        sys.exit(f"wiki-plot count {len(rows)} != documented {EXPECTED_WIKI}")

    # Which of these actually shipped in the t02 index. Shipped titles are the ones the app
    # can retrieve, so the eval and the v2 index are both scoped to them; the rest are kept
    # in the corpus so a later ship-scope change does not need a re-run.
    with open(LABELS, encoding='utf-8') as fh:
        labels = json.load(fh)
    records = labels['records'] if isinstance(labels, dict) else labels
    shipped = set()
    for rec in records:
        shipped.add(key(rec.get('mediaType', 'movie'), rec['tmdbId']))

    ordered = sorted(rows, key=lambda k: (rows[k]['mediaType'], rows[k]['tmdbId']))
    with open(os.path.join(OUT_DIR, 'wikiplot-corpus.jsonl'), 'w', encoding='utf-8') as fh:
        for k in ordered:
            rows[k]['shipped'] = k in shipped
            fh.write(json.dumps(rows[k], ensure_ascii=False) + '\n')

    shipped_in_corpus = [k for k in ordered if rows[k]['shipped']]
    with open(os.path.join(OUT_DIR, 'corpus-ids.json'), 'w', encoding='utf-8') as fh:
        json.dump({
            'all': ordered,
            'shipped': shipped_in_corpus,
            'counts': {
                'enriched': total,
                'wikiPlot': len(rows),
                'wikiPlotMovies': sum(1 for k in ordered if rows[k]['mediaType'] == 'movie'),
                'wikiPlotSeries': sum(1 for k in ordered if rows[k]['mediaType'] == 'tv'),
                'shippedLabels': len(shipped),
                'shippedAndWikiPlot': len(shipped_in_corpus),
            },
        }, fh, indent=2)

    print(json.dumps({
        'enriched': total,
        'wikiPlot': len(rows),
        'shippedLabels': len(shipped),
        'shippedAndWikiPlot': len(shipped_in_corpus),
        'shippedNotInWikiCorpus': len(shipped - set(ordered)),
    }, indent=2))


if __name__ == '__main__':
    main()
