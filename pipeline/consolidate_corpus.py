#!/usr/bin/env python3
"""Consolidate every per-title signal into ONE inspectable JSONL — the dataset's source of truth.

  pipeline/consolidate_corpus.py \
      --combined out-repass/combined-v1-r2.jsonl \
      --combined out-repass/combined-v1-r2-token-fallback.jsonl \
      --combined out-repass/combined-v1-r2-token-fallback-2.jsonl \
      --delta out-repass/delta-v2.jsonl \
      --delta out-repass/delta-v2-rest.jsonl \
      --facts out-repass/facts-5b1c3213b6a1.json \
      --labels out-repass/genres-moods.json \
      --expect 47618 \
      --out out-repass/corpus-<version>.jsonl

`./den stage corpus --out-dir out-repass --expect 47618` runs the same join
with the same arguments, built from `pipeline/corpus.py`'s declaration rather than retyped — the shard
paths come from the declared glob, so the set cannot be short by one.

## A later run supersedes an earlier one, by key

A title re-grounded on a different article is classified and critiqued again into a NEW shard, beside
the one that answered it from the old article. When two shards of one pass answer the same key, the shard
whose run STARTED later wins: `runStartedAt` in its sidecar manifest. The pass writes that stamp once, when
it creates the manifest, and never rewrites it — a resume under a different configuration is refused
rather than re-stamped — so it is a recorded fact about the run, not a file mtime or a glob order. It is
also the right clock: a run's article dump is pinned when it starts, so the run that started later read
the newer article, however long either one took to finish.

Refused rather than guessed: a key twice in ONE shard (one run, two answers); a key in two shards where
either has no recorded start, or both started at the same instant. The report lists every shard with the
keys it took over from an earlier one (`supersedes`), the rows a later one took from it (`superseded`),
and the rows a tombstone withdrew.

## Withdrawing a title that lost its plot

A re-fetch can leave a title with no plot at all (a redirect that no longer counts, #64). Its old rows
were answered from an article it no longer has, and no new run will answer it, so nothing supersedes
them. `withdrawn.jsonl` in the out-dir says so, one title per line, written by

  pipeline/consolidate_corpus.py withdraw --keys A-to-plotless.txt --reason "…" \
      --out out-repass/withdrawn.jsonl

and passed to the join as `--withdrawn`. A tombstone removes the rows of every run that STARTED before
its `withdrawnAt`, so a title that later regains a plot and is answered again by a newer run comes back
without the tombstone being edited. The title itself stays in the corpus, with its facts and labels;
what goes is the judgements read from the wrong article. The file is append-only: a key is withdrawn
once.

## Classify and critique must have read the same article

Each row records the `articleSha256` it was answered from. Superseding lets a new classify shard be
folded in before its critique is, which would ship facets from one article beside a critique of another,
so the join refuses a kept critique row whose classify row is missing or read a different article.

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
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

FACET_AXES = ("era", "setting", "scope", "ending", "pacing", "chronology",
              "continuity", "conflict", "ensemble", "tone", "timespan", "archetype")
APPLICABILITY = ("validity", "narrative_applicability")
AUDIENCE = ("intended_to_frighten", "made_for_children", "made_for_teens")
# Any answer key holding source prose rather than a judgement. Belt and braces: the passes do not put
# article text under these names today, and if one ever does it must not reach a published file.
PROSE = ("evidence", "plot", "overview", "synopsis", "text", "sections", "article")


def key_of(record):
    return f"{record['mediaType']}:{record['tmdbId']}"


def timestamp(value, where):
    """An ISO-8601 instant with a zone. A naive one cannot be ordered against the pass's UTC stamps."""
    try:
        when = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        sys.exit(f"{where}: {value!r} is not an ISO-8601 timestamp")
    if when.tzinfo is None:
        sys.exit(f"{where}: {value!r} has no time zone")
    return when


def started_at(shard):
    """When the run that wrote `shard` started, from its sidecar manifest; None when it records none."""
    manifest = shard + ".manifest.json"
    if not os.path.exists(manifest):
        return None
    with open(manifest, encoding="utf-8") as fh:
        stamp = json.load(fh).get("runStartedAt")
    return None if stamp is None else timestamp(stamp, f"{manifest} runStartedAt")


def shard_order(paths):
    """`paths` oldest run first — the order a later shard supersedes an earlier one in. A shard with no
    recorded start sorts last, by path; it can only take part in a join where no other shard shares a
    key with it, so where it sorts changes nothing written."""
    started = {path: started_at(path) for path in paths}
    return sorted(paths, key=lambda p: (0, started[p], p) if started[p] else (1, p))


