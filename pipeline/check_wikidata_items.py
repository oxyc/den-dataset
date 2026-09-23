#!/usr/bin/env python3
"""The publisher's gate on titles whose TMDB id several Wikidata items claim, with nothing chosen.

Every per-title Wikidata query answers from ONE item (`lib/wikidata.resolve`). Where no rule singles one
out and `data/wikidata-item-decisions.json` names none, the title is written with no Wikidata fields rather
than two works' fields mixed — so it ships with no card. That is the right failure for one build and the
wrong one to publish: Total Drama and Charité were among the first nine. The store build records the
titles as `wikidataItems.ambiguous` in the manifest, and this refuses while that list is not empty.

    pipeline/check_wikidata_items.py --gate out/dataset.meta.json
"""
import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DECISIONS = os.path.join(REPO, "data", "wikidata-item-decisions.json")


def gate(meta_path, decisions_path=DECISIONS):
    """0 passes, 1 refuses."""
    with open(meta_path, encoding="utf-8") as fh:
        meta = json.load(fh)
    decisions = os.path.relpath(decisions_path)
    rebuild = f"pipeline/build_store.py … --stamp-meta {meta_path}"
    record = meta.get("wikidataItems")
    if not (isinstance(record, dict) and isinstance(record.get("ambiguous"), list)):
        print(f"error: the manifest records no wikidataItems, so nothing says how many titles the store ships "
              f"with no Wikidata item chosen. Rebuild the store with {rebuild}.", file=sys.stderr)
        return 1
    if record["ambiguous"]:
        shown = ", ".join(record["ambiguous"][:20]) + (" …" if len(record["ambiguous"]) > 20 else "")
        print(f"error: {len(record['ambiguous'])} title(s) ship with no Wikidata fields and no card: several "
              f"items claim each one's TMDB id and nothing chose between them: {shown}\n"
              f"       To decide them:\n"
              f"         1. look at each claimant on Wikidata against TMDB's own record of the title (name,\n"
              f"            dates, seasons, episodes, IMDb id)\n"
              f"         2. add {{mediaType, tmdbId, item, why, upstream}} for each to {decisions}\n"
              f"         3. re-run `./den stage facts` (it re-scrapes those titles) and rebuild the store with\n"
              f"            {rebuild}", file=sys.stderr)
        return 1
    print("wikidata item gate: every contested title has its item chosen")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gate", metavar="META", required=True,
                    help="refuse this manifest's store while it ships a title with no Wikidata item chosen")
    ap.add_argument("--decisions", default=DECISIONS)
    args = ap.parse_args()
    return gate(args.gate, args.decisions)


if __name__ == "__main__":
    sys.exit(main())
