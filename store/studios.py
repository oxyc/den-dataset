"""Iconic studios: the curated list in `data/iconic-studios.json`, written beside the `companies` column.

den-spec `wire/store-v2.md` § "Iconic studios". `companies` already says which production companies each
title credits; these sections say which of those companies are a taste in themselves (oxyc/den#132), so a
reader links them and gives them a row with no second source to fetch.

One studio is often several Wikidata items: a TV arm, a renamed item, a duplicate. The file groups them
under the studio's own item, and the store keeps the grouping, so a reader counts Toho Animation's titles
as Toho's.

Only studios the corpus credits are written. A studio in the file that no title credits is left out, not
refused: the file is a judgement about the world, and a small corpus (a test fixture, a partial run)
legitimately credits none of it.
"""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CURATED = os.path.join(REPO, "data", "iconic-studios.json")


def _qid(value, where):
    if not (isinstance(value, str) and value.startswith("Q") and value[1:].isdigit()):
        sys.exit(f"{where}: {value!r} is not a Wikidata item id")
    return int(value[1:])


def load(path=CURATED):
    """`[(qid, name, [qid, …])]` from the curated file, refusing a malformed one.

    A missing file is fatal rather than an empty list: every store is built from this repo, so its absence
    means a broken checkout, and a store without studios looks exactly like one whose corpus credits none.
    """
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    studios, seen = [], set()
    for i, entry in enumerate(raw.get("studios") or []):
        where = f"{path} studio {i}"
        name, why = entry.get("name"), entry.get("why")
        if not (isinstance(name, str) and name.strip() and isinstance(why, str) and why.strip()):
            sys.exit(f"{where}: every studio needs a name and a why")
        members = [_qid(q, where) for q in [entry.get("qid"), *(entry.get("also") or [])]]
        # One item in two studios would make a title's studio depend on which entry a reader met first.
        for q in members:
            if q in seen:
                sys.exit(f"{where}: Q{q} is listed twice — an item belongs to one studio")
            seen.add(q)
        studios.append((members[0], name.strip(), members))
    if not studios:
        sys.exit(f"{path} lists no studios")
    return studios


class Studios:
    """The curated studios the corpus credits, resolved to entity ids before the dictionary is frozen."""

    def __init__(self, curated):
        self.curated = curated
        self.kept = []

    def resolve(self, rows, ent):
        """Keep each studio with at least one item some title credits, and those items' entity ids.

        Resolved against the corpus's own `productionCompanies`, not the entity table: the table also
        describes entities no title credits, and a studio with a row but no titles is not one to link.
        """
        credited = set()
        for row in rows:
            for q in (row.get("facts") or {}).get("productionCompanies") or []:
                if isinstance(q, str):
                    credited.add(q)
        for qid, name, members in sorted(self.curated):
            ents = sorted(ent.id(f"Q{q}") for q in members if f"Q{q}" in credited)
            if ents:
                self.kept.append((qid, name, ents))

    def intern(self, strings):
        # Only the studios that ship: a name interned for a studio left out would sit in the published
        # dictionary with nothing pointing at it.
        for _, name, _ in self.kept:
            strings.add(name)

    def put(self, sec, strings):
        count = len(self.kept)
        sec.put("studio_qid", "I", [qid for qid, _, _ in self.kept], 4, expect=count)
        sec.put("studio_name", "I", [strings.id(name) for _, name, _ in self.kept], 4, expect=count)
        sec.put_list("studio_ent", "I", 4, [ents for _, _, ents in self.kept],
                     allow_empty=True, expect_rows=count)
