# Premise-triplet judging — subagent prompt (v2 ruler)

Two hard constraints on how you use tools, before anything else:

1. **One command per Bash call.** No `&&`, no `;`, no `|`. Compound commands trigger a
   permission prompt and stall the run.
2. **Never point `grep`, `rg`, `find` or `ls` at a directory tree.** Read files with the
   Read tool. This task needs exactly one Read and one Write, and no searching.
3. **Do not spawn subagents.** Do the work yourself.

## Task

Read `{IN_PATH}` — a JSON array of cases, each `{id, anchor, a, b}` where `anchor`, `a` and
`b` are `{title, year, plot}`.

**You are not told which of `a` and `b` is which, and their order is randomised.** Score
both pairs on their merits. Do not try to infer which one is "meant" to be the answer —
guessing the intent is the one thing that would make this judgement worthless.

For **each case**, judge `anchor` vs `a` and `anchor` vs `b` independently.

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

Verdict from the axes — apply the thresholds mechanically, do not overrule them:
- **`twin`** — 4 or more axes true, **and** at least one of `situation` or `device` is true.
- **`related`** — 2 or 3 axes true.
- **`unrelated`** — 0 or 1 axes true.

Note that "4+ axes but neither `situation` nor `device`" is a `related`, not a `twin`. That
case is common and is the whole reason the rule exists.

## Output

Write a JSON array to exactly `{OUT_PATH}` — one object per case, in input order, all of
them. Nothing else: no prose, no markdown fence, no commentary.

```json
[
  {
    "id": "movie:11#0",
    "a": {
      "axes": {"situation": true, "engine": true, "goal": true,
               "relationship": false, "obstacle": true, "device": false},
      "verdict": "twin",
      "reason": "orphan on a backwater world pulled into a war by a mentor who dies"
    },
    "b": {
      "axes": {"situation": false, "engine": false, "goal": false,
               "relationship": false, "obstacle": true, "device": false},
      "verdict": "unrelated",
      "reason": "same effects-driven adventure register, but a survival chase with no destiny arc"
    }
  }
]
```

`reason` is one clause under 25 words naming the shared or absent story engine.

Rules that make the output usable:
- Every `id` copied **verbatim** from the input.
- Emit an object for **every** case in the batch.
- The `verdict` must follow from the `axes` you wrote, by the thresholds above. A verdict
  that contradicts its own axes fails the case.
