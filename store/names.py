"""Wikidata names — `wd_name_key`, `wd_name_title`, `wd_name_orig`, `wd_name_year`.

den-spec `wire/store-v3.md` § "Wikidata names". `pipeline/names.py` writes, for every film and series
Wikidata gives a TMDB id, its name, year and original title. This keeps the ones the store has no row for —
a row's own card is its name — sorted by the packed key, the name stripped of a Wikipedia disambiguator as a
card's is (`cards.display_title`).

The strings share the one dictionary and its length bound (`prose_check`, the writer's prose backstop): an
original title over it is left off, and a name over it leaves its row out, each counted. The bound is not
raised for these, because a name that long is the case the bound exists to question. None of them reach
`alias_titles`, so they never become something search matches or the alias gate weighs.

Malformed input is fatal: a row that is not `[media, tmdbId, name, year, original]`, an unknown media, a key
listed twice. A missing input writes no sections.
"""
import json
import sys

from .cards import display_title
from .format import I16_NONE, U32_NONE

MEDIA = {"movie": 0, "tv": 1}


class WikidataNames:
    """The rows the store does not hold, sorted by packed key, and what was left out and why."""

    def __init__(self, path, keys, prose_check):
        self.present = path is not None
        self.rows = []
        self.source = None
        self.record = {}
        if not path:
            return
        with open(path, encoding="utf-8") as fh:
            table = json.load(fh)
        if not isinstance(table, dict) or table.get("schema") != 1 or not isinstance(table.get("rows"), list):
            sys.exit(f"{path}: not a schema-1 names table (pipeline/names.py)")
        self.source = table.get("source")
        held = {(MEDIA[media] << 32) | int(tmdb_id)
                for media, tmdb_id in (key.split(":") for key in keys)}
        seen = set()
        in_store = too_long = originals_dropped = 0
        fits = lambda text: prose_check([text]) is None
        for row in table["rows"]:
            if not (isinstance(row, list) and len(row) == 5 and row[0] in MEDIA and isinstance(row[1], int)
                    and row[1] > 0 and isinstance(row[2], str) and row[2].strip()
                    and (row[3] is None or isinstance(row[3], int))
                    and (row[4] is None or isinstance(row[4], str))):
                sys.exit(f"{path}: malformed row {row!r} — expected [movie|tv, tmdbId, name, year|null, "
                         f"original|null]")
            media, tmdb_id, name, year, original = row
            packed = (MEDIA[media] << 32) | tmdb_id
            if packed in seen:
                sys.exit(f"{path}: {media}:{tmdb_id} is listed twice")
            seen.add(packed)
            if packed in held:
                in_store += 1
                continue
            name = display_title({"en": name})
            if not name or not fits(name):
                too_long += 1
                continue
            original = original.strip() if original else None
            if original and not fits(original):
                originals_dropped += 1
                original = None
            if original == name:
                original = None
            if year is not None and not -32767 <= year <= 32767:
                year = None
            self.rows.append((packed, name, original, year))
        self.rows.sort()
        self.record = {"rows": len(table["rows"]), "kept": len(self.rows), "inStore": in_store,
                       "nameTooLong": too_long, "originalTooLong": originals_dropped}

    def intern(self, strings):
        for _, name, original, _ in self.rows:
            strings.add(name)
            strings.add(original)

    def put(self, sec, strings):
        if not self.present:
            return
        count = len(self.rows)
        sec.put("wd_name_key", "Q", [row[0] for row in self.rows], 8, expect=count)
        sec.put("wd_name_title", "I", [strings.id(row[1]) for row in self.rows], 4, expect=count)
        sec.put("wd_name_orig", "I", [strings.id(row[2], U32_NONE) for row in self.rows], 4, expect=count)
        sec.put("wd_name_year", "h", [I16_NONE if row[3] is None else row[3] for row in self.rows], 2,
                expect=count)
