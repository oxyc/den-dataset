"""The inverted maker index — `maker_ent`, `maker_rows_v`, `maker_rows_o`.

den-spec `wire/store-v1.md` § "The inverted maker index". 355 KB that turns a per-request linear scan of
every record into a lookup: `titles_sharing_makers` was 83.7 µs and 158.7 µs at 2x corpus, and 0.4 µs
flat once indexed.

It is derived entirely from the `makers` column `facts.py` built, so it has no row loop of its own.
"""
from collections import defaultdict


def put(sec, makers):
    by_maker = defaultdict(list)
    for row_i, row in enumerate(makers):
        for i in row:
            by_maker[i].append(row_i)
    maker_ent = sorted(by_maker)
    sec.put("maker_ent", "I", maker_ent, 4, expect=len(maker_ent))
    flat, offsets = [], [0]
    for i in maker_ent:
        flat.extend(by_maker[i])
        offsets.append(len(flat))
    sec.put("maker_rows_v", "I", flat, 4, expect=len(flat))
    sec.put("maker_rows_o", "I", offsets, 4, expect=len(maker_ent) + 1)
