#!/usr/bin/env python3
"""DT-F's weekly re-cluster: groups the embedding finds cohesive that the vocabulary has no word for.

    pipeline/recluster.py --labels out/labels-t02.json --vectors out/vectors-bge-m3.bin --out report.json \
        [--k 200] [--iterations 8] [--min-size 25] [--max-purity 0.35] [--min-cohesion 0.55]

k-means over the shipped vectors, then one question per cluster: how dominant is its most common existing
subgenre (PURITY), and how tight is it (COHESION — mean cosine to its centroid)? Low purity alone finds
grab-bags; low purity with high cohesion is a coherent group the taxonomy cannot name. It reports and never
edits: naming a cluster is a human judgement, and adding a label is a taxonomy bump that forces a
whole-universe reclassification.

Not a stage. It is in no build order — nothing it writes is read by anything the pipeline builds — so no
stage imports it; `pipeline/recluster-run.sh` is its timer and runs it by path.

**Deterministic, and the Swift's bytes.** Seeds are stride-sampled rather than random, so a weekly run is
comparable to the last one instead of reshuffling every cluster id; and the arithmetic is the Swift's in
the Swift's order, so a report means the same thing as the ones before it. That order is what costs: a
dot product accumulated left to right is ~40x slower in Python than in C, and k-means is ~40 billion of
them at k=800. So each assignment is decided in two steps. `math.sumprod` scores every centroid (C speed,
extended precision), and only the centroids within `SLACK` of the best are re-scored in the Swift's exact
order to pick the winner. A sequential double sum of 1024 products of unit vectors is off the exact value
by at most ~1024 ulps (~1e-13), so a centroid further than SLACK behind cannot be the one the exact
arithmetic picks — the answer is the Swift's, including its tie-break to the lowest index.
"""
import argparse
import array
import functools
import json
import math
import operator
import os
import platform
import sys

# `math.sumprod` is 3.12's. A plain Python dot product in its place gives the same report — the fast score
# only shortlists, the exact one decides — but measured over the corpus at k=50 it took 305 s against 68,
# which at the timer's k=800 is an hour and more rather than ~15 minutes. So an older interpreter is refused
# here, by name, before the imports below fail on it less legibly.
if sys.version_info < (3, 12):
    sys.exit(f"recluster.py needs Python 3.12 or newer (for math.sumprod); {sys.executable} is "
             f"{platform.python_version()}. pipeline/recluster-run.sh finds one on PATH, or set PYTHON to one.")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if not __package__:
    # Run as a file: the repo, not pipeline/, is the import root.
    sys.path[0] = REPO
from pipeline import jsonbytes  # noqa: E402  — the Swift encoder's bytes
from store import vector_blob  # noqa: E402  — the DENVEC02 layout, one definition

#: How far behind the best fast score a centroid can be and still be re-scored exactly. Nine orders of
#: magnitude above the error it has to cover.
SLACK = 1e-9


def exact_dot(a, b):
    """Left to right from 0.0 — the Swift's `dot += a[d] * b[d]`. Not `sum()`, which compensates since 3.12."""
    return functools.reduce(operator.add, map(operator.mul, a, b), 0.0)


def normalised(values):
    norm = exact_dot(values, values)
    if norm <= 0:
        return list(values)
    inv = 1 / math.sqrt(norm)
    return [x * inv for x in values]


def rows(blob, base, count, dim):
    """Each int8 row as unit-length doubles, converted once: k-means reads every row every iteration.

    Held as `array('d')` — 8 bytes a value, the Swift's footprint (~390 MB for the corpus) — and turned
    into a list only while one row is being scored, because a list of float objects is four times that."""
    raw = memoryview(blob)[base:base + count * dim].cast("b")
    return [array.array("d", normalised([float(x) for x in raw[i * dim:(i + 1) * dim]]))
            for i in range(count)]


def nearest(row, centroids):
    """The index of the centroid with the largest exact dot, lowest index on a tie."""
    row = row.tolist()  # `sumprod` is ~3x faster over a list of floats than over an array
    fast = [math.sumprod(row, centroid) for centroid in centroids]
    top = max(fast)
    best, best_score = 0, -math.inf
    for c, score in enumerate(fast):
        if score >= top - SLACK:
            exact = exact_dot(row, centroids[c])
            if exact > best_score:
                best, best_score = c, exact
    return best


