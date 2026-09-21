#!/usr/bin/env python3
"""Consolidate every per-title signal into ONE inspectable JSONL — the dataset's source of truth.

  scripts/v2/consolidate_corpus.py \
      --combined out-repass/combined-v1-r2.jsonl \
      --combined out-repass/combined-v1-r2-token-fallback.jsonl \
      --combined out-repass/combined-v1-r2-token-fallback-2.jsonl \
      --delta out-repass/delta-v1.jsonl \
      --facts out-repass/facts-5b1c3213b6a1.json \
      --labels out-repass/labels-t02.json \
      --premise-labels out-repass/labels-premise.json \
      --expect 47618 \
      --out out-repass/corpus-<version>.jsonl

`./den stage corpus --out-dir out-repass --dataset-version <version> --expect 47618` runs the same join
with the same arguments, built from `pipeline/corpus.py`'s declaration rather than retyped — the shard
paths come from the declared glob, so the set cannot be short by one.

## Why this exists

The signals were spread across ten artifacts keyed the same way, and nothing checked they agreed. A title
could sit in one and not another with no error anywhere: eleven of them — House of the Dragon and Moon
Knight among them — were absent from `rail-facets` for a day because the producer read one shard of a
three-shard bundle. One file per title, built once and audited, makes that a contradiction rather than a
silence.

This file is the SOURCE OF TRUTH, meant to be read by a human. The binary store den-atlas loads is a build
artifact generated from it: regenerable, never hand-edited, never the thing you debug against. Inspect a
title with `zcat corpus-<version>.jsonl.gz | grep '"key":"tv:1399"' | python3 -m json.tool`.

## What is deliberately NOT here

TMDB overviews and Wikipedia article or section text. The derived judgements about that prose are ours to
publish; the prose is not (TMDb §1.C, and the Wikipedia text the pass read). Every field below is either a
CC0 Wikidata fact or a model judgement — never the source material. `--allow-prose` does not exist on
purpose: there is no flag that makes redistributing it acceptable.
"""
import argparse
import gzip
import json
import sys

FACET_AXES = ("era", "setting", "scope", "ending", "pacing", "chronology",
              "continuity", "conflict", "ensemble", "tone", "timespan", "archetype")
APPLICABILITY = ("validity", "narrative_applicability")
AUDIENCE = ("intended_to_frighten", "made_for_children", "made_for_teens")
# Any answer key holding source prose rather than a judgement. Belt and braces: the passes do not put
# article text under these names today, and if one ever does it must not reach a published file.
PROSE = ("evidence", "plot", "overview", "synopsis", "text", "sections", "article")


def key_of(record):
    return f"{record['mediaType']}:{record['tmdbId']}"


def records(paths, label, expect_disjoint=True):
    """Every record across the shards of one pass, refusing a duplicate key."""
    seen = set()
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                record = json.loads(line)
                key = key_of(record)
                if expect_disjoint and key in seen:
                    sys.exit(f"duplicate key across {label} shards: {key}")
                seen.add(key)
                yield key, record


def typed(answers, names):
    """The model's answer for each named question, whole — choice, probabilities, confidence."""
    return {n: answers[n] for n in names if isinstance(answers.get(n), dict)}


def prefixed(answers, prefix):
    return {k[len(prefix):]: v for k, v in answers.items()
            if k.startswith(prefix) and isinstance(v, dict)}


def by_key(path, label):
    """A labels artifact keyed by `media:tmdbId`.

    The wrapper key is NOT guessed. `labels-t02.json` nests under `records`, and an earlier version of
    this function tried `labels`/`tags` and then fell through to the wrapper dict itself — which is a
    dict, so it was returned as if it were the rows. Every lookup then missed and every corpus row was
    written with `labels: null`, silently, for all 47,529 titles. Name the shapes, and fail on anything
    else rather than returning something dict-like.
    """
    if not path:
        return {}
    with open(path, encoding="utf-8") as fh:
        blob = json.load(fh)
    rows = None
    if isinstance(blob, dict):
        for key in ("records", "labels", "tags"):
            if isinstance(blob.get(key), (list, dict)):
                rows = blob[key]
                break
        else:
            # A bare mapping of key -> row is legitimate; a wrapper with no known rows key is not.
            rows = blob if all(":" in k for k in list(blob)[:8]) else None
    else:
        rows = blob
    if rows is None:
        sys.exit(f"{label}: {path} has no recognisable rows (keys: {sorted(blob)[:6]})")
    if isinstance(rows, dict):
        return rows
    out = {key_of(r): r for r in rows if isinstance(r, dict) and "tmdbId" in r}
    if not out:
        sys.exit(f"{label}: {path} produced no keyed rows")
    return out


#: The inputs this join reads, in the order the parser below declares them. `pipeline/corpus.py` builds
#: the command line out of its own declaration and `pipeline/corpus_test.py` holds the two lists
#: together, so an input can only be added or dropped in one place without something going red.
INPUT_ARGS = ("combined", "delta", "facts", "labels", "premise_labels")


