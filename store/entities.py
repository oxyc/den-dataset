"""Entities — the people, companies and places the fact columns point at.

den-spec `wire/store-v1.md` § "Entities". The index is the UNION of the entity table and every Q-id a
record refers to: 1,236 references (0.76%) name something the table does not describe, and interning
against the table alone dropped them — a cast member nobody can name still connects two titles, and the
rail counts that overlap by id without ever needing the name.
"""
import re
import sys
from collections import defaultdict

from .format import I32_NONE, U32_NONE
from .facts import ENTITY_LISTS, MAKER_FIELDS, PRECISION, PRECISION_NONE

#: A person's traits that name an item (oxyc/den#136), entity-table field -> section base name.
TRAIT_LISTS = {"gender": "ent_gender", "citizenship": "ent_citizen", "occupation": "ent_occupation"}
#: A person's dates, entity-table field -> section name; the precision is `<name>_prec`.
TRAIT_DATES = {"born": "ent_born", "died": "ent_died"}
_PERSON_DATE = re.compile(r"(-?)(\d{4,})(?:-(\d\d))?(?:-(\d\d))?")


def _numbers(qids):
    return {int(q[1:]) for q in qids if q.startswith("Q") and q[1:].isdigit()}


def referenced_qids(rows):
    """Every Q-id any record REFERS to, so the entity table can cover all of them."""
    found = set()
    for row in rows:
        facts_row = row.get("facts") or {}
        for field in (*MAKER_FIELDS, *ENTITY_LISTS):
            for q in facts_row.get(field) or []:
                if isinstance(q, str) and q.startswith("Q") and q[1:].isdigit():
                    found.add(int(q[1:]))
    return found


def trait_qids(table):
    """Every item a person's traits name — a gender, a country, an occupation — as a raw Q-id number, so
    the entity table holds each one even when Wikidata gives it no English name."""
    found = set()
    for qid, ent in table.items():
        if isinstance(ent, dict):
            for field in TRAIT_LISTS:
                for q in ent.get(field) or []:
                    if not (isinstance(q, str) and q.startswith("Q") and q[1:].isdigit()):
                        sys.exit(f"entity {qid} {field}: {q!r} is not a Wikidata item id")
                    found.add(int(q[1:]))
    return found


def days_from_civil(year, month, day):
    """Days since 1970-01-01 in the proleptic Gregorian calendar, for any year: `datetime.date` stops at
    year 1, and a screenwriter credit reaches Sophocles. Year 0 is 1 BCE, as in Wikidata's RDF dates.
    Howard Hinnant's `days_from_civil`."""
    year -= month <= 2
    era = year // 400
    yoe = year - era * 400
    doy = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def person_date(value, where):
    """`{"date": "-0496", "precision": "year"}` → (days since the epoch, precision code), the same
    encoding as `released`, with the decade and century a person's date may carry."""
    if value is None:
        return I32_NONE, PRECISION_NONE
    text = value.get("date") if isinstance(value, dict) else None
    code = PRECISION.get(value.get("precision")) if isinstance(value, dict) else None
    match = _PERSON_DATE.fullmatch(text) if isinstance(text, str) else None
    if match is None or code is None:
        sys.exit(f"{where}: {value!r} is not a person date this writer understands")
    sign, year, month, day = match.groups()
    return days_from_civil(int(year) * (-1 if sign else 1), int(month or 1), int(day or 1)), code


def intern(strings, table):
    """Every entity's name, and every other name they go by — people search indexes those too."""
    for qid, ent in table.items():
        strings.add((ent.get("en") if isinstance(ent, dict) else ent) or qid)
        if isinstance(ent, dict):
            for alias in ent.get("aliases") or []:
                strings.add(alias)
            strings.add(person_imdb_id(ent.get("imdbId")))


def person_imdb_id(raw):
    """The `nm…` id, or `None` — the person counterpart of `facts.title_imdb_id`. The facts stage keeps
    only `nm` ids already; this refuses anything else a hand-edited table might carry, because the column
    is a join key and a wrong-namespace id is a join that silently never matches."""
    if not isinstance(raw, str):
        return None
    return raw if raw.startswith("nm") and raw[2:].isdigit() else None


