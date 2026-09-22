"""Labels — `primary_genre`, `animated`, and the scored `subgenre` / `mood` lists.

den-spec `wire/store-v1.md` § "Labels". A title's genres & moods are the corpus `labels` field, which the
corpus join took from `genres-moods.json`. The premise pass's labels used to be a second source; they were a
copy of the plot pass's (44,528 of 44,531 identical, oxyc/den-dataset#56), and a second source is a second
answer, so they are not read.
"""
from .format import hundredths


def title_labels(row):
    """A title's genres & moods, or `{}` when it has none."""
    return row.get("labels") or {}


def labelled(entries, strings, what, key):
    """`[{"label": ..., "confidence": ...}]` → `[(string id, hundredths)]`, tolerating a bare string."""
    out = []
    for entry in entries or []:
        if isinstance(entry, dict):
            label, conf = entry.get("label"), entry.get("confidence")
        else:
            label, conf = entry, None
        if not label:
            continue
        out.append((strings.id(label), hundredths(conf, f"{what} confidence", key) if conf else 0))
    return out


class Labels:
    """The four label columns, and the coverage count the build asserts against `genres-moods.json`."""

    def __init__(self):
        self.primary = []
        self.subgenres = []
        self.moods = []
        self.animated = []
        self.with_labels = 0

    def intern(self, strings, row):
        labels = title_labels(row)
        strings.add(labels.get("primaryGenre"))
        for entry in (labels.get("subgenres") or []) + (labels.get("moods") or []):
            strings.add(entry.get("label") if isinstance(entry, dict) else entry)

    def add(self, strings, key, row):
        # Counted off the SAME dict the sections below are written from, so the count proves what was
        # written rather than what the corpus holds.
        labels = title_labels(row)
        if labels:
            self.with_labels += 1
        self.primary.append(strings.id(labels.get("primaryGenre")))
        self.subgenres.append(labelled(labels.get("subgenres"), strings, "subgenre", key))
        self.moods.append(labelled(labels.get("moods"), strings, "mood", key))
        self.animated.append(1 if labels.get("animated") else 0)

    def put(self, sec, rows):
        sec.put("primary_genre", "I", self.primary, 4, expect=rows)
        sec.put_labelled_list("subgenre", self.subgenres)
        sec.put_labelled_list("mood", self.moods)
        sec.put("animated", "B", self.animated, 1, expect=rows)
