# Premise-triplet generation — subagent prompt (v2 ruler)

Two hard constraints on how you use tools, before anything else:

1. **One command per Bash call.** No `&&`, no `;`, no `|`. Compound commands trigger a
   permission prompt and stall the run.
2. **Never point `grep`, `rg`, `find` or `ls` at a directory tree.** Read files with the
   Read tool. You need exactly two file operations for this task and no searching at all.
3. **Do not spawn subagents.** Do the work yourself.

## Task

Read `{IN_PATH}`. It is a JSON array of items, each `{anchor, candidates}` where the anchor
and every candidate has `{key, title, year, plot}`. The plots are Wikipedia plot summaries,
excerpted head-and-tail (a `[…]` marks the cut).

For **each anchor**, decide which candidate — if any — shares the anchor's **core premise**,
and which candidate is the best **hard negative**.

## What "shares a core premise" means

Two films share a core premise when a viewer would use the same *"it's the one where ___"*
sentence for both. That sentence names the **story engine** — not the genre, not the tone,
not the setting, not the subject matter.

The canonical positive: two films about grief messages sent to a dead loved one's
**reassigned phone number**. Different countries, different casts, different decades — same
premise.

The canonical trap: two war films from the same year with the same tone that are about
structurally different things. Same genre, same era, same register — *different premise*.

Judge these six axes, each strictly yes/no, from the plots alone:

| axis | asks |
|---|---|
| `situation` | Does the same central predicament set the story in motion? |
| `engine` | Does the same mechanism drive scene-to-scene action — a loop, an investigation, a heist plan, a countdown, a road journey? |
| `goal` | Does the protagonist want structurally the same thing? |
| `relationship` | Does the same core relationship configuration carry the story? |
| `obstacle` | Is the opposing force the same *kind* of force? |
| `device` | Do both turn on the same distinctive hook or gimmick? (`false` if neither has one) |

Verdict from the axes:
- **`twin`** — 4 or more axes true, **and** at least one of `situation` or `device` is true.
- **`related`** — 2 or 3 axes true.
- **`unrelated`** — 0 or 1 axes true.

The `situation`-or-`device` requirement is deliberate: it stops "two people fall in love,
are kept apart, end up together" from scoring as a twin on `goal` + `relationship` +
`obstacle` alone.

## Choosing the triplet

- **positive** — the candidate whose verdict is `twin`. If more than one qualifies, take the
  one with the most axes true.
- **If no candidate is a `twin`, emit `"positive": null`.** This is common and correct.
  Most anchors have no premise twin in a pool of ten. **Do not stretch a `related` into a
  `twin` to fill the slot** — a ruler padded with weak positives is worse than a small one,
  because every later comparison inherits the error.
- **negative** — a candidate whose verdict is `unrelated` **and** which is superficially
  confusable with the anchor: same genre, or same setting/era, or overlapping subject
  matter. A candidate that is obviously unrelated tests nothing; prefer the one that looks
  most similar on the surface while sharing no premise. If every candidate is `twin` or
  `related`, emit `"negative": null`.

## Output

Write a JSON array to exactly `{OUT_PATH}` — one object per anchor, in input order, all
{N} of them. Nothing else: no prose, no markdown fence, no trailing commentary.

```json
[
  {
    "key": "movie:11",
    "positive": {
      "key": "movie:1893",
      "axes": {"situation": true, "engine": true, "goal": true,
               "relationship": false, "obstacle": true, "device": false},
      "verdict": "twin",
      "reason": "orphaned youth on a backwater world is pulled into a galactic war by a mentor who dies"
    },
    "negative": {
      "key": "movie:329",
      "axes": {"situation": false, "engine": false, "goal": false,
               "relationship": false, "obstacle": true, "device": false},
      "verdict": "unrelated",
      "reason": "same era and same effects-driven adventure register, but it is a survival chase with no chosen-one arc"
    }
  }
]
```

`reason` is one clause, under 25 words, naming the shared (or absent) story engine. It is
read by a human auditing the ruler, so make it specific enough to argue with.

Rules that make the output usable:
- Every `key` must be copied verbatim from the input. Never invent one.
- `positive.key` and `negative.key` must be different, and neither may equal the anchor.
- Emit an object for **every** anchor in the batch, even when both slots are null.
- The `verdict` must be consistent with the `axes` you wrote, by the thresholds above.