def latest(paths, label, withdrawals=None, keep=lambda record: record):
    """`(rows, report, withdrawn keys)`: the record that answers for each key across one pass's shards,
    under the supersede rule and the tombstones in the module docstring. `keep` trims a record to what
    the caller reads, so the whole pass is not held in memory.

    The shards are read oldest run first whatever order they were passed in, so what is kept and what
    the report counts depend only on the manifests.
    """
    ordered = shard_order(paths)
    started = {path: started_at(path) for path in ordered}
    report = {path: {"shard": os.path.basename(path),
                     "runStartedAt": started[path].isoformat() if started[path] else None,
                     "rows": 0, "supersedes": 0, "superseded": 0, "withdrawn": 0}
              for path in ordered}
    rows, owner = {}, {}
    for path in ordered:
        own = set()
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                record = json.loads(line)
                key = key_of(record)
                if key in own:
                    sys.exit(f"duplicate key within {label} shard {path}: {key} — one run answered it twice")
                own.add(key)
                report[path]["rows"] += 1
                earlier = owner.get(key)
                if earlier is not None:
                    if started[earlier] is None or started[path] is None:
                        undated = earlier if started[earlier] is None else path
                        sys.exit(f"duplicate key across {label} shards: {key} in {earlier} and {path}. A shard "
                                 f"supersedes another only by the runStartedAt its manifest records, and "
                                 f"{undated} records none")
                    if started[earlier] == started[path]:
                        sys.exit(f"duplicate key across {label} shards: {key} in {earlier} and {path}, whose "
                                 f"runs both started at {started[path].isoformat()}, so neither is later")
                    report[earlier]["superseded"] += 1
                    report[path]["supersedes"] += 1
                rows[key] = keep(record)
                owner[key] = path
    withdrawn, stood = set(), 0
    for key, when in (withdrawals or {}).items():
        path = owner.get(key)
        if path is None:
            continue
        if started[path] is None:
            sys.exit(f"{key} is withdrawn and answered in {path}, whose manifest records no runStartedAt, so "
                     f"nothing says whether that run read the article before or after the withdrawal")
        if started[path] < when:
            del rows[key]
            report[path]["withdrawn"] += 1
            withdrawn.add(key)
        else:
            stood += 1
    return rows, {"shards": list(report.values()), "withdrawn": len(withdrawn),
                  "answeredAfterWithdrawal": stood}, withdrawn


