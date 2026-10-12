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
what goes is the judgements read from the wrong article. The file is append-only. A title that regains a
plot and later loses it again gets another, later tombstone; the latest one decides which answers stand.

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

## A published corpus as the base

The daily job rebuilds from what was published (oxyc/den-dataset#27): its out-dir holds only today's pass
shards, so `--base corpus-<ver>.jsonl.gz` supplies the rest. A title no shard answers takes its judgements
from its base row — the classify answers when no shard classified it, the critique answers when no shard
critiqued it either — and a tombstone withdraws them there as it does from a shard: every tombstone in a
rebuilt out-dir is newer than the base. Facts and genres & moods always come from this run's files. Every
title the base holds must reach the corpus: the published dataset is never shrunk by a join.

## Where each title's plot came from

`--enriched` adds `source` to each row: the newest enriched record's `SOURCE` fields and `plotSha256`, the
digest of its plot. It is what the next day's change set compares against (`pipeline/changes.py`) and what
the grounding census reads, so a run that starts from the published corpus knows what the live dataset
was built from without the batches. Identifiers and a digest only — never the plot. A title with no
enriched record keeps its base row's `source`.

**A title nothing has classified keeps its base row's `source` whatever the enriched batches say.** The
spine carries titles that have facts and nothing else (the library titles `fit.rs` judges, 119 of them in
b66e38e333af), and a catalogue pass can ground one of them in a Wikipedia plot on a day that buys no
classification. A plot is taken in together with its genres & moods and the vector built from them, never
before: the row would say "has a plot" over a store that has no plot vector for it, which the publish's
plot-vector gate refuses (oxyc/den-dataset#10). So the row stays as published until a run classifies the
title; one the base never recorded a source for is written with `hasWikiPlot: false` and no plot fields
(`held_source`), because the gate reads an absent source as "nothing said" and falls back to the enriched
tree. The next day's change set is read from this `source`, so that title is then `gainedPlot` or `added`
and the run that can buy picks it up, where a source carrying the plot would have filed it as already seen.

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
#: The enriched record's fields a row's `source` carries: which article, language and revision the plot was
#: read from, how it was chosen, and which Wikidata item answers for the title. Never `overview`.
SOURCE = ("hasWikiPlot", "plotArticle", "plotLanguage", "plotRevId", "plotArticleRole", "plotArticleRedirected",
          "wikidataItem", "wikidataCandidates", "animated")
#: What a base row contributes when no shard of this run answered its title: the classify answers, and the
#: critique answers.
BASE_COMBINED = ("applicability", "facets", "scores", "nouls")
BASE_DELTA = ("critique", "technique", "depicts", "audience")
BASE_STRUCTURAL = ("structural",)


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
    """`key` → its latest withdrawal, from the append-only tombstone file.

    A title can regain a plot, be answered by a later pass, then lose it again. Its next tombstone must be
    later than its last: accepting an older or equal stamp would make file order decide which answers ship."""
    if not path:
        return {}
    out = {}
    with open(path, encoding="utf-8") as fh:
        for number, line in enumerate(fh, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            key = key_of(row)
            if not row.get("reason"):
                sys.exit(f"{path}:{number}: {key} is withdrawn with no reason")
            when = timestamp(row.get("withdrawnAt"), f"{path}:{number} withdrawnAt")
            if key in out and when <= out[key]:
                sys.exit(f"{path}:{number}: {key} is withdrawn again at {when.isoformat()}, not after "
                         f"its earlier withdrawal at {out[key].isoformat()}")
            out[key] = when
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
    title can be traced back to the re-fetch that decided it. A key can be withdrawn again only at a
    strictly later instant, after it had a chance to regain a plot and receive newer answers.
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
    at = now or datetime.now(timezone.utc)
    again = [k for k in keys if k in already and at <= already[k]]
    if again:
        sys.exit(f"refusing: {len(again)} keys are not withdrawn later than their last tombstone in "
                 f"{args.out}, e.g. {again[:4]}")
    with open(args.keys, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    when = at.isoformat()
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


MATURITY_OBSERVATION_FIELDS = (
    "authority", "authorityId", "raw", "age", "matchedBy", "assessed", "runtimeMinutes", "format",
)
MATURITY_MATCH_METHODS = {"exact-title+year+director", "authority-id-decision"}


def maturity_rows(path):
    """Verified public-authority ages, reduced to the factual fields the corpus may publish.

    This is the publication boundary, so it verifies the producer's evidence instead of trusting a
    top-level number. Zero matched titles remains a valid fail-closed result.
    """
    with open(path, encoding="utf-8") as fh:
        blob = json.load(fh)
    rows = blob.get("records") if isinstance(blob, dict) else None
    if not isinstance(rows, list):
        sys.exit(f"maturity: {path} has no records list")
    out = {}
    for row in rows:
        if not isinstance(row, dict) or "mediaType" not in row or "tmdbId" not in row:
            sys.exit(f"maturity: {path} has an unkeyed record")
        media, ident, minimum = row["mediaType"], row["tmdbId"], row.get("minimumAge")
        observations = row.get("observations")
        if (media not in ("movie", "tv") or isinstance(ident, bool) or not isinstance(ident, int)
                or ident <= 0 or isinstance(minimum, bool)
                or (minimum is not None and (not isinstance(minimum, int) or not 0 <= minimum <= 18))
                or not isinstance(observations, list) or not observations):
            sys.exit(f"maturity: {path} has a malformed record for {media}:{ident}")
        key = key_of(row)
        if key in out:
            sys.exit(f"maturity: {path} has duplicate key {key}")
        safe = []
        for observation in observations:
            if not isinstance(observation, dict):
                sys.exit(f"maturity: {path} has a malformed observation for {key}")
            authority = observation.get("authority")
            authority_id = observation.get("authorityId")
            raw = observation.get("raw")
            age = observation.get("age")
            method = observation.get("matchedBy")
            if (not isinstance(authority, str) or not authority
                    or not isinstance(authority_id, str) or not authority_id
                    or not isinstance(raw, str) or not raw
                    or isinstance(age, bool)
                    or (age is not None and (not isinstance(age, int) or not 0 <= age <= 18))
                    or method not in MATURITY_MATCH_METHODS):
                sys.exit(f"maturity: {path} has no accepted authority evidence for {key}")
            safe.append({name: observation[name] for name in MATURITY_OBSERVATION_FIELDS
                         if name == "age" or observation.get(name) is not None})
        ages = [observation["age"] for observation in safe if observation.get("age") is not None]
        if minimum != (max(ages) if ages else None):
            sys.exit(f"maturity: {path} minimumAge is not the highest observation for {key}")
        out[key] = {"mediaType": media, "tmdbId": ident, "minimumAge": minimum,
                    "observations": safe}
    return out


#: The inputs this join reads, in the order the parser below declares them. `pipeline/corpus.py` builds
#: the command line out of its own declaration and `pipeline/corpus_test.py` holds the two lists
#: together, so an input can only be added or dropped in one place without something going red.
INPUT_ARGS = ("combined", "delta", "structural", "facts", "maturity", "labels", "withdrawn", "enriched", "base")


def build_parser():
    """The argument list, so a test can hold it against `INPUT_ARGS` and run a built command line
    through the parser that will receive it rather than against a copy of it."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--combined", action="append", default=[], help="a shard of the corpus pass")
    ap.add_argument("--delta", action="append", default=[], help="a shard of the delta pass")
    ap.add_argument("--structural", action="append", default=[], help="a shard of the structural pass")
    ap.add_argument("--facts", required=True, help="the FULL facts file, not facts-slim")
    ap.add_argument("--maturity", required=True, help="verified public-authority numeric maturity facts")
    ap.add_argument("--labels", required=True, help="genres-moods.json: each title's genres & moods")
    ap.add_argument("--withdrawn", help="tombstones: titles whose older pass rows no longer stand")
    ap.add_argument("--enriched", help="the enriched batch directory each row's `source` is read from")
    ap.add_argument("--base", help="the published corpus: the judgements of every title no shard answers")
    ap.add_argument("--expect", type=int, default=None, help="required title count")
    ap.add_argument("--out", required=True, help="written gzipped when it ends .gz")
    return ap


def plot_sha256(record):
    """The digest of a grounded record's plot, or its recorded one. Only when `hasWikiPlot`: an old batch's
    `overview` is TMDB's prose otherwise, and no digest of it is taken."""
    if not record.get("hasWikiPlot"):
        return None
    text = record.get("overview")
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if isinstance(text, str) else record.get("plotSha256")


def sources(directory):
    """`key -> source` from the newest enriched record per title — every reader's rule."""
    import re
    numbered = []
    for name in os.listdir(directory):
        found = re.match(r"^batch-([0-9]+)\.json$", name)
        if found:
            numbered.append((int(found.group(1)), name))
    out = {}
    for _number, name in sorted(numbered):
        with open(os.path.join(directory, name), encoding="utf-8") as fh:
            for record in json.load(fh):
                source = {k: record[k] for k in SOURCE if k in record}
                digest = plot_sha256(record)
                if digest:
                    source["plotSha256"] = digest
                out[key_of(record)] = source
    return out


#: The `source` fields that say a title has a plot, and where it came from.
PLOT_FIELDS = ("hasWikiPlot", "plotArticle", "plotLanguage", "plotRevId", "plotArticleRole",
               "plotArticleRedirected", "plotSha256")


#: The `source` field of a title held unclassified with no published source.
HELD_MARK = "heldForLabels"


def held_source(published, enriched):
    """The `source` of a title nothing has classified: the published one, or — when the base never recorded
    one — the newest enriched record's identifiers with the plot taken out and `hasWikiPlot` false, said
    outright. An absent source would read as "nothing recorded" and leave the plot-vector gate to the
    enriched tree alone; this row has to SAY it ships no plot.

    A source built here also says `heldForLabels`: the title was fetched and is waiting to be classified, so
    the next day's seed (`pipeline/published.py`) must not count it processed, or nothing would fetch it
    again and it would wait for its revisit slice."""
    if published:
        return published
    if not enriched:
        return None
    return {**{k: v for k, v in enriched.items() if k not in PLOT_FIELDS}, "hasWikiPlot": False,
            HELD_MARK: True}


def base_rows(path):
    """The published corpus, `key -> row`."""
    out = {}
    with gzip.open(path, "rt", encoding="utf-8") if path.endswith(".gz") else open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                row = json.loads(line)
                out[row["key"]] = row
    return out


def critiqued(answers):
    """Whether a classify row's answers include the delta pass's, asked in the same call (#187)."""
    return any(name.startswith("critique__") for name in answers)


def pass_row(record):
    """What the join reads from a pass row: its identity, its answers and the article it read."""
    return {"mediaType": record["mediaType"], "tmdbId": record["tmdbId"],
            "answers": record.get("answers") or {}, "articleSha256": record.get("articleSha256")}


def main():
    if sys.argv[1:2] == ["withdraw"]:
        return withdraw(sys.argv[2:])
    args = build_parser().parse_args()
    if not (args.combined and args.delta) and not args.base:
        sys.exit("a join with no --base needs --combined and --delta shards: without them no title has a judgement")
    withdrawals = read_withdrawals(args.withdrawn)
    base = base_rows(args.base) if args.base else {}
    source = sources(args.enriched) if args.enriched else {}

    print("reading facts …", file=sys.stderr)
    with open(args.facts, encoding="utf-8") as fh:
        facts_blob = json.load(fh)
    entities = facts_blob.get("entities") or {}
    facts = {key_of(r): r for r in facts_blob["records"]}

    print("reading maturity …", file=sys.stderr)
    maturity = maturity_rows(args.maturity)
    malformed = sorted(key for key, value in maturity.items()
                       if isinstance(value.get("minimumAge"), bool)
                       or (value.get("minimumAge") is not None
                           and (not isinstance(value["minimumAge"], int)
                                or not 0 <= value["minimumAge"] <= 18)))
    if malformed:
        sys.exit(f"maturity: {len(malformed)} records have neither null nor an integer minimumAge "
                 f"from 0 through 18, "
                 f"e.g. {malformed[:4]}")
    outside = sorted(set(maturity) - set(facts))
    if outside:
        sys.exit(f"maturity: {len(outside)} records name no title in the facts artifact, e.g. {outside[:4]}")

    print("reading genres & moods …", file=sys.stderr)
    labels = by_key(args.labels, "labels")

    print("reading the passes …", file=sys.stderr)
    combined_rows, combined_report, combined_withdrawn = latest(args.combined, "combined", withdrawals,
                                                                keep=pass_row)
    delta_rows, delta_report, _ = latest(args.delta, "delta", withdrawals, keep=pass_row)
    structural_rows, structural_report, _ = latest(
        args.structural, "structural", withdrawals, keep=pass_row)
    # A classify row bought with the critique questions (#187) carries its own critique, read from its own
    # article; an older delta row read from another article is superseded by it, as an older classify row is.
    for key, row in combined_rows.items():
        if critiqued(row["answers"]) and key in delta_rows \
                and delta_rows[key]["articleSha256"] != row["articleSha256"]:
            del delta_rows[key]
    unpaired = sorted(k for rows in (delta_rows, structural_rows) for k, r in rows.items()
                      if k not in combined_rows or combined_rows[k]["articleSha256"] != r["articleSha256"])
    if unpaired:
        sys.exit(f"{len(unpaired)} titles keep a derived-pass row whose classify row is missing or read a "
                 f"different article, e.g. {unpaired[:4]} — fold a re-run in with both of its passes")
    delta = {k: r["answers"] for k, r in delta_rows.items()}
    structural = {k: r["answers"] for k, r in structural_rows.items()}

    print("joining …", file=sys.stderr)
    out_path = args.out
    opener = gzip.open if out_path.endswith(".gz") else open
    written, with_delta, with_structural, with_facts, with_maturity, with_labels = 0, 0, 0, 0, 0, 0
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
    left = sorted(set(base) - answered - set(facts))
    if left:
        sys.exit(f"{len(left)} titles the published corpus holds would leave it — they are in no facts file "
                 f"and no shard, e.g. {left[:4]}. The facts' delta pass keeps every published title.")
    # A title this run classified that the day did not finalize — classify judged its article about another
    # work, so it has no genres & moods, no vector and no facts — has nothing the store can carry. A published
    # title is in the facts by the delta pass, so this is only ever a new one: it waits, its answers kept in
    # the paid-answers ledger, until a run can finalize it, as a title held for spend does.
    unfinalized = sorted(answered - set(facts) - set(base))
    if unfinalized:
        print(f"waiting: {len(unfinalized)} new titles were classified but not finalized (no genres & moods), "
              f"e.g. {unfinalized[:4]}", file=sys.stderr)
    spine = sorted((answered | set(facts) | set(base)) - set(unfinalized))
    from_base = held = 0
    try:
        for key in spine:
            record = combined_rows.get(key)
            answers = (record or {}).get("answers") or {}
            media, tmdb = key.split(":", 1)
            record = record or {"mediaType": media, "tmdbId": int(tmdb)}
            leaked = [k for k in answers if any(p in k.lower() for p in PROSE)]

            if leaked:
                sys.exit(f"{key}: refusing to write source prose ({', '.join(sorted(leaked))})")
            d = delta.get(key) or (answers if critiqued(answers) else {})
            s = structural.get(key, {})
            fact = facts.get(key)
            # No shard answered this title, and no tombstone took its answers: the published ones stand.
            prior = base.get(key) if key not in combined_rows and key not in withdrawals else None
            row = {
                "key": key,
                "mediaType": record["mediaType"],
                "tmdbId": record["tmdbId"],
                # Wikidata, CC0. The full record — composers, cinematographers, narrativeLocations and
                # mainSubjects included, all of which facts-slim dropped.
                "facts": {k: v for k, v in (fact or {}).items() if k not in ("mediaType", "tmdbId")},
                "maturity": {k: v for k, v in (maturity.get(key) or {}).items()
                             if k not in ("mediaType", "tmdbId")} or None,
                "labels": labels.get(key),
                "applicability": typed(answers, APPLICABILITY),
                "facets": typed(answers, FACET_AXES),
                "scores": prefixed(answers, "score__"),
                "nouls": prefixed(answers, "tax__"),
                "critique": prefixed(d, "critique__"),
                "technique": prefixed(d, "technique__"),
                "depicts": prefixed(d, "depicts__"),
                "structural": prefixed(s, "structural__"),
                "audience": typed(d, AUDIENCE),
            }
            if prior is not None:
                row.update({
                    name: prior.get(name) or {}
                    for name in BASE_COMBINED + BASE_DELTA + BASE_STRUCTURAL
                })
                from_base += 1
            # Nothing has classified this title: no shard this run, no genres & moods, no published judgement.
            unclassified = (key not in combined_rows and not row["labels"]
                            and not any((base.get(key) or {}).get(name) for name in BASE_COMBINED))
            if unclassified:
                found = held_source((base.get(key) or {}).get("source"), source.get(key))
                held += 1 if (source.get(key) or {}).get("hasWikiPlot") and not (found or {}).get("hasWikiPlot") \
                    else 0
            else:
                found = source.get(key) or (base.get(key) or {}).get("source")
            if found:
                row["source"] = found
            line = json.dumps(row, ensure_ascii=False, sort_keys=True)
            handle.write((line + "\n").encode("utf-8") if out_path.endswith(".gz") else line + "\n")
            written += 1
            with_delta += 1 if d else 0
            with_structural += 1 if row["structural"] else 0
            with_facts += 1 if fact else 0
            with_maturity += 1 if row["maturity"] else 0
            with_labels += 1 if row["labels"] else 0
    finally:
        handle.close()

    if args.expect is not None and written != args.expect:
        sys.exit(f"expected {args.expect} titles, wrote {written} — a shard is missing")

    # Every facts record must appear. The count is the union, so a facts record that never made it is a
    # bug in the join, not a corpus that legitimately lacks it.
    if with_facts != len(facts):
        sys.exit(f"{len(facts) - with_facts} facts records did not reach the corpus")
    if with_maturity != len(maturity):
        sys.exit(f"maturity: {len(maturity) - with_maturity} of {len(maturity)} records in {args.maturity} "
                 f"did not reach the corpus")

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

    print(json.dumps({"titles": written, "withFacts": with_facts, "withMaturity": with_maturity,
                      "withLabels": with_labels,
                      "withDelta": with_delta, "withStructural": with_structural,
                      "withPass": len(combined_rows), "fromBase": from_base, "heldAtPublishedSource": held,
                      "factsOnly": written - (len(answered) - len(unfinalized)) - from_base,
                      "unfinalized": len(unfinalized),
                      "entities": len(entities), "out": out_path, "entitiesOut": ents_path,
                      "tombstones": len(withdrawals), "combined": combined_report, "delta": delta_report,
                      "structural": structural_report},
                     indent=1))


if __name__ == "__main__":
    main()
