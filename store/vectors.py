"""Vectors — `vec_plot`, `vec_premise` and the two `_has` columns.

den-spec `wire/store-v1.md` § "Vectors". Each blob is re-ordered from its own row order into the
store's by KEY; a row with no vector is zeroed and its `_has` byte left at 0.
"""
import os
import sys

_V2 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "v2")
if _V2 not in sys.path:
    sys.path.insert(0, _V2)
import vector_blob  # noqa: E402  — the blob layout, shared with the migration

DIMS = 1024


def read(path, declared, what):
    """`(blob, base, row_of_key)` — the file, where row 0 starts, and which row each title owns.

    The row order used to come from `what`: a separate `labels-*.json` whose record order the blob was
    assumed to match. That assumption could not be checked, only relied on — regenerate the labels file
    with a different record order and every vector moves onto the wrong title, in a file that still loads
    and still returns real numbers for everything.

    A `DENVEC02` blob names its own rows, so the join is by key and the ordering assumption is gone. What
    replaces it is a check that can actually fail: the blob's key set must equal the key set `what`
    declares. A blob and a labels file from different generations now disagree loudly here instead of
    shifting the corpus silently.
    """
    count, dims, keys, blob, base = vector_blob.read(path)
    if dims != DIMS:
        sys.exit(f"{path}: header says {dims} dims, this store stores {DIMS}")
    declared = set(declared)
    found = set(keys)
    if found != declared:
        missing, extra = sorted(declared - found)[:3], sorted(found - declared)[:3]
        sys.exit(f"{path}: {count} vectors keyed for {len(found)} titles, but {what} declares "
                 f"{len(declared)} — {len(declared - found)} declared titles have no vector "
                 f"(e.g. {missing}) and {len(found - declared)} vectors name a title it does not "
                 f"(e.g. {extra}). The blob and its labels file must be the same generation.")
    return blob, base, {k: i for i, k in enumerate(keys)}


def _gather(keys, blob, base, row_of_key, what):
    """One blob's rows, laid out in the store's row order. Returns `(matrix, has, hits)`."""
    rows = len(keys)
    matrix = bytearray(rows * DIMS)
    has = [0] * rows
    hits = 0
    for out_i, key in enumerate(keys):
        src = row_of_key.get(key)
        if src is None:
            continue
        start = base + src * DIMS
        row = blob[start:start + DIMS]
        if len(row) != DIMS:
            sys.exit(f"{key}: {what} vector row {src} is {len(row)} bytes, not {DIMS}")
        matrix[out_i * DIMS:(out_i + 1) * DIMS] = row
        has[out_i] = 1
        hits += 1
    return matrix, has, hits


def put(sec, keys, args, plot_labels, premise_labels):
    """The four vector sections. Returns `(plot hits, plot rows declared, premise hits, premise rows
    declared)` — the counts the build asserts against the blobs' own key columns."""
    print("reading vectors …", file=sys.stderr)
    plot_blob, plot_base, plot_row = read(args.vectors, plot_labels, args.vector_labels)
    plot, has_plot, plot_hits = _gather(keys, plot_blob, plot_base, plot_row, "plot")
    sec.put_raw("vec_plot", plot, 1, expect=len(keys) * DIMS)

    premise = bytearray(len(keys) * DIMS)
    has_premise = [0] * len(keys)
    premise_hits = 0
    premise_row = {}
    if args.premise_vectors and premise_labels:
        pblob, pbase, premise_row = read(args.premise_vectors, premise_labels, args.premise_labels)
        premise, has_premise, premise_hits = _gather(keys, pblob, pbase, premise_row, "premise")
    sec.put_raw("vec_premise", premise, 1, expect=len(keys) * DIMS)
    sec.put("vec_premise_has", "B", has_premise, 1, expect=len(keys))
    # The plain question, answerable without scanning 1024 bytes for a non-zero.
    sec.put("vec_plot_has", "B", has_plot, 1, expect=len(keys))
    return plot_hits, len(plot_row), premise_hits, len(premise_row)