def read_withdrawals(path):
    """`key` → when it was withdrawn, from a tombstone file. A key withdrawn twice is refused: the file
    is append-only, and a second line for one title means two writers disagreed about it."""
    if not path:
        return {}
    out = {}
    with open(path, encoding="utf-8") as fh:
        for number, line in enumerate(fh, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            key = key_of(row)
            if key in out:
                sys.exit(f"{path}:{number}: {key} is withdrawn twice")
            if not row.get("reason"):
                sys.exit(f"{path}:{number}: {key} is withdrawn with no reason")
            out[key] = timestamp(row.get("withdrawnAt"), f"{path}:{number} withdrawnAt")
    return out


def read_keys(path):
    """`mediaType:tmdbId` keys, one per line, in file order; a malformed or repeated line is refused."""
    keys = []
    with open(path, encoding="utf-8") as fh:
        for number, line in enumerate(fh, 1):
            key = line.strip()
            if not key:
                continue
            media, _, tmdb = key.partition(":")
            if media not in ("movie", "tv") or not tmdb.isdigit():
                sys.exit(f"{path}:{number}: {key!r} is not a mediaType:tmdbId key")
            if key in keys:
                sys.exit(f"{path}:{number}: {key} is listed twice")
            keys.append(key)
    return keys


def withdraw(argv=None, now=None):
    """`consolidate_corpus.py withdraw`: append a tombstone for each listed key to the withdrawn file.

    Each line records why, when, and which key list it came from (name and digest), so a withdrawn
    title can be traced back to the re-fetch that decided it. A key already withdrawn is refused and
    nothing is written.
    """
    ap = argparse.ArgumentParser(prog="consolidate_corpus.py withdraw")
    ap.add_argument("--keys", required=True, help="the titles to withdraw, one mediaType:tmdbId per line")
    ap.add_argument("--reason", required=True, help="why their rows no longer stand, e.g. #64 redirect")
    ap.add_argument("--out", required=True, help="the out-dir's withdrawn.jsonl; appended to")
    args = ap.parse_args(argv)
    if not args.reason.strip():
        sys.exit("--reason is empty")
    keys = read_keys(args.keys)
    already = read_withdrawals(args.out) if os.path.exists(args.out) else {}
    again = [k for k in keys if k in already]
    if again:
        sys.exit(f"refusing: {len(again)} keys are already withdrawn in {args.out}, e.g. {again[:4]}")
    with open(args.keys, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    when = (now or datetime.now(timezone.utc)).isoformat()
    with open(args.out, "a", encoding="utf-8") as fh:
        for key in keys:
            media, tmdb = key.split(":")
            fh.write(json.dumps({"keysFile": os.path.basename(args.keys), "keysSha256": digest,
                                 "mediaType": media, "reason": args.reason, "tmdbId": int(tmdb),
                                 "withdrawnAt": when}, sort_keys=True) + "\n")
    print(json.dumps({"withdrawn": len(keys), "withdrawnAt": when, "out": args.out}, indent=1))
    return 0


def typed(answers, names):
    """The model's answer for each named question, whole — choice, probabilities, confidence."""
    return {n: answers[n] for n in names if isinstance(answers.get(n), dict)}


def prefixed(answers, prefix):
    return {k[len(prefix):]: v for k, v in answers.items()
            if k.startswith(prefix) and isinstance(v, dict)}


def by_key(path, label):
    """A labels artifact keyed by `media:tmdbId`.

    The wrapper key is NOT guessed. `labels-t02.json` nests under `records` and `genres-moods.json` under
    `titles`, and an earlier version of this function tried `labels`/`tags` and then fell through to the
    wrapper dict itself — which is a dict, so it was returned as if it were the rows. Every lookup then
    missed and every corpus row was written with `labels: null`, silently, for all 47,529 titles. Name the
    shapes, and fail on anything else rather than returning something dict-like.
    """
    if not path:
        return {}
    with open(path, encoding="utf-8") as fh:
        blob = json.load(fh)
    rows = None
    if isinstance(blob, dict):
        for key in ("records", "titles", "labels", "tags"):
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
INPUT_ARGS = ("combined", "delta", "facts", "labels", "withdrawn")


def build_parser():
    """The argument list, so a test can hold it against `INPUT_ARGS` and run a built command line
    through the parser that will receive it rather than against a copy of it."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--combined", required=True, action="append", help="a shard of the corpus pass")
    ap.add_argument("--delta", required=True, action="append", help="a shard of the delta pass")
    ap.add_argument("--facts", required=True, help="the FULL facts file, not facts-slim")
    ap.add_argument("--labels", required=True, help="genres-moods.json: each title's genres & moods")
    ap.add_argument("--withdrawn", help="tombstones: titles whose older pass rows no longer stand")
    ap.add_argument("--expect", type=int, default=None, help="required title count")
    ap.add_argument("--out", required=True, help="written gzipped when it ends .gz")
    return ap


def pass_row(record):
    """What the join reads from a pass row: its identity, its answers and the article it read."""
    return {"mediaType": record["mediaType"], "tmdbId": record["tmdbId"],
            "answers": record.get("answers") or {}, "articleSha256": record.get("articleSha256")}


def main():
    if sys.argv[1:2] == ["withdraw"]:
        return withdraw(sys.argv[2:])
    args = build_parser().parse_args()
    withdrawals = read_withdrawals(args.withdrawn)

    print("reading facts …", file=sys.stderr)
    with open(args.facts, encoding="utf-8") as fh:
        facts_blob = json.load(fh)
    entities = facts_blob.get("entities") or {}
    facts = {key_of(r): r for r in facts_blob["records"]}

    print("reading genres & moods …", file=sys.stderr)
    labels = by_key(args.labels, "labels")

    print("reading the passes …", file=sys.stderr)
    combined_rows, combined_report, combined_withdrawn = latest(args.combined, "combined", withdrawals,
                                                                keep=pass_row)
    delta_rows, delta_report, _ = latest(args.delta, "delta", withdrawals, keep=pass_row)
    unpaired = sorted(k for k, r in delta_rows.items()
                      if k not in combined_rows or combined_rows[k]["articleSha256"] != r["articleSha256"])
    if unpaired:
        sys.exit(f"{len(unpaired)} titles keep a critique row whose classify row is missing or read a "
                 f"different article, e.g. {unpaired[:4]} — fold a re-run in with both of its passes")
    delta = {k: r["answers"] for k, r in delta_rows.items()}

    print("joining …", file=sys.stderr)
    out_path = args.out
    opener = gzip.open if out_path.endswith(".gz") else open
    written, with_delta, with_facts, with_labels = 0, 0, 0, 0
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
    #
    # A withdrawn title keeps its place on the spine: it lost the judgements read from the wrong article,
    # not its facts or its labels, and `--expect` counts titles.
    answered = set(combined_rows) | combined_withdrawn
    spine = sorted(answered | set(facts))
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
    # worth seeing: a join that loses twenty thousand records still clears `written * 0.5` with room to
    # spare. The artifact's own count is the only number that knows how many there were meant to be.
    if with_labels != len(labels):
        sys.exit(f"labels: {len(labels) - with_labels} of {len(labels)} records in {args.labels} did not "
                 f"reach the corpus — the join is wrong, not the data")

    # The entity names the Q-ids refer to, beside the corpus rather than repeated 47,529 times in it.
    ents_path = out_path.replace(".jsonl", "-entities.json").replace(".gz", "") + (
        ".gz" if out_path.endswith(".gz") else "")
    with (gzip.GzipFile(filename="", mode="wb", fileobj=open(ents_path, "wb"), compresslevel=9, mtime=0)
          if ents_path.endswith(".gz") else open(ents_path, "w", encoding="utf-8")) as eh:
        blob = json.dumps(entities, ensure_ascii=False, sort_keys=True)
        eh.write(blob.encode("utf-8") if ents_path.endswith(".gz") else blob)

    print(json.dumps({"titles": written, "withFacts": with_facts, "withLabels": with_labels,
                      "withDelta": with_delta,
                      "withPass": len(combined_rows), "factsOnly": written - len(answered),
                      "entities": len(entities), "out": out_path, "entitiesOut": ents_path,
                      "tombstones": len(withdrawals), "combined": combined_report, "delta": delta_report},
                     indent=1))


if __name__ == "__main__":
    main()
