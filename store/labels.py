"""Labels — `primary_genre`, `animated`, and the scored `subgenre` / `mood` lists.

den-spec `wire/store-v1.md` § "Labels". Two passes label a title — the plot pass writes `labels`, the
premise pass writes `premiseLabels` — and these sections are the union of the two.
"""
from .format import hundredths


def title_labels(row):
    """The labelling a title got, from EITHER pass — `labels` (plot) or `premiseLabels`.

    Two passes label a title and only one of them was ever read here. 3 titles of 47,618 were labelled
    from their premise and never from a plot, so the store answered no primary genre, no subgenres and no
    moods for them while the legacy blobs answered all three — *Father and Sons*, *Two Sons of Ringo* and
    *Sítio do Picapau Amarelo*. They sit in the premise index, which is exactly where a reader asks.

    The plot record wins WHOLE where both passes answered, rather than field by field: within one pass the
    fields are coherent — an empty `subgenres` is the pass saying "none", not a gap — so filling one pass's
    empty list from the other's would emit a combination neither pass produced. It costs nothing on this
    corpus: all 44,528 titles both passes labelled agree byte-identically on all four fields, so this rule
    and any other give the same bytes for them, and the union is a pure addition of the 3.

    That agreement is a property of TODAY'S corpus, not a guarantee, so `disagreement()` below counts it
    and the build refuses rather than quietly choosing. A repass where the passes diverge is a decision
    about which labelling ships, and it should be made by someone rather than by the order of an `or`.
    """
    return (row.get("labels") or row.get("premiseLabels")) or {}


#: The fields a pass answers — the whole of what `title_labels` picks between.
LABEL_FIELDS = ("primaryGenre", "subgenres", "moods", "animated")


def disagreement(row):
    """The fields the two passes answer differently for one title, or `()` when they agree or only one
    answered.

    `title_labels` silently prefers the plot record, so this is the only thing that can see a divergence
    at all."""
    plot, premise = row.get("labels"), row.get("premiseLabels")
    if not plot or not premise:
        return ()
    return tuple(f for f in LABEL_FIELDS if plot.get(f) != premise.get(f))


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
    """The four label columns, and the per-pass coverage counts the build asserts against the artifacts."""

    def __init__(self):
        self.primary = []
        self.subgenres = []
        self.moods = []
        self.animated = []
        self.with_labels = 0
        self.with_plot_labels = 0
        self.with_premise = 0
        #: Titles the two passes answer differently for. The build refuses on a non-empty list.
        self.divergent = []

    def intern(self, strings, row):
        labels = title_labels(row)
        strings.add(labels.get("primaryGenre"))
        for entry in (labels.get("subgenres") or []) + (labels.get("moods") or []):
            strings.add(entry.get("label") if isinstance(entry, dict) else entry)

    def add(self, strings, key, row):
        # Counted off the SAME dict the sections below are written from, so the count proves the union
        # happened rather than agreeing with it by construction.
        labels = title_labels(row)
        if labels:
            self.with_labels += 1
        if row.get("labels"):
            self.with_plot_labels += 1
        if row.get("premiseLabels"):
            self.with_premise += 1
        fields = disagreement(row)
        if fields:
            self.divergent.append((key, fields))

        self.primary.append(strings.id(labels.get("primaryGenre")))
        self.subgenres.append(labelled(labels.get("subgenres"), strings, "subgenre", key))
        self.moods.append(labelled(labels.get("moods"), strings, "mood", key))
        self.animated.append(1 if labels.get("animated") else 0)

    def put(self, sec, rows):
        sec.put("primary_genre", "I", self.primary, 4, expect=rows)
        sec.put_labelled_list("subgenre", self.subgenres)
        sec.put_labelled_list("mood", self.moods)
        sec.put("animated", "B", self.animated, 1, expect=rows)
