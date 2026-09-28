"""Jev's More Like This scores (#132) — `jev_like_v`, `jev_like_p`, `jev_like_o`.

`tools/rulers/more_like_cascade_full.py export` writes, per anchor title, the candidates its cascade weighed
(atlas's top ten and the screen's promotions) with Jev's overall Noul for each, in hundredths. This resolves
the keys to rows: `jev_like_v` is the candidates' rows in the file's order, and `jev_like_p` beside it each
one's Noul as a `u8` in hundredths. The two share `jev_like_o`.

Sparse: an anchor the cascade gave no scores (no article evidence, a failed call) owns an empty span, and a
reader then ranks it exactly as without the section. Joined by key like every other section: an anchor or a
candidate that is not a corpus title, a candidate that is the anchor or repeats, and a score that is not a
probability in hundredths are fatal. A missing input writes no sections.
"""
import json
import sys


class JevMoreLike:
    """Validated per-row lists of `(row, hundredths)`, in the file's order."""

    def __init__(self, path, keys):
        self.present = path is not None
        self.per_row = [()] * len(keys)
        self.anchors = 0
        self.source_count = 0
        if not path:
            return
        with open(path, encoding="utf-8") as fh:
            anchors = json.load(fh).get("anchors")
        if not isinstance(anchors, dict) or not anchors:
            sys.exit(f"{path}: `anchors` must be a non-empty object of key -> [[candidate, score], ...]")
        rows = {key: row for row, key in enumerate(keys)}
        for anchor, pairs in anchors.items():
            if anchor not in rows:
                sys.exit(f"{path}: {anchor} has Jev scores and is not a corpus title — a row that joins "
                         f"nothing is a bug in the join, never a property of the data")
            found = []
            for pair in pairs if isinstance(pairs, list) and pairs else [None]:
                other, score = pair if isinstance(pair, list) and len(pair) == 2 else (None, None)
                hundredths = round(score * 100) if isinstance(score, (int, float)) \
                    and not isinstance(score, bool) else None
                if other not in rows or other == anchor or rows[other] in (r for r, _ in found) \
                        or hundredths is None or not 0 <= hundredths <= 100 or hundredths / 100 != score:
                    sys.exit(f"{path}: {anchor} has a malformed candidate {pair!r} — expected a distinct "
                             f"other corpus title and a probability with at most two decimals")
                found.append((rows[other], hundredths))
            self.per_row[rows[anchor]] = tuple(found)
        self.source_count = len(anchors)
        self.anchors = sum(1 for row in self.per_row if row)

    def put(self, sec):
        if not self.present:
            return
        sec.put_list("jev_like", "I", 4, ([row for row, _ in found] for found in self.per_row))
        sec.put("jev_like_p", "B", [p for found in self.per_row for _, p in found], 1)
