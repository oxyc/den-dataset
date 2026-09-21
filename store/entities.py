"""Entities — the people, companies and places the fact columns point at.

den-spec `wire/store-v1.md` § "Entities". The index is the UNION of the entity table and every Q-id a
record refers to: 1,236 references (0.76%) name something the table does not describe, and interning
against the table alone dropped them — a cast member nobody can name still connects two titles, and the
rail counts that overlap by id without ever needing the name.
"""
from collections import defaultdict

from .format import U32_NONE
from .facts import ENTITY_LISTS, MAKER_FIELDS


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


def intern(strings, table):
    """Every entity's name, and every other name they go by — people search indexes those too."""
    for qid, ent in table.items():
        strings.add((ent.get("en") if isinstance(ent, dict) else ent) or qid)
        if isinstance(ent, dict):
            for alias in ent.get("aliases") or []:
                strings.add(alias)


class Entities:
    """The entity index, and the sections describing it.

    Built before the row loop, because the fact columns intern their Q-ids against it.
    """

    def __init__(self, table, rows):
        self.table = table
        self.known = _numbers(table)
        self.referenced = referenced_qids(rows)
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
        ent_name, ent_tmdb, ent_alias = [], [], []
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
        count = len(self.qids)
        sec.put("ent_qid", "I", self.qids, 4, expect=count)
        sec.put("ent_name", "I", ent_name, 4, expect=count)
        sec.put("ent_tmdb", "I", ent_tmdb, 4, expect=count)
        sec.put("ent_credits", "I", credits, 4, expect=count)
        sec.put_list("ent_alias", "I", 4, ent_alias, expect_rows=count)
