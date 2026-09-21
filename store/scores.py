"""Scores, world, nouls, critique — and the three dense tables built the same way: technique,
depicts, audience.

den-spec `wire/store-v1.md` § "Scores, world, nouls, critique". Each dense table is R x N hundredths
with its vocabulary beside it as string ids; every title carries every axis, because an unanswered axis
must centre to `-mean` at load rather than be skipped by a cosine's name intersection.
"""
from .format import hundredths, score_hundredths

SCORE_AXES = ("intensity", "humour", "emotional_weight", "complexity")
SCORE_SECTION = {"intensity": "score_intensity", "humour": "score_humour",
                 "emotional_weight": "score_weight", "complexity": "score_complexity"}

# `world` is the max of these. The definition came from the rail-facets producer, which `build_store.py`
# replaced and which is deleted — so this list is now the only place it is written down.
FANTASTICAL = [f"theme__{k}" for k in (
    "vampire", "werewolf_monster", "zombie", "superhero", "time_travel", "cyberpunk",
    "dystopian_post_apocalyptic", "folk_horror")] + [f"subgenre__{k}" for k in (
    "supernatural_horror", "sci_fi_horror", "sci_fi_action", "fantasy_adventure")]

#: The dense tables, in the order they are written: corpus field -> section base name. Each contributes
#: `<name>` (R x N hundredths) and `<name>_names` (its vocabulary, as string ids).
DENSE_TABLES = (("critique", "critique"), ("technique", "technique"),
                ("depicts", "depicts"), ("audience", "audience"))


class Scores:
    """The four score axes, `world`, the sparse noul list, and the four dense tables.

    The vocabularies are collected over the whole corpus first, because a dense table's width is the
    number of names it ends up with — so `freeze_vocabularies` has to run before the first row is added.
    """

    def __init__(self):
        self.names = {"noul": set(), "critique": set(), "technique": set(),
                      "depicts": set(), "audience": set()}
        self.axes = {a: [] for a in SCORE_AXES}
        self.world = []
        self.noul_rows = []
        self.dense = {name: bytearray() for _, name in DENSE_TABLES}
        self._noul_id = None

    def intern(self, row):
        """The vocabularies, which are the taxonomy's names rather than a model's answers."""
        self.names["noul"].update((row.get("nouls") or {}).keys())
        for field, _ in DENSE_TABLES:
            self.names[field].update((row.get(field) or {}).keys())

    def freeze_vocabularies(self, strings):
        """Sort every vocabulary and intern it. The sorted order is each dense table's column order and
        each noul id, so it has to be fixed before any row is written."""
        for what in self.names:
            self.names[what] = sorted(self.names[what])
        for what in ("noul", "critique", "technique", "depicts", "audience"):
            for name in self.names[what]:
                strings.add(name)
        self._noul_id = {name: i for i, name in enumerate(self.names["noul"])}

    def add(self, key, row):
        sc = row.get("scores") or {}
        for axis in SCORE_AXES:
            entry = sc.get(axis)
            self.axes[axis].append(score_hundredths((entry or {}).get("score"), f"score {axis}", key))

        nouls = row.get("nouls") or {}
        worst = 0
        pairs = []
        for name, entry in sorted(nouls.items()):
            value = hundredths((entry or {}).get("noul"), f"noul {name}", key)
            if value:
                pairs.append((self._noul_id[name], value))
            if name in FANTASTICAL:
                worst = max(worst, value)
        self.world.append(worst)
        self.noul_rows.append(pairs)

        for field, section in DENSE_TABLES:
            answers = row.get(field) or {}
            column = self.dense[section]
            for name in self.names[field]:
                column.append(hundredths((answers.get(name) or {}).get("noul"), f"{field} {name}", key))

    def put(self, sec, rows, strings):
        for axis in SCORE_AXES:
            sec.put(SCORE_SECTION[axis], "H", self.axes[axis], 2, expect=rows)
        sec.put("world", "B", self.world, 1, expect=rows)
        sec.put_list("noul_k", "B", 1, [[k for k, _ in row] for row in self.noul_rows])
        sec.put_list("noul_v", "B", 1, [[v for _, v in row] for row in self.noul_rows])
        sec.put("noul_names", "I", [strings.id(x) for x in self.names["noul"]], 4,
                expect=len(self.names["noul"]))
        for field, section in DENSE_TABLES:
            names = self.names[field]
            sec.put_raw(section, self.dense[section], 1, expect=rows * len(names))
            sec.put(f"{section}_names", "I", [strings.id(x) for x in names], 4, expect=len(names))
