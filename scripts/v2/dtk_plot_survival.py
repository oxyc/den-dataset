#!/usr/bin/env python3
"""DT-K's spending gate: how many of the ~2,960 missing series would survive `requireWikiPlot`?

DT-K says to decide the no-plot policy for series BEFORE spending anything, and its current
estimate — ~32% survive, ~950 titles — rests on 22 and 8 hand-checked articles. The ticket
itself says to re-measure over the real worklist first. This does that.

Method, and every step is chosen to match what the pipeline would actually do rather than to
be convenient:

1. The eligible universe comes from TMDB `discover/tv` at the pipeline's own vote floor
   (`vote_count >= 50`), not from a list of interesting shows.
2. The gap is that universe minus the series already in `labels-t02.json`.
3. A random sample of the gap is resolved to enwiki through **Wikidata P4983**, the same
   property `WikipediaSource.wikidata(forTMDBIds:mediaType:)` uses. Title matching is not used
   at all: it would silently succeed on the wrong article for every remake and reboot, which
   is exactly the population TV is full of.
4. A title counts as surviving only if its article has a section whose heading is in
   `WikipediaSource.plotSectionNames`, copied verbatim below. Anything looser measures a
   different gate than the one that will run.

Reported with a Wilson interval, because the decision is "is this worth the spend" and a point
estimate from a few hundred draws cannot answer that on its own.

Read-only. Hits TMDB, Wikidata and enwiki; writes one JSON report.
"""
import argparse
import json
import math
import os
import random
import sys
import time
import urllib.parse
import urllib.request

ROOT = '/Users/cindy/Projects/Personal/den-dataset'
UA = 'den-dataset/DT-K-survey (https://github.com/generoi; contact via repo)'

# Copied from Sources/DenDataset/WikipediaSource.swift. If that list changes and this one does
# not, this script measures a gate that no longer exists — so it is asserted, not trusted.
PLOT_SECTIONS = ['plot', 'plot summary', 'synopsis', 'storyline', 'premise', 'story', 'summary']


def assert_sections_match_swift():
    path = os.path.join(ROOT, 'Sources', 'DenDataset', 'WikipediaSource.swift')
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            if 'plotSectionNames' in line and '[' in line:
                names = [s.strip().strip('"') for s in
                         line[line.index('[') + 1:line.rindex(']')].split(',')]
                if names != PLOT_SECTIONS:
                    sys.exit(f'plot section list has drifted from WikipediaSource.swift:\n'
                             f'  swift:  {names}\n  here:   {PLOT_SECTIONS}')
                return
    sys.exit('could not find plotSectionNames in WikipediaSource.swift')


