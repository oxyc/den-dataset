"""Each title's other versions (oxyc/den-atlas#112) — `versions_v`, `versions_k`, `versions_o`.

The facts stage groups them (`pipeline/versions.py`) into each record's `otherVersions`; this resolves the
keys to rows. `versions_v` is the other titles' rows, ascending, and `versions_k` beside it the relation:
0 when the two adapt the same source work, 1 when they are linked through a screen work (a remake). The two
share `versions_o`.

Joined by key like every other section, and held to the shape the derivation promises: a version that is
not a corpus title, the title itself, a repeat, an unknown kind, or a link the other title does not return
is fatal. A title with none owns an empty span, and a corpus with none still writes the sections, empty.
"""
import sys

#: `otherVersions[].kind` -> `versions_k`.
KINDS = {"source": 0, "remake": 1}


class Versions:
    """Validated per-row lists of `(row, kind)`, ascending by row."""

    def __init__(self, rows, keys):
        index = {key: row for row, key in enumerate(keys)}
        self.per_row = []
        for key in keys:
            found = {}
            for link in (rows[key].get("facts") or {}).get("otherVersions") or []:
                other, kind = (link or {}).get("key"), (link or {}).get("kind")
                if other not in index or other == key or kind not in KINDS or index[other] in found:
                    sys.exit(f"{key}: otherVersions entry {link!r} is not a distinct other corpus title with "
                             f"a kind in {sorted(KINDS)} — a version that joins nothing is a bug in the join")
                found[index[other]] = KINDS[kind]
            self.per_row.append(sorted(found.items()))
        links = {(row, other, kind) for row, found in enumerate(self.per_row) for other, kind in found}
        one_way = sorted((keys[a], keys[b]) for a, b, kind in links if (b, a, kind) not in links)
        if one_way:
            sys.exit(f"otherVersions: {len(one_way)} links are not returned with the same kind, e.g. "
                     f"{one_way[:3]} — versions are symmetric")
        self.titles = sum(1 for found in self.per_row if found)

    def put(self, sec):
        sec.put_list("versions", "I", 4, ([row for row, _ in found] for found in self.per_row), allow_empty=True)
        sec.put("versions_k", "B", [kind for found in self.per_row for _, kind in found], 1)
