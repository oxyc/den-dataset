"""Identity — `keys`, `strings`, `str_off`.

den-spec `wire/store-v1.md` § "Identity". `keys` is the primary key every other per-title section is
indexed by, and the dictionary is the one place a string can reach the file: every other section holds
a number, an id, or an offset into this one.
"""


def sorted_keys(rows):
    """The row order: movies before series, then by tmdb id.

    It is the order `keys` is written in and the order den-atlas binary-searches, so it is also what
    every per-title section's row i means.
    """
    return sorted(rows, key=lambda k: ((0 if k.split(":", 1)[0] == "movie" else 1),
                                       int(k.split(":", 1)[1])))


def put(sec, keys, ordered_strings, rows):
    """`keys`, then the dictionary blob and its offsets."""
    sec.put("keys", "Q", [((0 if k.split(":", 1)[0] == "movie" else 1) << 32) | int(k.split(":", 1)[1])
                          for k in keys], 8, expect=rows)
    blob = "".join(ordered_strings).encode("utf-8")
    offs, at = [0], 0
    for s in ordered_strings:
        at += len(s.encode("utf-8"))
        offs.append(at)
    sec.put_raw("strings", blob, 1)
    sec.put("str_off", "I", offs, 4, expect=len(ordered_strings) + 1)
