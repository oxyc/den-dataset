#!/usr/bin/env python3
"""The premise-generation worklist, and the evidence each title is judged on.

  scripts/v2/build_premise_worklist.py --combined out-repass/combined-v1-r2.jsonl \
      --articles out-repass/articles.jsonl --out-dir out-premise-v2

Premise tags are free-form strings, so they are the one artifact a decision-only model cannot produce and
the only remaining paid step in this corpus rebuild. That makes it worth spending care on *which* titles are
sent and *what* they are shown, because both decide the bill and neither is recoverable after the fact.

## Which titles

Not the raw coverage gap. The Jev pass already judged every article, so the gap is filtered by what it found:

- a title that already has tags is skipped — `data/premise-tags-v1.json` **and** `out-premise-999/tags.json`,
  which is gitignored and holds 999 results that exist nowhere else. Missing it would regenerate work
  already paid for (den-dataset#13).
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
import json
import os
import sys

ROLE_KEEP = ("story-premise", "theme-subject")


def load_have(root):
    """Every title that already has premise tags, from both sources."""
    have = set(json.load(open(os.path.join(root, "data/premise-tags-v1.json"), encoding="utf-8"))["tags"])
    extra_path = os.path.join(root, "out-premise-999/tags.json")
    if os.path.exists(extra_path):
        extra = json.load(open(extra_path, encoding="utf-8"))
        have |= set(extra) if isinstance(extra, dict) else {f"{r['mediaType']}:{r['tmdbId']}" for r in extra}
    else:
        sys.exit("out-premise-999/tags.json is missing — it is gitignored and holds 999 results that exist "
                 "nowhere else; refusing to build a worklist that would regenerate them")
    return have


ap = argparse.ArgumentParser()
ap.add_argument("--combined", required=True, help="the Jev bundle (combined-v1-r2.jsonl)")
ap.add_argument("--articles", required=True, help="dump-articles output, for the section text")
ap.add_argument("--out-dir", required=True)
args = ap.parse_args()

root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
have = load_have(root)
os.makedirs(args.out_dir, exist_ok=True)

# The article text, keyed for section slicing. One pass, kept as offsets rather than copies.
text = {}
with open(args.articles, encoding="utf-8") as fh:
    for line in fh:
        r = json.loads(line)
        text[f"{r['mediaType']}:{r['tmdbId']}"] = r["text"]

work, review, skipped = [], [], {"hasTags": 0, "badValidity": 0, "nonNarrative": 0, "noArticleText": 0}
with open(args.combined, encoding="utf-8") as fh:
    for line in fh:
        r = json.loads(line)
        key = f"{r['mediaType']}:{r['tmdbId']}"
        if key in have:
            skipped["hasTags"] += 1
            continue
        answers = r.get("answers") or {}
        if (answers.get("validity") or {}).get("choice") != "correct-screen-work":
            skipped["badValidity"] += 1
            continue
        applic = (answers.get("narrative_applicability") or {}).get("choice")
        if applic in ("non-narrative-program", "documentary-or-factual"):
            skipped["nonNarrative"] += 1
            continue
        body = text.get(key)
        if body is None:
            skipped["noArticleText"] += 1
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
            "evidenceChars": len(evidence), "evidence": evidence,
        }
        (work if premise_only else review).append(row)

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

print(json.dumps({
    "worklist": len(work), "reviewQueue": len(review), "mergeOnly": len(merge_only),
    "skipped": skipped, "alreadyHaveTags": len(have),
    "evidenceChars": chars, "medianEvidenceChars":
        sorted(r["evidenceChars"] for r in work)[len(work) // 2] if work else 0,
    "batches": batches, "perBatch": PER_BATCH,
    "estInputTokens": round(in_tokens), "estOutputTokens": round(out_tokens),
    "estTotalTokens": round(in_tokens + out_tokens),
    "outDir": args.out_dir,
}, indent=2))
