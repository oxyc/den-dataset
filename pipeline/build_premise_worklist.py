#!/usr/bin/env python3
"""The premise-generation worklist, and the evidence each title is judged on.

  pipeline/build_premise_worklist.py --combined out-repass/combined-v1-r2.jsonl \
      --articles out-repass/articles.jsonl --out-dir out-premise-v2

  # An increment: only newly admitted/regained titles, with an explicit token ceiling.
  pipeline/build_premise_worklist.py --combined out/combined-v1-r2.jsonl \
      --articles out/articles.jsonl --changes out/changes/plan.json \
      --token-ceiling 50000 --out-dir out/premise-increment

Premise tags are free-form strings, so they are the one artifact a decision-only model cannot produce and
the only remaining paid step in this corpus rebuild. That makes it worth spending care on *which* titles are
sent and *what* they are shown, because both decide the bill and neither is recoverable after the fact.
An incremental run takes admission from `changes/plan.json`; Wikipedia prose drift is deliberately not an
admission reason and cannot enter its worklist.

## Which titles

Not the raw coverage gap. The Jev pass already judged every article, so the gap is filtered by what it found:

- a title that already has tags is skipped — BOTH committed tag files, `data/premise-tags-v1.json` and
  `data/premise-tags-v2.json`, plus `out-premise-999/tags.json` when that gitignored directory happens to
  be present. It used to be v1 plus that directory, required, on the belief that its 999 results existed
  nowhere else; they are all in v2 (verified, 0 missing), so the hard exit is gone. Reading v2 as well is
  not a no-op: it skips 5,999 more titles than v1 ∪ the extras did, which is correct — v2 is the complete
  run — but it is a real change to the worklist, not just a portability fix (den-dataset#13).
- `validity` must be `correct-screen-work`. A title grounded on the source novel would otherwise get premise
  tags describing the book — which is how six tmdbIds came to share Wuthering Heights (#16).
- `narrative_applicability` must not be a non-narrative program. A talk or game show has no premise, and
  asking for one invents it.
- the article must carry at least one section Jev classified `story-premise`. Without that the prompt would
  be shown production and reception prose and would tag the making of the film.

Titles that pass every test but the last are written to a separate review queue rather than dropped or sent:
the lead may still carry a premise, and that is a judgement for a person, not a default.

## What they are shown

Only the `story-premise` sections, plus `theme-subject` when present — never the whole article. This is the
opposite of the classification pass, which reads everything so it can judge what the article IS. A generator
shown a Reception section will write tags about reviews.

## The 219 that need no model at all

`vectorsMissingFor` names titles that have tags and no vector, because the DT-N merge never ran. They are
emitted as their own list: regenerating them would pay twice for text already on disk.
"""
import argparse
import hashlib
import json
import os
import sys

ROLE_KEEP = ("story-premise", "theme-subject")


def digest(path):
    """SHA-256 of an input, so a generated row can be traced to immutable evidence."""
    hashed = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hashed.update(chunk)
    return hashed.hexdigest()


def title_key(row):
    return (row["mediaType"] != "movie", int(row["tmdbId"]))


