"""Awards: the ceremonies each title won or was nominated at, and the ceremony table they index.

den-spec `wire/store-v2.md` § "Awards". The facts stage reads Wikidata's P166 (award received) and P1411
(nominated for) and files each award under its ceremony or awarding body (`lib/wikidata_facts.py`
`award_ceremonies`), so a record carries `awardsWonAt` — ceremonies it won at least one award at — and
`awardsNominatedAt` — ceremonies it was only nominated at. The two are disjoint by construction.

A ceremony's name is its entry in the entity table the facts stage wrote; one Wikidata has no label for is
named by its Q-id, as an unnamed entity is.
"""
import sys


def _num(qid, key):
    if not (isinstance(qid, str) and qid.startswith("Q") and qid[1:].isdigit()):
        sys.exit(f"{key}: award ceremony {qid!r} is not a Wikidata item id")
    return int(qid[1:])


class Awards:
    """Built before the dictionary is frozen, because the ceremony names have to be interned."""

    def __init__(self, table):
        self.table = table
        self.ceremonies = []
        self._index = {}
        self.rows = []
        self.titles = 0

    def resolve(self, keys, rows):
        """Every ceremony any title names, sorted by Q-id; then each row's awards against that order."""
        per_row, seen = [], set()
        for key in keys:
            facts = rows[key].get("facts") or {}
            won = [_num(q, key) for q in facts.get("awardsWonAt") or []]
            nominated = [_num(q, key) for q in facts.get("awardsNominatedAt") or []]
            if set(won) & set(nominated):
                sys.exit(f"{key}: a ceremony is in both awardsWonAt and awardsNominatedAt — the facts stage "
                         f"lists a ceremony with a win under won alone")
            per_row.append({**{q: 0 for q in nominated}, **{q: 1 for q in won}})
            seen.update(won)
            seen.update(nominated)
        self.ceremonies = sorted(seen)
        self._index = {q: i for i, q in enumerate(self.ceremonies)}
        self.rows = [sorted((self._index[q], w) for q, w in row.items()) for row in per_row]
        self.titles = sum(1 for row in self.rows if row)

    def name(self, num):
        ent = self.table.get(f"Q{num}")
        name = ent.get("en") if isinstance(ent, dict) else ent
        return name or f"Q{num}"

    def intern(self, strings):
        for num in self.ceremonies:
            strings.add(self.name(num))

    def put(self, sec, strings):
        count = len(self.ceremonies)
        sec.put("ceremony_qid", "I", self.ceremonies, 4, expect=count)
        sec.put("ceremony_name", "I", [strings.id(self.name(q)) for q in self.ceremonies], 4, expect=count)
        values, won, offsets = [], [], [0]
        for row in self.rows:
            for index, flag in row:
                values.append(index)
                won.append(flag)
            offsets.append(len(values))
        if len(offsets) != sec.rows + 1:
            sys.exit(f"section award: {len(offsets)} offsets, expected {sec.rows + 1}")
        # May be empty: a small corpus can hold no award-winning title. A real one with none was scraped
        # before the facts stage asked for awards, which is why the build prints `awardTitles`.
        sec.put("award_v", "I", values, 4)
        sec.put("award_w", "B", won, 1)
        sec.put("award_o", "I", offsets, 4)
