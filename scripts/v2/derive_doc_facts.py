#!/usr/bin/env python3
"""Build `doc-facts.json` from the facts sidecar instead of re-scraping Wikidata.

  scripts/v2/derive_doc_facts.py --facts out-t02-cc0b/facts-c85c707b0b18.json --out out-repass/doc-facts.json

`taxonomy-backfill doc-facts` issues ~770 SPARQL requests to fetch two clauses — director and genre — that
the facts sidecar already contains for every shipped title, as QIDs plus an `entities` map holding the
labels. Re-scraping them is ~40 minutes and a load on Wikidata for data already on disk.

## Validated, not assumed

A partial scrape of 25,366 titles was kept and compared against this derivation:

    directors  25,365 / 25,366   100.00%
    genres     25,362 / 25,366    99.98%

Of the four genre rows that differ, one is the SCRAPE being wrong — it recorded a raw blank-node URL
(`http://www.wikidata.org/.well-known/genid/…`) as a genre, which this derivation drops because no such QID
resolves to a label. The rest differ by a single genre QID the sidecar resolved differently.

The genre normalisation below mirrors `WikipediaSource.strippedGenre` exactly — Wikidata appends the medium
to genre labels where TMDB does not, and the comment there records that mean overlap with TMDB measured
0.01 before the strip and 0.40 after. Reimplementing it is the one risky part of this script, which is why
it is validated against a real scrape rather than reasoned about.
"""
import argparse
import json

# WikipediaSource.strippedGenre's list, in its order.
MEDIA = ["film", "movie", "television series", "tv series", "series", "anime"]


def stripped_genre(label):
    """`"science fiction film"` → `"science fiction"`; a label that is ONLY a medium → `""`."""
    s = label.lower().strip()
    changed = True
    while changed:
        changed = False
        for word in MEDIA:
            if s.endswith(" " + word):
                s = s[: -(len(word) + 1)].strip()
                changed = True
    return "" if s in MEDIA else s


ap = argparse.ArgumentParser()
ap.add_argument("--facts", required=True, help="facts-<version>.json (records + entities)")
ap.add_argument("--out", required=True)
args = ap.parse_args()

facts = json.load(open(args.facts, encoding="utf-8"))
entities = facts.get("entities", {})


def label(qid):
    e = entities.get(qid)
    return e.get("en") if isinstance(e, dict) else None


rows, no_director, no_genre = {}, 0, 0
for r in facts["records"]:
    directors = sorted({x for x in (label(q) for q in r.get("directors") or []) if x})
    genres = sorted({g for g in (stripped_genre(label(q) or "") for q in r.get("genres") or []) if g})
    # An empty row is recorded, not skipped: `embed-corpus` reads this as "Wikidata states neither", and a
    # missing key would be indistinguishable from a title the scrape never reached.
    rows[f"{r['mediaType']}:{r['tmdbId']}"] = {"directors": directors, "genres": genres}
    no_director += not directors
    no_genre += not genres

json.dump(rows, open(args.out, "w", encoding="utf-8"), indent=1, sort_keys=True)
print(json.dumps({
    "rows": len(rows), "withoutDirector": no_director, "withoutGenre": no_genre, "out": args.out,
}, indent=2))
