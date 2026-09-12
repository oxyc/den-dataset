#!/usr/bin/env python3
"""Validate Haiku vote passes before `assemble` consumes them.

Every check here exists because its absence shipped, or nearly shipped, bad labels:

  fabrication  a run invented tmdbIds in 3 of 12 batches WITH correct row counts. A fabricated id attaches
               one title's labels to another and no count or checksum can see it, so a batch carrying one
               is failed whole rather than partially salvaged.
  density      11 of 18 batches once came back valid, complete, in-vocabulary — and 46-70% of titles with
               no subgenre at all, against a careful run's 1.77 per title. Structurally perfect, worthless.
               Only a title appearing in two batches with different answers exposed it.
  vocabulary   the three lists are separate and get confused predictably: primary genres (Adventure,
               Mystery) used as subgenres, subgenres (Dark Comedy) filed under moods.

Usage:
    scripts/check-votes.py out-t02                 # every batch that has a pass1
    scripts/check-votes.py out-t02 --batch 156     # one batch
    scripts/check-votes.py out-t02 --min-density 1.2 --max-empty 0.25
"""
import argparse, glob, json, os, sys

REFERENCE = "a careful reference run averages 1.77 subgenres and 2.09 moods per title"


def vocabulary(out_dir):
    """The taxonomy as SHIPPED, not a hardcoded copy that can drift from it."""
    labels = sorted(glob.glob(os.path.join(out_dir, "labels-t*.json")))
    if not labels:
        sys.exit(f"no labels-t*.json in {out_dir} — cannot check labels against the taxonomy")
    records = json.load(open(labels[-1]))["records"]
    return (
        {r["primaryGenre"] for r in records if r.get("primaryGenre")},
        {s["label"] for r in records for s in r.get("subgenres", [])},
        {m["label"] for r in records for m in r.get("moods", [])},
    )


def check(out_dir, batch_id, primary, subs, moods, min_density, max_empty):
    enriched = json.load(open(os.path.join(out_dir, "enriched", f"batch-{batch_id}.json")))
    passes = sorted(glob.glob(os.path.join(out_dir, "votes", f"batch-{batch_id}-pass*.json")))
    if not passes:
        return [f"no vote passes for batch {batch_id}"]

    # Two different populations, and conflating them produces nonsense in both directions.
    # FABRICATION is judged against every id in the batch — a pass may legitimately carry votes for titles
    # that were later dropped for having no plot.
    # MISSING is judged only against plot-bearing titles — the rest are dropped by
    # `assemble --require-wiki-plot` under the TMDB ToS rule, so their absence is correct.
    known = {r["tmdbId"] for r in enriched}
    want = [r["tmdbId"] for r in enriched if r.get("hasWikiPlot")]
    problems = []
    if len({r["mediaType"] for r in enriched}) > 1:
        problems.append("batch mixes movie and tv — vote records carry a bare tmdbId and the id namespaces "
                        "overlap, so a vote can attach to the wrong title")

    for path in passes:
        try:
            got = json.load(open(path))
        except Exception as exc:
            problems.append(f"{os.path.basename(path)}: unparseable ({exc})")
            continue

        ids = [r.get("tmdbId") for r in got]
        invented = set(ids) - known
        if invented:
            problems.append(f"{os.path.basename(path)}: FABRICATED ids {sorted(invented)[:5]} — "
                            "these attach one title's labels to another; reject the whole pass")
        missing = set(want) - set(ids)
        if missing:
            problems.append(f"{os.path.basename(path)}: {len(missing)} of {len(want)} plot-bearing titles "
                            "have no vote — they will be silently dropped from the index")

        bad = []
        for r in got:
            if r.get("primary_genre") and r["primary_genre"] not in primary:
                bad.append(f"primary:{r['primary_genre']}")
            bad += [f"sub:{s.get('label')}" for s in r.get("subgenres") or [] if s.get("label") not in subs]
            bad += [f"mood:{m.get('label')}" for m in r.get("moods") or [] if m.get("label") not in moods]
        if bad:
            problems.append(f"{os.path.basename(path)}: {len(bad)} out-of-vocabulary "
                            f"({', '.join(sorted(set(bad))[:4])})")

        # Density is measured over the titles this pass actually labelled.
        scored = [r for r in got if r.get("tmdbId") in set(want)] or got
        if scored:
            density = sum(len(r.get("subgenres") or []) for r in scored) / len(scored)
            empty = sum(1 for r in scored if not r.get("subgenres")) / len(scored)
            if density < min_density or empty > max_empty:
                problems.append(f"{os.path.basename(path)}: THIN — {density:.2f} subgenres/title, "
                                f"{empty:.0%} with none ({REFERENCE}). Structurally valid batches at this "
                                "density are near-empty filler; re-run rather than ship them")
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    ap.add_argument("--batch", type=int)
    ap.add_argument("--min-density", type=float, default=1.2)
    ap.add_argument("--max-empty", type=float, default=0.25)
    args = ap.parse_args()

    primary, subs, moods = vocabulary(args.out_dir)
    if args.batch:
        ids = [args.batch]
    else:
        ids = sorted(int(os.path.basename(p).split("-")[1].split("-pass")[0])
                     for p in glob.glob(os.path.join(args.out_dir, "votes", "batch-*-pass1.json")))
    if not ids:
        sys.exit("no vote passes found")

    failed = 0
    for batch_id in ids:
        problems = check(args.out_dir, batch_id, primary, subs, moods, args.min_density, args.max_empty)
        if problems:
            failed += 1
            print(f"batch-{batch_id}:")
            for p in problems:
                print(f"    {p}")
    print(f"\n{len(ids) - failed}/{len(ids)} batches OK")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