def get_json(url, data=None, headers=None, retries=4):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data,
                                         headers={'User-Agent': UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except Exception as exc:                       # noqa: BLE001 — retry anything transient
            if attempt == retries - 1:
                raise SystemExit(f'request failed after {retries} tries: {url}\n  {exc}')
            time.sleep(2 ** attempt)


def tmdb_key():
    with open(os.path.join(ROOT, 'den.env'), encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if line.startswith('TMDB_API_KEY='):
                return line.split('=', 1)[1].strip().strip('"').strip("'")
    sys.exit('no TMDB_API_KEY in den.env')


def eligible_series(key, vote_floor, max_pages):
    """The universe at the pipeline's vote floor, newest-popular first.

    TMDB caps `discover` at 500 pages, so a full enumeration of a 6,800-title universe is
    reachable; max_pages exists to bound a survey run, not because the API cannot go further.
    """
    ids = []
    page = 1
    while page <= max_pages:
        url = ('https://api.themoviedb.org/3/discover/tv'
               f'?api_key={key}&vote_count.gte={vote_floor}&page={page}'
               '&sort_by=popularity.desc&include_adult=false')
        body = get_json(url)
        for r in body.get('results', []):
            ids.append(r['id'])
        total = body.get('total_pages', 1)
        if page >= total:
            break
        page += 1
        time.sleep(0.05)
    return ids, total


def shipped_series():
    with open(os.path.join(ROOT, 'out-t02', 'labels-t02.json'), encoding='utf-8') as fh:
        return {r['tmdbId'] for r in json.load(fh)['records'] if r.get('mediaType') == 'tv'}


def wikidata_articles(tmdb_ids):
    """TMDB series id -> enwiki article title, via P4983. One POST per chunk."""
    out = {}
    for start in range(0, len(tmdb_ids), 200):
        chunk = tmdb_ids[start:start + 200]
        values = ' '.join(f'"{i}"' for i in chunk)
        query = f'''
SELECT ?tmdb ?article WHERE {{
  VALUES ?tmdb {{ {values} }}
  ?item wdt:P4983 ?tmdb .
  ?article schema:about ?item ;
           schema:isPartOf <https://en.wikipedia.org/> .
}}'''
        body = get_json('https://query.wikidata.org/sparql',
                        data=urllib.parse.urlencode({'query': query}).encode(),
                        headers={'Accept': 'application/sparql-results+json',
                                 'Content-Type': 'application/x-www-form-urlencoded'})
        for row in body['results']['bindings']:
            title = urllib.parse.unquote(row['article']['value'].rsplit('/', 1)[-1])
            out[int(row['tmdb']['value'])] = title.replace('_', ' ')
        time.sleep(1.0)                                # be a good citizen on WDQS
    return out


def has_plot_section(article):
    url = ('https://en.wikipedia.org/w/api.php?action=parse&prop=sections&format=json'
           f'&formatversion=2&page={urllib.parse.quote(article)}')
    body = get_json(url)
    if 'parse' not in body:
        return None                                    # article vanished or is a redirect loop
    for s in body['parse'].get('sections', []):
        if s.get('line', '').strip().lower() in PLOT_SECTIONS:
            return True
    return False


def wilson(k, n, z=1.96):
    if not n:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    halfwidth = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(max(0.0, centre - halfwidth), 4), round(min(1.0, centre + halfwidth), 4))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--vote-floor', type=int, default=50)
    ap.add_argument('--max-pages', type=int, default=400)
    ap.add_argument('--sample', type=int, default=300)
    ap.add_argument('--seed', type=int, default=20260905)
    ap.add_argument('--out', default=os.path.join(ROOT, 'out-t02', 'v2',
                                                  'dtk-plot-survival.json'))
    args = ap.parse_args()

    assert_sections_match_swift()
    key = tmdb_key()

    universe, total_pages = eligible_series(key, args.vote_floor, args.max_pages)
    universe = sorted(set(universe))
    have = shipped_series()
    gap = sorted(set(universe) - have)
    print(f'universe {len(universe)} (over {total_pages} pages) · shipped {len(have)} · '
          f'gap {len(gap)}', flush=True)

    rng = random.Random(args.seed)
    sample = rng.sample(gap, min(args.sample, len(gap)))

    articles = wikidata_articles(sample)
    print(f'{len(articles)}/{len(sample)} sampled series have an enwiki article', flush=True)

    survived, checked, failed = 0, 0, 0
    for i, (tid, article) in enumerate(sorted(articles.items())):
        got = has_plot_section(article)
        if got is None:
            failed += 1
        else:
            checked += 1
            survived += got
        if i % 25 == 0:
            print(f'  {i}/{len(articles)} checked', flush=True)
        time.sleep(0.05)

    n = len(sample)
    # The rate that matters is over the SAMPLE, not over the titles that happened to have an
    # article: a series with no enwiki article is a series the gate drops, and scoring only
    # the ones with articles would report the conditional rate as if it were the survival rate.
    lo, hi = wilson(survived, n)
    report = {
        'voteFloor': args.vote_floor,
        'universe': len(universe), 'shippedSeries': len(have), 'gap': len(gap),
        'sample': n, 'withEnwikiArticle': len(articles),
        'articleRate': round(len(articles) / n, 4),
        'withPlotSection': survived,
        'sectionLookupFailed': failed,
        'survivalRate': round(survived / n, 4),
        'survivalRate95CI': [lo, hi],
        'projectedSurvivors': [int(lo * len(gap)), int(hi * len(gap))],
        'ticketEstimate': {'survivalRate': 0.32, 'projected': 950,
                           'basedOnArticlesChecked': 22},
        'plotSections': PLOT_SECTIONS,
        'seed': args.seed,
    }
    with open(args.out, 'w', encoding='utf-8') as fh:
        json.dump(report, fh, indent=2)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