class Entities:
    """The entity index, and the sections describing it.

    Built before the row loop, because the fact columns intern their Q-ids against it.
    """

    def __init__(self, table, rows):
        self.table = table
        self.known = _numbers(table)
        self.referenced = referenced_qids(rows) | trait_qids(table)
        #: The sorted Q-id numbers. Everything referring to a person or company uses this index.
        self.qids = sorted(self.known | self.referenced)
        self._index = {q: i for i, q in enumerate(self.qids)}
        #: References the table could not resolve, by field. A reference that is dropped WITHOUT SAYING
        #: SO is how 2,680 franchise links disappeared into a section that looked perfectly well formed.
        self.unresolved = defaultdict(int)

    def intern_unnamed(self, strings):
        """A Q-id with no entry stands in as its own name, so it still resolves to something."""
        for num in self.referenced - self.known:
            strings.add(f"Q{num}")

    def id(self, qid, what=None):
        """An entity index, or None — counted, never silently discarded. 2,680 franchise references were
        dropped here without a word because the entity table does not contain franchise entities."""
        if not isinstance(qid, str) or not qid.startswith("Q") or not qid[1:].isdigit():
            return None
        found = self._index.get(int(qid[1:]))
        if found is None and what:
            self.unresolved[what] += 1
        return found

    def put(self, sec, strings, makers, cast):
        """The entity table, and how many titles credit each — the rarity weight the people row needs."""
        credits = [0] * len(self.qids)
        for row in makers:
            for i in row:
                credits[i] += 1
        for row in cast:
            for i in row:
                credits[i] += 1
        ent_name, ent_tmdb, ent_alias, ent_imdb = [], [], [], []
        trait_lists = {field: [] for field in TRAIT_LISTS}
        trait_dates = {field: ([], []) for field in TRAIT_DATES}
        by_num = {int(q[1:]): q for q in self.table if q.startswith("Q") and q[1:].isdigit()}
        for num in self.qids:
            # An entity the table does not describe: referenced by a record but with no entry. Its Q-id
            # stands in as its name — it still connects the titles that credit it, which is what the rail
            # and the cast overlap actually read.
            raw = self.table.get(by_num.get(num, f"Q{num}"))
            ent = raw if isinstance(raw, dict) else ({"en": raw} if raw else {})
            ent_name.append(strings.id(ent.get("en") or f"Q{num}"))
            tmdb = ent.get("tmdbPersonId")
            ent_tmdb.append(int(tmdb) if isinstance(tmdb, str) and tmdb.isdigit() else U32_NONE)
            # The OTHER names a person goes by. People search indexes these as well as the `en` name —
            # 64,075 of 162,812 entities have them, 118,958 in all — so a store without them answers
            # "Michael James Vogel" with nothing while the JSON facts answered Mike Vogel.
            #
            # The strings, not hashes: the reader hashes them with `name_key`, which folds and normalises
            # in a way this writer would have to reimplement, and a second copy of that algorithm is
            # exactly the drift this format exists to prevent. They land in the shared dictionary, so the
            # repeats cost nothing, and the reader drops them once its index is built.
            ent_alias.append([strings.id(a) for a in (ent.get("aliases") or []) if a])
            # IMDb's person id (Wikidata P345), for joining IMDb's own principals at run time. A join key
            # only: the Q-id stays the id anything public addresses an entity by.
            ent_imdb.append(strings.id(person_imdb_id(ent.get("imdbId"))))
            # A person's traits as Wikidata states them (oxyc/den#136): the items as entity ids, which
            # `trait_qids` put in the index, and the dates as `released` stores a date.
            for field in TRAIT_LISTS:
                trait_lists[field].append([self._index[int(q[1:])] for q in ent.get(field) or []])
            for field, (days, precision) in trait_dates.items():
                day, code = person_date(ent.get(field), f"entity Q{num} {field}")
                days.append(day)
                precision.append(code)
        count = len(self.qids)
        sec.put("ent_qid", "I", self.qids, 4, expect=count)
        sec.put("ent_name", "I", ent_name, 4, expect=count)
        sec.put("ent_tmdb", "I", ent_tmdb, 4, expect=count)
        sec.put("ent_credits", "I", credits, 4, expect=count)
        sec.put_list("ent_alias", "I", 4, ent_alias, expect_rows=count)
        sec.put("ent_imdb", "I", ent_imdb, 4, expect=count)
        # May be empty: a store built from facts scraped before traits were asked, or a corpus of no people.
        for field, section in TRAIT_LISTS.items():
            sec.put_list(section, "I", 4, trait_lists[field], allow_empty=True, expect_rows=count)
        for field, section in TRAIT_DATES.items():
            days, precision = trait_dates[field]
            sec.put(section, "i", days, 4, expect=count)
            sec.put(f"{section}_prec", "B", precision, 1, expect=count)

    def trait_counts(self):
        """How many entities carry each trait, for the build's summary line."""
        counts = {field: 0 for field in (*TRAIT_LISTS, *TRAIT_DATES)}
        for ent in self.table.values():
            if isinstance(ent, dict):
                for field in counts:
                    counts[field] += bool(ent.get(field))
        return counts
