#!/usr/bin/env python3
"""Build the small adversarial re-pilot that guards v2 applicability boundaries."""
import argparse
import hashlib
import json
import re


REQUIRED = {
    ("movie", 14064),   # Ten Canoes — framed control
    ("movie", 234284),  # Time of EVE: The Movie — article covers ONA + compilation
    ("movie", 324560),  # Brimstone — nonlinear control
    ("movie", 354072),  # Our Times — framed control
    ("movie", 645757),  # That Christmas — intersecting stories, not anthology
    ("tv", 4573),       # Late Night with Conan O'Brien — no narrative/archetype
    ("tv", 60726),      # Forever — one immortal is not multi-generational
    ("tv", 61986),      # Bloodline — nonlinear, slow-burn control
    ("tv", 80563),      # How Not to Summon a Demon Lord — light-novel source page
    ("tv", 94734),      # Perfect Life — parallel-strands control
    ("tv", 96316),      # Rent-a-Girlfriend — manga source page
    ("tv", 97062),      # Killer Inside — documentary has no story archetype
}
NON_NARRATIVE = re.compile(
    r"\b(documentary|talk show|reality (?:television|tv)|game show|news program|variety show|anthology)\b",
    re.I,
)


def key(row):
    return row["mediaType"], row["tmdbId"]


ap = argparse.ArgumentParser()
ap.add_argument("--articles", required=True, help="the 230-title pilot article JSONL")
ap.add_argument("--facets", required=True, help="the completed pilot facet JSONL")
ap.add_argument("--out", required=True)
ap.add_argument("--controls", type=int, default=12)
args = ap.parse_args()

with open(args.articles, encoding="utf-8") as fh:
    articles = {key(row): row for row in map(json.loads, fh)}
with open(args.facets, encoding="utf-8") as fh:
    facets = {key(row): row for row in map(json.loads, fh)}
missing = REQUIRED - set(articles)
if missing:
    raise SystemExit(f"pilot input lacks required adversarial keys: {sorted(missing)}")

selected = set(REQUIRED)
for item, article in articles.items():
    lead = article["text"].split("\n", 1)[0]
    if NON_NARRATIVE.search(lead):
        selected.add(item)

candidates = [item for item in articles if item not in selected
              and facets[item]["facets"]["archetype"]["value"] != "does-not-apply"]
candidates.sort(key=lambda item: hashlib.sha256(f"facet-adversarial:{item[0]}:{item[1]}".encode()).digest())
selected.update(candidates[:args.controls])

with open(args.out, "w", encoding="utf-8") as fh:
    for item, article in articles.items():
        if item in selected:
            fh.write(json.dumps(article, ensure_ascii=False) + "\n")

print(json.dumps({"out": args.out, "titles": len(selected), "required": len(REQUIRED),
                  "controls": args.controls}, indent=2))