def kmeans(vectors, k, iterations):
    """Stride-seeded k-means on the unit sphere. Returns the last assignment and the centroids after the
    update that follows it — the pair the Swift reported from, cohesion included."""
    count = len(vectors)
    stride = max(1, count // max(k, 1))
    centroids = [vectors[i * stride].tolist() for i in range(k) if i * stride < count]
    if not centroids:
        sys.exit("no centroids — is the corpus empty?")
    dim = len(vectors[0])
    assignment = [0] * count
    for _ in range(iterations):
        assignment = [nearest(row, centroids) for row in vectors]
        sums = [None] * len(centroids)
        for i, c in enumerate(assignment):
            sums[c] = list(map(operator.add, sums[c] or [0.0] * dim, vectors[i]))
        for c, total in enumerate(sums):
            if total is not None:
                centroids[c] = normalised(total)
    return assignment, centroids


def candidates(records, vectors, assignment, centroids, min_size, max_purity, min_cohesion):
    members = [[] for _ in centroids]
    for i, c in enumerate(assignment):
        members[c].append(i)
    out = []
    for cluster, idxs in enumerate(members):
        if len(idxs) < min_size:
            continue
        tally = {}
        for i in idxs:
            for item in records[i]["subgenres"]:
                tally[item["label"]] = tally.get(item["label"], 0) + 1
        # Most common label; the alphabetically first on a tie.
        dominant = min(tally.items(), key=lambda kv: (-kv[1], kv[0])) if tally else None
        purity = (dominant[1] if dominant else 0) / len(idxs)
        if purity > max_purity:
            continue  # an existing label rediscovering itself
        cohesion = functools.reduce(operator.add, (exact_dot(vectors[i], centroids[cluster]) for i in idxs),
                                    0.0) / len(idxs)
        if cohesion < min_cohesion:
            continue  # a loose grab-bag, not an emergent group
        row = {"cluster": cluster, "size": len(idxs), "purity": purity, "cohesion": cohesion,
               "examples": [f"{records[i]['mediaType']}:{records[i]['tmdbId']}" for i in idxs[:8]]}
        if dominant:
            row["dominantLabel"] = dominant[0]
        out.append(row)
    # Tightest first: cohesion is what makes a candidate worth a human's time, not size.
    return sorted(out, key=lambda row: (-row["cohesion"], row["cluster"]))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--labels", required=True, help="the labels naming each vector")
    ap.add_argument("--vectors", required=True, help="the shipped DENVEC02 blob")
    ap.add_argument("--out", required=True, help="where to write the emergent-candidate report")
    ap.add_argument("--k", type=int, default=200)
    ap.add_argument("--iterations", type=int, default=8)
    ap.add_argument("--min-size", type=int, default=25)
    ap.add_argument("--max-purity", type=float, default=0.35)
    ap.add_argument("--min-cohesion", type=float, default=0.55)
    args = ap.parse_args(argv)

    with open(args.labels, encoding="utf-8") as fh:
        records = json.load(fh)["records"]
    count, dim, keys, blob, base = vector_blob.read(args.vectors)
    # An identity check, not a count check: purity is per label, and a blob whose rows belong to other
    # titles would report clean-looking purity for clusters built from the wrong vectors.
    expected = [f"{r['mediaType']}:{r['tmdbId']}" for r in records]
    if keys != expected:
        at = next((i for i, (a, b) in enumerate(zip(keys, expected)) if a != b), min(len(keys), len(expected)))
        sys.exit(f"{args.vectors} ({count}x{dim}) names different titles than {args.labels} "
                 f"({len(records)} records): row {at} is {keys[at] if at < len(keys) else 'absent'} in the "
                 f"blob and {expected[at] if at < len(expected) else 'absent'} in the labels")

    vectors = rows(blob, base, count, dim)
    assignment, centroids = kmeans(vectors, args.k, args.iterations)
    found = candidates(records, vectors, assignment, centroids, args.min_size, args.max_purity,
                       args.min_cohesion)
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(jsonbytes.compact(found))
    os.replace(tmp, args.out)
    print(f"recluster: {len(found)} emergent candidate(s) of {len(centroids)} clusters "
          f"(size >= {args.min_size}, purity <= {args.max_purity:g}) -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
