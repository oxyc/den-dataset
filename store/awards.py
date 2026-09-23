"""Awards: the ceremonies each title won or was nominated at, and the ceremony table they index.

den-spec `wire/store-v2.md` § "Awards". The facts stage reads Wikidata's P166 (award received) and P1411
(nominated for) and files each award under its ceremony or awarding body (`lib/wikidata_facts.py`
`ceremony`), so a record carries `awardsWonAt` — ceremonies it won at least one award at — and
`awardsNominatedAt` — ceremonies it was only nominated at. The two are disjoint by construction.

Wikidata files some bodies' prizes half under the organisation and half under its "Awards" group, so one
body reaches the corpus as two ceremonies: the National Board of Review, Venice, Berlin. The hand-kept
`data/award-ceremony-merges.json` files each `from` under its `into` here, before the table is built, and a
title recognised at both is recognised once, won if it won at either.

A merge whose `from` no title names any more is stale: the facts have moved on and the entry now says
nothing. It is recorded, not refused, because a small corpus (a fixture, a partial run) legitimately names
none of these bodies; `--stamp-meta` writes the record as `awardMerges`, and `check_award_merges.py --gate`
refuses to publish a store built with a stale one.

A ceremony's name is its entry in the entity table the facts stage wrote; one Wikidata has no label for is
named by its Q-id, as an unnamed entity is.
"""
import hashlib
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MERGES = os.path.join(REPO, "data", "award-ceremony-merges.json")


def _num(qid, key):
    if not (isinstance(qid, str) and qid.startswith("Q") and qid[1:].isdigit()):
        sys.exit(f"{key}: award ceremony {qid!r} is not a Wikidata item id")
    return int(qid[1:])


def load_merges(path=MERGES):
    """`({from: into}, sha256)` from the merge list, refusing a malformed one.

    Missing is fatal rather than empty, as `studios.load` is: every store is built from this repo. Refused:
    an entry without a `why`, a bad Q-id, a `from` listed twice, one merged into itself, and a chain — an
    `into` that is some other entry's `from` would make the answer depend on the order entries apply in.
    """
    with open(path, "rb") as fh:
        blob = fh.read()
    merges = {}
    for i, entry in enumerate(json.loads(blob).get("merges") or []):
        where = f"{path} merge {i}"
        why = entry.get("why")
        if not (isinstance(why, str) and why.strip()):
            sys.exit(f"{where}: every merge needs a why")
        source, into = _num(entry.get("from"), where), _num(entry.get("into"), where)
        if source == into:
            sys.exit(f"{where}: Q{source} is merged into itself")
        if source in merges:
            sys.exit(f"{where}: Q{source} is merged twice")
        merges[source] = into
    for source, into in merges.items():
        if into in merges:
            sys.exit(f"{path}: Q{source} merges into Q{into}, which itself merges into Q{merges[into]} — "
                     f"merge into the last one directly")
    return merges, hashlib.sha256(blob).hexdigest()


class Awards:
    """Built before the dictionary is frozen, because the ceremony names have to be interned."""

    def __init__(self, table, merges=None, merges_sha=None):
        self.table = table
        self.merges = merges or {}
        self.merges_sha = merges_sha
        self.ceremonies = []
        self._index = {}
        self.rows = []
        self.titles = 0
        self.record = {}

    def resolve(self, keys, rows):
        """Every ceremony any title names after the merges, sorted by Q-id; then each row's awards against
        that order. A title at both halves of a merged body is there once, won if it won at either."""
        per_row, named = [], set()
        for key in keys:
            facts = rows[key].get("facts") or {}
            won = [_num(q, key) for q in facts.get("awardsWonAt") or []]
            nominated = [_num(q, key) for q in facts.get("awardsNominatedAt") or []]
            if set(won) & set(nominated):
                sys.exit(f"{key}: a ceremony is in both awardsWonAt and awardsNominatedAt — the facts stage "
                         f"lists a ceremony with a win under won alone")
            named.update(won)
            named.update(nominated)
            row = {}
            for q, flag in [(q, 0) for q in nominated] + [(q, 1) for q in won]:
                q = self.merges.get(q, q)
                row[q] = max(row.get(q, 0), flag)
            per_row.append(row)
        self.ceremonies = sorted({q for row in per_row for q in row})
        self._index = {q: i for i, q in enumerate(self.ceremonies)}
        self.rows = [sorted((self._index[q], w) for q, w in row.items()) for row in per_row]
        self.titles = sum(1 for row in self.rows if row)
        applied = sorted(q for q in self.merges if q in named)
        self.record = {"sha256": self.merges_sha, "applied": len(applied),
                       "stale": [f"Q{q}" for q in sorted(set(self.merges) - set(applied))]}

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
