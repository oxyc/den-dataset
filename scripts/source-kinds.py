#!/usr/bin/env python3
"""What KIND of work each `basedOn` (P144) target is — the fact that turns an adaptation link into a row.

The facts sidecar records P144 as a bare Q-id: "this film is based on Q1234". That is enough to link
adaptations of one source to each other, but not to answer "show me films based on books", because nothing
says whether Q1234 is a novel, a manga, a video game or another film. This fetches P31 (instance of) for
every distinct source work and folds it into a small closed vocabulary.

Closed, not free-form, for the reason the taxonomy work already established: Wikidata's own types are far
finer than a browse row can use (`novel`, `novella`, `epistolary novel`, `roman à clef` are all "a book"),
and an open vocabulary cannot be rendered as a stable shelf.

    scripts/source-kinds.py <facts.json> <out.json> [--limit N]

Output: {"kinds": {"<qid>": "book", …}, "titleKinds": {"movie:603": ["book"], …}, "counts": {…}}
"""
import collections
import json
import sys
import time
import urllib.parse
import urllib.request

UA = 'den-dataset/1.0 (github.com/oxyc/den-dataset)'
ENDPOINT = 'https://query.wikidata.org/sparql'

# Wikidata P31 label → our vocabulary. Matched on the lowercased label, exact first then by suffix, so
# "epistolary novel" and "novel series" both reach `book` without listing every variant.
EXACT = {
    'literary work': 'book', 'written work': 'book', 'novel': 'book', 'novella': 'book',
    'short story': 'book', 'book': 'book', 'non-fiction book': 'book', 'autobiography': 'book',
    'memoir': 'book', 'biography': 'book', 'fairy tale': 'book', 'poem': 'book', 'anthology': 'book',
    'short story collection': 'book', 'children\'s literature': 'book', 'fable': 'book',
    'comic book series': 'comic', 'manga series': 'comic', 'graphic novel': 'comic', 'manga': 'comic',
    'comic strip': 'comic', 'comic book': 'comic', 'webtoon': 'comic', 'light novel': 'book',
    'dramatic work': 'play', 'play': 'play', 'musical': 'play', 'dramatico-musical work': 'play',
    'opera': 'play', 'theatrical production': 'play',
    'video game': 'game', 'video game series': 'game',
    'film': 'screen', 'television series': 'screen', 'animated television series': 'screen',
    'film series': 'screen', 'limited series': 'screen', 'anime television series': 'screen',
    'television film': 'screen', 'short film': 'screen', 'animated film': 'screen',
    'media franchise': 'franchise', 'brand': 'franchise',
    'song': 'music', 'single': 'music', 'album': 'music',
}
# A source that is a PERSON or a CHARACTER is not an adaptation of a text. "Based on Batman" is a different
# claim from "based on a novel", and folding them together would put superhero films in a books row.
CHARACTERISH = ('character', 'fictional human', 'superhero team', 'human', 'mutate', 'fictional')


def classify(label):
    low = label.strip().lower()
    if low in EXACT:
        return EXACT[low]
    if any(marker in low for marker in CHARACTERISH):
        return 'character'
    # Suffix rules catch the long tail Wikidata keeps inventing: "serial novel", "mystery novel".
    for suffix, kind in (('novel', 'book'), ('literature', 'book'), ('manga', 'comic'),
                         ('comics', 'comic'), ('video game', 'game'), ('play', 'play')):
        if low.endswith(suffix):
            return kind
    return 'other'


def sparql(qids, attempts=8):
    values = ' '.join(f'wd:{q}' for q in qids)
    query = (f'SELECT ?w ?tLabel WHERE {{ VALUES ?w {{ {values} }} ?w wdt:P31 ?t . '
             'SERVICE wikibase:label { bd:serviceParam wikibase:language "en". } }')
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(
                ENDPOINT, data=urllib.parse.urlencode({'query': query, 'format': 'json'}).encode(),
                headers={'User-Agent': UA, 'Accept': 'application/sparql-results+json'})
            return json.load(urllib.request.urlopen(request, timeout=180))['results']['bindings']
        except Exception as error:  # noqa: BLE001 — WDQS throttles hard; the retry IS the handling
            if attempt == attempts - 1:
                print(f'  giving up on a batch: {error}', file=sys.stderr)
                return []
            time.sleep(30 * (attempt + 1))
    return []


def main():
    facts_path, out_path = sys.argv[1], sys.argv[2]
    limit = int(sys.argv[sys.argv.index('--limit') + 1]) if '--limit' in sys.argv else None

    facts = json.load(open(facts_path, encoding='utf-8'))
    records = facts['records'] if isinstance(facts, dict) else facts

    sources = collections.Counter()
    for record in records:
        for qid in (record.get('basedOn') or []):
            sources[qid] += 1
    ids = [q for q, _ in sources.most_common()]
    if limit:
        ids = ids[:limit]
    print(f'{len(ids)} distinct source works to type', file=sys.stderr)

    # A work can be several things at once ("literary work" AND "novel"); keep the strongest signal by
    # preferring a real kind over `other`, and a text over a character.
    priority = ['book', 'comic', 'play', 'game', 'screen', 'music', 'franchise', 'character', 'other']
    best = {}
    for start in range(0, len(ids), 120):
        for row in sparql(ids[start:start + 120]):
            qid = row['w']['value'].rsplit('/', 1)[-1]
            kind = classify(row['tLabel']['value'])
            if qid not in best or priority.index(kind) < priority.index(best[qid]):
                best[qid] = kind
        done = min(start + 120, len(ids))
        print(f'  {done}/{len(ids)}…', file=sys.stderr)
        time.sleep(1.5)

    title_kinds = {}
    for record in records:
        kinds = sorted({best[q] for q in (record.get('basedOn') or []) if q in best})
        if kinds:
            title_kinds[f"{record['mediaType']}:{record['tmdbId']}"] = kinds

    counts = collections.Counter(k for kinds in title_kinds.values() for k in kinds)
    payload = {'kinds': best, 'titleKinds': title_kinds, 'counts': dict(counts.most_common())}
    with open(out_path, 'w', encoding='utf-8') as fh:
        json.dump(payload, fh)
    print(json.dumps({'sourceWorks': len(best), 'titles': len(title_kinds),
                      'counts': dict(counts.most_common())}))


if __name__ == '__main__':
    main()
