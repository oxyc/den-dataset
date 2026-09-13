#!/usr/bin/env python3
"""DT-N — merge the 219 coverage-fill premise vectors into the published premise index.

The premise index has shipped 219 rows short of the plot index since July. Those rows are not missing work:
they were computed and left unmerged in `v2/vectors/vectors-coverage-fill.bin`, so the best-performing index
in the dataset (premise beats plot at recommendation by +11.3 pp) simply cannot answer for 219 titles —
The Dark Knight among them.

Two things make this worth doing carefully rather than quickly:

  * Row order is NOT in the blob. It comes from the sidecar written beside it — `premise-ids.json` for the
    premise index. Pair the wrong sidecar with the wrong blob and you get an index that loads cleanly and
    returns nonsense, which is exactly the failure this repo has shipped before.
  * The published sidecar is a list of BARE tmdbIds, and this corpus contains ids that are both a film and a
    series. The fill keys are `mediaType:tmdbId`. Collapsing them to bare ids to match the old sidecar would
    reintroduce the collision, so the merge is refused if a fill id collides with an existing one.

    scripts/merge-premise-coverage.py <out-t02 dir> <dest dir>

Writes vectors-premise.bin + premise-ids.json + labels-premise.json into <dest dir>. Verifies before writing
and refuses on any mismatch; nothing is published by this script.
"""
import json
import os
import struct
import sys


def read_blob(path):
    with open(path, "rb") as fh:
        count, dim = struct.unpack("<ii", fh.read(8))
        raw = fh.read()
    if len(raw) != count * dim:
        sys.exit(f"{path}: header says {count}x{dim} = {count * dim} bytes, got {len(raw)}")
    return count, dim, raw


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "out-t02"
    dest = sys.argv[2] if len(sys.argv) > 2 else "out-premise-merged"
    os.makedirs(dest, exist_ok=True)

    base_count, dim, base_raw = read_blob(os.path.join(root, "vectors-premise.bin"))
    fill_count, fill_dim, fill_raw = read_blob(os.path.join(root, "v2/vectors/vectors-coverage-fill.bin"))
    if dim != fill_dim:
        sys.exit(f"dimension mismatch: premise {dim} vs fill {fill_dim} — different embedders, refusing")

    ids = json.load(open(os.path.join(root, "premise-tags-wip/premise-ids.json")))
    labels = json.load(open(os.path.join(root, "labels-premise.json")))
    rows = labels["records"] if isinstance(labels, dict) else labels
    fill_keys = json.load(open(os.path.join(root, "v2/vectors/keys-coverage-fill.json")))

    if len(ids) != base_count or len(rows) != base_count:
        sys.exit(f"sidecar disagrees with the blob: {base_count} vectors, {len(ids)} ids, {len(rows)} labels")
    if len(fill_keys) != fill_count:
        sys.exit(f"fill sidecar disagrees: {fill_count} vectors, {len(fill_keys)} keys")

    # The existing index is keyed positionally against labels-premise.json, which carries mediaType.
    base_pairs = [(r["mediaType"], r["tmdbId"]) for r in rows]
    if [t for _, t in base_pairs] != ids:
        sys.exit("premise-ids.json is not aligned with labels-premise.json — refusing to guess row order")

    have = set(base_pairs)
    fill_pairs = []
    for key in fill_keys:
        mt, _, tid = key.partition(":")
        fill_pairs.append((mt, int(tid)))
    dupes = [p for p in fill_pairs if p in have]
    if dupes:
        sys.exit(f"{len(dupes)} fill rows are already in the index ({dupes[:3]}) — refusing to double them")

    # A bare-id sidecar cannot represent a film and a series sharing an id. Refuse rather than ship an
    # index whose sidecar silently maps two titles to one row.
    merged_pairs = base_pairs + fill_pairs
    bare = [t for _, t in merged_pairs]
    if len(set(bare)) != len(bare):
        collide = {t for t in bare if bare.count(t) > 1}
        sys.exit(f"merging would make premise-ids.json ambiguous for {len(collide)} bare ids "
                 f"({sorted(collide)[:3]}) — the sidecar needs mediaType before this can ship")

    merged_count = base_count + fill_count
    out_blob = os.path.join(dest, "vectors-premise.bin")
    with open(out_blob, "wb") as fh:
        fh.write(struct.pack("<ii", merged_count, dim))
        fh.write(base_raw)
        fh.write(fill_raw)

    by_key = {(r["mediaType"], r["tmdbId"]): r for r in rows}
    plot = json.load(open(os.path.join(root, "labels-t02.json")))
    plot_rows = plot["records"] if isinstance(plot, dict) else plot
    plot_by_key = {(r["mediaType"], r["tmdbId"]): r for r in plot_rows}
    merged_rows = []
    for pair in merged_pairs:
        row = by_key.get(pair) or plot_by_key.get(pair)
        if row is None:
            sys.exit(f"no label row for {pair} — cannot write a sidecar that names it")
        merged_rows.append(row)

    json.dump([t for _, t in merged_pairs], open(os.path.join(dest, "premise-ids.json"), "w"))
    shape = {"count": merged_count, "records": merged_rows}
    if isinstance(labels, dict) and "taxonomyVersion" in labels:
        shape["taxonomyVersion"] = labels["taxonomyVersion"]
    json.dump(shape, open(os.path.join(dest, "labels-premise.json"), "w"), separators=(",", ":"))

    check_count, check_dim, check_raw = read_blob(out_blob)
    ok = (check_count == merged_count and check_dim == dim
          and check_raw[: base_count * dim] == base_raw
          and check_raw[base_count * dim:] == fill_raw)
    print(json.dumps({"merged": merged_count, "was": base_count, "added": fill_count,
                      "dim": dim, "roundTrip": ok, "dest": dest}))
    if not ok:
        sys.exit("round-trip check failed — do NOT publish this blob")


if __name__ == "__main__":
    main()
