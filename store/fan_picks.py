"""Fan picks for You Might Also Like (oxyc/den-atlas#121) — `fan_picks_v`, `fan_picks_o`, `fan_picks_a`.

`tools/fan_picks.py export` writes, per title a model was asked about, the titles it named that a fan would
also love, matched to corpus keys, in the model's order. This resolves the keys to rows: `fan_picks_v` is the
picks' rows in the file's order, sharing `fan_picks_o`, and `fan_picks_a` is 1 for every title the file names
— an asked title with no picks is an answer, where a title the file does not name was never asked.

Joined by key like every other section: a title or a pick that is not a corpus title, a pick that is the title
itself or repeats, and a list that is not a list of keys are fatal. A missing input writes no sections.
"""
import json
import sys


class FanPicks:
    """Validated per-row lists of pick rows, in the file's order, and which rows were asked."""

    def __init__(self, path, keys):
        self.present = path is not None
        self.per_row = [()] * len(keys)
        self.asked = [0] * len(keys)
        self.anchors = 0
        self.source_count = 0
        if not path:
            return
        with open(path, encoding="utf-8") as fh:
            anchors = json.load(fh).get("anchors")
        if not isinstance(anchors, dict) or not anchors:
            sys.exit(f"{path}: `anchors` must be a non-empty object of key -> [pick key, ...]")
        rows = {key: row for row, key in enumerate(keys)}
        for anchor, picks in anchors.items():
            if anchor not in rows:
                sys.exit(f"{path}: {anchor} has fan picks and is not a corpus title — a row that joins "
                         f"nothing is a bug in the join, never a property of the data")
            if not isinstance(picks, list):
                sys.exit(f"{path}: {anchor}'s fan picks are not a list")
            found = []
            for pick in picks:
                if pick not in rows or pick == anchor or rows[pick] in found:
                    sys.exit(f"{path}: {anchor} has a malformed pick {pick!r} — expected a distinct other "
                             f"corpus title")
                found.append(rows[pick])
            self.per_row[rows[anchor]] = tuple(found)
            self.asked[rows[anchor]] = 1
        self.source_count = len(anchors)
        self.anchors = sum(self.asked)

    def put(self, sec):
        if not self.present:
            return
        sec.put_list("fan_picks", "I", 4, self.per_row, allow_empty=True)
        sec.put("fan_picks_a", "B", self.asked, 1, expect=len(self.asked))
