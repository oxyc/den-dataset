"""Each title's premise tags from `data/premise-tags-v2.json`, as `premise_tag_v`/`premise_tag_o`.

A tag is a short hyphenated structural phrase a model wrote from the title's Wikipedia plot
(`data/premise-tags-v2.SPEC.md`), most defining first. The premise vectors are embedded from the same
tags; these sections carry the strings themselves, for a reader that needs the words rather than the
vector. A missing input writes no sections, so a store built without it reads as no tags.

Joined by key like every other section: a tag set for a title the corpus does not hold is fatal, and so
is an entry that is not a non-empty list of distinct non-empty strings. A title with no tag set owns an
empty span.
"""
import json
import sys


class PremiseTags:
    """Validated tag sets, resolved to corpus rows before strings are frozen."""

    def __init__(self, path, keys):
        self.present = path is not None
        self.per_row = [()] * len(keys)
        self.tagged = 0
        self.source_count = 0
        if not path:
            return
        with open(path, encoding="utf-8") as fh:
            tags = json.load(fh).get("tags")
        if not isinstance(tags, dict) or not tags:
            sys.exit(f"{path}: `tags` must be a non-empty object of key -> tag list")
        rows = {key: row for row, key in enumerate(keys)}
        for key, values in tags.items():
            if key not in rows:
                sys.exit(f"{path}: {key} has premise tags and is not a corpus title — a tag set that "
                         f"joins nothing is a bug in the join, never a property of the data")
            if (not isinstance(values, list) or not values
                    or not all(isinstance(v, str) and v.strip() == v and v for v in values)
                    or len(set(values)) != len(values)):
                sys.exit(f"{path}: {key} has malformed premise tags {values!r} — expected a non-empty list "
                         f"of distinct, non-empty, unpadded strings")
            self.per_row[rows[key]] = tuple(values)
        self.source_count = len(tags)
        self.tagged = sum(1 for row in self.per_row if row)

    def intern(self, strings):
        for row in self.per_row:
            for tag in row:
                strings.add(tag)

    def put(self, sec, strings):
        if not self.present:
            return
        sec.put_list("premise_tag", "I", 4, ([strings.id(tag) for tag in row] for row in self.per_row))