def build_parser():
    """The argument list, so a test can hold it against `INPUT_ARGS` and run a built command line
    through the parser that will receive it rather than against a copy of it."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--combined", required=True, action="append", help="a shard of the corpus pass")
    ap.add_argument("--delta", required=True, action="append", help="a shard of the delta pass")
    ap.add_argument("--facts", required=True, help="the FULL facts file, not facts-slim")
    ap.add_argument("--labels", required=True)
    ap.add_argument("--premise-labels")
    ap.add_argument("--expect", type=int, default=None, help="required title count")
    ap.add_argument("--out", required=True, help="written gzipped when it ends .gz")
    return ap


def main():
    args = build_parser().parse_args()

    print("reading facts …", file=sys.stderr)
    with open(args.facts, encoding="utf-8") as fh:
        facts_blob = json.load(fh)
    entities = facts_blob.get("entities") or {}
    facts = {key_of(r): r for r in facts_blob["records"]}

    print("reading labels …", file=sys.stderr)
    labels = by_key(args.labels, "labels")
    premise = by_key(args.premise_labels, "labels")

    print("reading the delta pass …", file=sys.stderr)
    delta = {k: (r.get("answers") or {}) for k, r in records(args.delta, "delta")}

    print("joining …", file=sys.stderr)
    out_path = args.out
    opener = gzip.open if out_path.endswith(".gz") else open
    written, with_delta, with_facts, with_labels, with_premise = 0, 0, 0, 0, 0
    # mtime=0 so an unchanged corpus produces byte-identical output and the publish step does not
    # re-upload an asset that did not change.
    handle = (gzip.GzipFile(filename="", mode="wb", fileobj=open(out_path, "wb"), compresslevel=9, mtime=0)
              if out_path.endswith(".gz") else open(out_path, "w", encoding="utf-8"))
    # The spine is the union of the facts and the combined pass, NOT the combined pass alone. 89 titles
    # carry facts and no pass row at all, and they are exactly the records that nothing else covers: no
    # labels, no vectors, no facets. `fit.rs` reads them to judge library titles that are not in the
    # index, and publish-dataset.sh's record-count guard exists because a facts rebuild once dropped 137
    # of them — "the only symptom was /recommend quietly losing library titles". Iterating the pass
    # silently omitted all 89, and a coverage guard against labelsRecords reads 99.98% and passes.
    combined_rows = {key: record for key, record in records(args.combined, "combined")}
    spine = sorted(set(combined_rows) | set(facts))
    try:
        for key in spine:
            record = combined_rows.get(key)
            answers = (record or {}).get("answers") or {}
            media, tmdb = key.split(":", 1)
            record = record or {"mediaType": media, "tmdbId": int(tmdb)}
            leaked = [k for k in answers if any(p in k.lower() for p in PROSE)]
            if leaked:
                sys.exit(f"{key}: refusing to write source prose ({', '.join(sorted(leaked))})")
            d = delta.get(key, {})
            fact = facts.get(key)
            row = {
                "key": key,
                "mediaType": record["mediaType"],
                "tmdbId": record["tmdbId"],
                # Wikidata, CC0. The full record — composers, cinematographers, narrativeLocations and
                # mainSubjects included, all of which facts-slim dropped.
                "facts": {k: v for k, v in (fact or {}).items() if k not in ("mediaType", "tmdbId")},
                "labels": labels.get(key),
                "premiseLabels": premise.get(key),
                "applicability": typed(answers, APPLICABILITY),
                "facets": typed(answers, FACET_AXES),
                "scores": prefixed(answers, "score__"),
                "nouls": prefixed(answers, "tax__"),
                "critique": prefixed(d, "critique__"),
                "technique": prefixed(d, "technique__"),
                "depicts": prefixed(d, "depicts__"),
                "audience": typed(d, AUDIENCE),
            }
            line = json.dumps(row, ensure_ascii=False, sort_keys=True)
            handle.write((line + "\n").encode("utf-8") if out_path.endswith(".gz") else line + "\n")
            written += 1
            with_delta += 1 if d else 0
            with_facts += 1 if fact else 0
            with_labels += 1 if row["labels"] else 0
            with_premise += 1 if row["premiseLabels"] else 0
    finally:
        handle.close()

    if args.expect is not None and written != args.expect:
        sys.exit(f"expected {args.expect} titles, wrote {written} — a shard is missing")

    # Every facts record must appear. The count is the union, so a facts record that never made it is a
    # bug in the join, not a corpus that legitimately lacks it.
    if with_facts != len(facts):
        sys.exit(f"{len(facts) - with_facts} facts records did not reach the corpus")

    # A join that silently misses is how `labels` was null on all 47,529 rows: the lookup returned a dict
    # for every key and none of them matched.
    #
    # Checked the way the facts are checked above — every record in the artifact must reach the corpus —
    # rather than against a fraction of the rows written. A floor of half the corpus cannot see the case
    # worth seeing: the premise pass covers 44,531 of 47,618 rows, so it could lose twenty thousand
    # records and still clear `written * 0.5` with room to spare. The artifact's own count is the only
    # number that knows how many there were meant to be.
    for name, hits, source, artifact in (("labels", with_labels, args.labels, labels),
                                         ("premiseLabels", with_premise, args.premise_labels, premise)):
        if source and hits != len(artifact):
            sys.exit(f"{name}: {len(artifact) - hits} of {len(artifact)} records in {source} did not "
                     f"reach the corpus — the join is wrong, not the data")

    # The entity names the Q-ids refer to, beside the corpus rather than repeated 47,529 times in it.
    ents_path = out_path.replace(".jsonl", "-entities.json").replace(".gz", "") + (
        ".gz" if out_path.endswith(".gz") else "")
    with (gzip.GzipFile(filename="", mode="wb", fileobj=open(ents_path, "wb"), compresslevel=9, mtime=0)
          if ents_path.endswith(".gz") else open(ents_path, "w", encoding="utf-8")) as eh:
        blob = json.dumps(entities, ensure_ascii=False, sort_keys=True)
        eh.write(blob.encode("utf-8") if ents_path.endswith(".gz") else blob)

    print(json.dumps({"titles": written, "withFacts": with_facts, "withLabels": with_labels,
                      "withPremiseLabels": with_premise, "withDelta": with_delta,
                      "withPass": len(combined_rows), "factsOnly": written - len(combined_rows),
                      "entities": len(entities), "out": out_path, "entitiesOut": ents_path}, indent=1))


if __name__ == "__main__":
    main()