def incremental_keys(path):
    """Newly admitted or legitimately regained titles from a published-baseline change plan.

    In particular, `gainedPlot`, `plot`, `article`, `item` and weekly revisit reasons are excluded. A
    source edit is not admission to the corpus and must never turn into a premise-generation bill.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            plan = json.load(handle)
    except (OSError, ValueError) as error:
        sys.exit(f"{path} is not a readable changes plan ({error})")
    if not isinstance(plan, dict) or not isinstance(plan.get("baseline"), dict):
        sys.exit(f"{path} has no published baseline; refusing to interpret a full generation as new titles")
    added, changed = plan.get("added"), plan.get("changed")
    if not isinstance(added, list) or not isinstance(changed, dict):
        sys.exit(f"{path} has no added/changed key sets")
    keys = set(added)
    keys.update(key for key, reasons in changed.items()
                if isinstance(reasons, list) and "regained" in reasons)
    for key in keys:
        media, separator, ident = key.partition(":") if isinstance(key, str) else ("", "", "")
        if separator != ":" or media not in ("movie", "tv") or not (ident.isascii() and ident.isdigit()):
            sys.exit(f"{path} carries an invalid title key: {key!r}")
    return keys


def keys_of(doc):
    """The `mediaType:tmdbId` keys of a tags file, in either shape it is written in."""
    if isinstance(doc, dict):
        return set(doc.get("tags", doc))
    return {f"{r['mediaType']}:{r['tmdbId']}" for r in doc}


def load_have(root, extra_tags=()):
    """Every title that already has premise tags.

    Both COMMITTED tag files, plus `out-premise-999/tags.json` when it happens to be there.

    That last one used to be required, with a hard exit calling it 999 results that exist nowhere else —
    so a fresh checkout of this repo could not run this script at all. It is not true any more and may
    never have been: all 999 of its keys are present in `data/premise-tags-v2.json`, which is committed.
    Refusing to run over a file that is gitignored, unreproducible and redundant is three problems, and
    the redundancy is the one that makes it safe to drop.
    """
    have = set()
    for name in ("data/premise-tags-v1.json", "data/premise-tags-v2.json"):
        path = os.path.join(root, name)
        if not os.path.exists(path):
            sys.exit(f"{name} is missing — it is committed, so this is a broken checkout, not a stale one")
        with open(path, encoding="utf-8") as fh:
            have |= keys_of(json.load(fh))
    for path in extra_tags:
        if not os.path.exists(path):
            sys.exit(f"{path} is missing — an explicitly named premise-tags input cannot be skipped")
        with open(path, encoding="utf-8") as fh:
            have |= keys_of(json.load(fh))
    extra_path = os.path.join(root, "out-premise-999/tags.json")
    if os.path.exists(extra_path):
        with open(extra_path, encoding="utf-8") as fh:
            have |= keys_of(json.load(fh))
    return have


ap = argparse.ArgumentParser()
# The bundle is every shard, not combined-v1-r2.jsonl alone: oversized articles run in separately
# manifested capacity shards (combined-v1-r2-token-fallback*.jsonl). Reading one shard silently
# leaves its titles off the worklist for good, which is how eleven of them — House of the Dragon
# and Moon Knight among them — ended up absent from a derived artifact. Repeat once per shard.
ap.add_argument("--combined", required=True, action="append",
                help="a shard of the Jev bundle; repeat for each (incl. the token-fallback shards)")
ap.add_argument("--articles", required=True, help="the articles stage's output, for the section text")
ap.add_argument("--out-dir", required=True)
ap.add_argument("--changes", help="changes/plan.json; restrict generation to added and regained titles")
ap.add_argument("--token-ceiling", type=int,
                help="with --changes, explicit maximum estimated input + output tokens for this worklist")
ap.add_argument("--existing-tags", action="append", default=[],
                help="an additional durable premise-tags file, such as the live bundle's copy")
args = ap.parse_args()

if bool(args.changes) != bool(args.token_ceiling):
    sys.exit("--changes and a positive --token-ceiling are required together")
if args.token_ceiling is not None and args.token_ceiling < 1:
    sys.exit("--token-ceiling must be positive")

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
have = load_have(root, args.existing_tags)
eligible = incremental_keys(args.changes) if args.changes else None
os.makedirs(args.out_dir, exist_ok=True)

# The article text, keyed for section slicing. One pass, kept as offsets rather than copies.
text = {}
with open(args.articles, encoding="utf-8") as fh:
    for line in fh:
        r = json.loads(line)
        text[f"{r['mediaType']}:{r['tmdbId']}"] = r["text"]

def bundle(paths):
    """Every record across the shards of the Jev bundle."""
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    yield json.loads(line)


def started(path):
    """When the run that wrote a shard started, from its manifest; a shard with none sorts first."""
    try:
        with open(path + ".manifest.json", encoding="utf-8") as fh:
            return json.load(fh).get("runStartedAt") or ""
    except OSError:
        return ""


def increment(paths, eligible):
    """An increment's eligible records, the newest run's row for a title two shards hold: a title classified
    again on a changed article sits beside the row it supersedes (as `consolidate_corpus` reads them), which a
    kept paid-answers ledger (`pipeline/paid.py`) makes ordinary rather than an error."""
    latest = {}
    for record in bundle(sorted(paths, key=started)):
        key = f"{record['mediaType']}:{record['tmdbId']}"
        if key in eligible:
            latest[key] = record
    return latest.values()


work, review, seen = [], [], set()
skipped = {"hasTags": 0, "badValidity": 0, "nonNarrative": 0, "noArticleText": 0}
# Why each title that is not in the worklist was turned away, with the article its classify row read: a
# caller can tell a verdict on that classification (badValidity, nonNarrative, review) from a title that only
# waits (noArticleText, articleChanged).
skipped_keys = {}
for r in (bundle(args.combined) if eligible is None else increment(args.combined, eligible)):
    key = f"{r['mediaType']}:{r['tmdbId']}"
    if key in seen:
        sys.exit(f"{key} occurs in more than one combined shard; refusing an ambiguous source record")
    seen.add(key)
    if key in have:
        skipped["hasTags"] += 1
        continue
    why = None
    answers = r.get("answers") or {}
    applic = (answers.get("narrative_applicability") or {}).get("choice")
    body = text.get(key)
    if (answers.get("validity") or {}).get("choice") != "correct-screen-work":
        why = "badValidity"
    elif applic in ("non-narrative-program", "documentary-or-factual"):
        why = "nonNarrative"
    elif body is None:
        why = "noArticleText"
    # The sections are offsets into the article the classify pass read. A title waiting since an earlier day
    # has its article fetched again (`changes/waiting.txt`), and an edited one would be cut at the wrong
    # places, so it waits until it is classified again.
    elif r.get("articleSha256") and r["articleSha256"] != hashlib.sha256(body.encode("utf-8")).hexdigest():
        why = "articleChanged"
    if why:
        skipped[why] = skipped.get(why, 0) + 1
        skipped_keys[key] = {"reason": why, "articleSha256": r.get("articleSha256")}
        continue

    kept = [s for s in r.get("sections", [])
            if ((s.get("role") or {}).get("value") in ROLE_KEEP)]
    premise_only = [s for s in kept if (s.get("role") or {}).get("value") == "story-premise"]
    evidence = "\n\n".join(
        f"== {s['heading']} ==\n{body[s['start']:s['end']]}" for s in kept)
    row = {
        "mediaType": r["mediaType"], "tmdbId": r["tmdbId"], "title": r.get("title"),
        "year": r.get("year"), "language": r.get("language"), "article": r.get("article"),
        "applicability": applic,
        "sections": [s["heading"] for s in kept],
        "storyPremiseSections": len(premise_only),
        "evidenceChars": len(evidence),
        "sourceDigestSha256": hashlib.sha256(evidence.encode("utf-8")).hexdigest(),
        "evidence": evidence,
    }
    (work if premise_only else review).append(row)
    if not premise_only:
        skipped_keys[key] = {"reason": "review", "articleSha256": r.get("articleSha256")}

work.sort(key=title_key)
review.sort(key=title_key)

# Tags on disk, vector never merged: no model needed, and regenerating would pay twice.
v1 = json.load(open(os.path.join(root, "data/premise-tags-v1.json"), encoding="utf-8"))
merge_only = sorted(set(v1.get("vectorsMissingFor") or []))

def dump(name, rows):
    path = os.path.join(args.out_dir, name)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path

dump("worklist.jsonl", work)
dump("review-queue.jsonl", review)
json.dump({"_": "tags on disk, vector never merged (DT-N). No model needed.", "keys": merge_only},
          open(os.path.join(args.out_dir, "merge-only.json"), "w"), indent=2)

chars = sum(r["evidenceChars"] for r in work)
# The generator's batch carries a shared spec plus per-title evidence; ~3.8 chars/token on prose, as measured
# on the facets batches. The spec is amortised over a batch, so it is added per batch, not per title.
SPEC_CHARS, PER_BATCH = 5_200, 22
batches = (len(work) + PER_BATCH - 1) // PER_BATCH

# The batches themselves, in `llm_phase.py`'s layout so its manifest coverage and `--list-missing` resume
# apply unchanged. Shape matches data/premise-tags-v1.SPEC.md exactly: the generator echoes `key` back, and
# never reconstructs one from a bare tmdbId — 1,097 ids in this corpus are both a film and a series.
in_dir = os.path.join(args.out_dir, "gen", "in")
os.makedirs(in_dir, exist_ok=True)
for i in range(batches):
    slice_ = work[i * PER_BATCH:(i + 1) * PER_BATCH]
    json.dump([{"key": f"{r['mediaType']}:{r['tmdbId']}", "mediaType": r["mediaType"],
                "tmdbId": r["tmdbId"], "plot": r["evidence"]} for r in slice_],
              open(os.path.join(in_dir, f"batch-{i:04d}.json"), "w", encoding="utf-8"),
              ensure_ascii=False)
json.dump({"titles": len(work), "batches": batches, "perBatch": PER_BATCH,
           "spec": "data/premise-tags-v1.SPEC.md",
           "ids": [f"{r['mediaType']}:{r['tmdbId']}" for r in work],
           "provenance": ("evidence is Wikipedia story-premise and theme-subject sections only, selected by "
                          "the Jev section audit; no TMDB prose is present in any batch file"),
           "languages": sorted({r["language"] for r in work})},
          open(os.path.join(args.out_dir, "gen", "manifest.json"), "w", encoding="utf-8"), indent=2)
in_tokens = (chars + batches * SPEC_CHARS) / 3.8
# 8-12 tags, ~28 chars each plus JSON scaffolding, per title.
out_tokens = len(work) * (12 * 34 + 40) / 3.8
estimated_total = round(in_tokens + out_tokens)
if args.token_ceiling is not None and estimated_total > args.token_ceiling:
    sys.exit(f"estimated {estimated_total} tokens exceeds --token-ceiling {args.token_ceiling}; "
             "nothing is authorized to generate")

manifest_path = os.path.join(args.out_dir, "gen", "manifest.json")
with open(manifest_path, encoding="utf-8") as handle:
    manifest = json.load(handle)
manifest.update({
    "worklistSha256": digest(os.path.join(args.out_dir, "worklist.jsonl")),
    "sourceInputs": {
        "articlesSha256": digest(args.articles),
        "combinedSha256": [digest(path) for path in args.combined],
        "changesSha256": digest(args.changes) if args.changes else None,
        "specSha256": digest(os.path.join(root, "data/premise-tags-v1.SPEC.md")),
    },
    "selection": "added-or-regained-only" if args.changes else "legacy-full-gap",
    "eligibleKeys": len(eligible) if eligible is not None else None,
    "eligibleMissingFromCombined": sorted(eligible - seen) if eligible is not None else [],
    "skippedKeys": dict(sorted(skipped_keys.items())) if eligible is not None else {},
    "estimatedInputTokens": round(in_tokens),
    "estimatedOutputTokens": round(out_tokens),
    "estimatedTotalTokens": estimated_total,
    "tokenCeiling": args.token_ceiling,
    "generationAuthorized": False,
})
with open(manifest_path, "w", encoding="utf-8") as handle:
    json.dump(manifest, handle, indent=2, sort_keys=True)

print(json.dumps({
    "worklist": len(work), "reviewQueue": len(review), "mergeOnly": len(merge_only),
    "skipped": skipped, "alreadyHaveTags": len(have),
    "evidenceChars": chars, "medianEvidenceChars":
        sorted(r["evidenceChars"] for r in work)[len(work) // 2] if work else 0,
    "batches": batches, "perBatch": PER_BATCH,
    "estInputTokens": round(in_tokens), "estOutputTokens": round(out_tokens),
    "estTotalTokens": estimated_total, "tokenCeiling": args.token_ceiling,
    "eligibleNewOrRegained": len(eligible) if eligible is not None else None,
    "eligibleMissingFromCombined": len(eligible - seen) if eligible is not None else None,
    "generationAuthorized": False,
    "outDir": args.out_dir,
}, indent=2))
