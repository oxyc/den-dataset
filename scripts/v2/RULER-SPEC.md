# The premise-similarity rubric (v2 ruler)

## Why this document exists

DT-H reports premise discrimination as a score "/12". That 12 is the **number of
hand-authored test cases** in the 1,526-title bake-off, not a twelve-point rubric, and the
`bakeoff/` scratchpad that held those cases is gone. There is therefore no rubric to reuse.
This one is authored for v2 and stated in full so the ruler's judgements are reproducible
and arguable, rather than resting on a number whose definition was lost.

## The question a judge answers

Two films share a **core premise** when a viewer would use the same *"it's the one where
___"* sentence for both. That sentence names the story engine — not the genre, not the
tone, not the setting, not the subject matter.

The canonical positive from DT-H: *Voicemails for Isabelle* and *Love Again* — grief
messages sent to a dead loved one's **reassigned phone number**. Different countries,
different casts, different decades, same premise.

The canonical trap: two war films set in the same year with the same tone that are about
structurally different things. Same genre, same era, same register — different premise.

## The six axes

Each is judged **yes / no** from the plots alone.

| axis | asks |
|---|---|
| `situation` | Does the same central predicament set the story in motion? |
| `engine` | Does the same mechanism drive scene-to-scene action — a loop, an investigation, a heist plan, a countdown, a road journey? |
| `goal` | Does the protagonist want structurally the same thing? |
| `relationship` | Does the same core relationship configuration carry the story? |
| `obstacle` | Is the opposing force the same *kind* of force? |
| `device` | Do both turn on the same distinctive hook or gimmick? (`no` if neither has one) |

## Verdicts

- **`twin`** — 4+ axes yes, and at least one of `situation` or `device` is yes.
- **`related`** — 2–3 axes yes.
- **`unrelated`** — 0–1 axes yes.

The `situation`-or-`device` requirement is what stops "two people fall in love, are kept
apart, and end up together" from scoring as a twin on `goal` + `relationship` + `obstacle`.

## What a triplet must satisfy

- **anchor** — the mined title.
- **positive** — verdict `twin`. If no candidate is a `twin`, the anchor yields **no
  triplet**. Emitting a weak positive to fill a quota is the one failure that would make
  the whole ruler useless, so "none" is an expected and correct answer.
- **hard negative** — verdict `unrelated`, *and* superficially confusable with the anchor:
  same primary genre, or same setting/era, or overlapping subject matter. A negative that
  is obviously unrelated tests nothing.

## What the judge never sees

Plots only. No tags — not the v1 premise tags, not the t02 labels, not TMDB keywords. The
mining used keywords to *propose* candidates; letting the judge see them would grade the
proposer. And no title-level popularity, so the ruler cannot learn "the famous one is the
answer".

## A measured limitation: the axis count is not a difficulty gradient

It is tempting to read "6 axes true" as a stronger twin than "4 axes true" and to use the
count as a confidence weight. Measured on 264 DEV triplets, the premise index's accuracy does
**not** track it:

| axes true on the positive | n | premise-v1 accuracy |
|---:|---:|---:|
| 4 | 59 | 0.678 |
| 5 | 144 | 0.660 |
| 6 | 61 | 0.705 |

Flat, and not monotone. So the rubric's binary `twin` / `not` decision is doing real work —
the twin set scores far above chance and above the plot index — but the 4/5/6 gradation
carries no information about how detectable a pair is to an embedding. **Do not weight cases
by axis count, and do not report a "strength" derived from it.** If a difficulty gradient is
ever wanted, it has to come from something else, e.g. the margin the index itself assigns.

## Provenance rule

Only titles with `hasWikiPlot == true` reach a judge. The batch builder asserts it per
record; every batch file records the assertion. Enriched rows without a wiki plot carry
TMDB prose in `overview`, and sending that to an AI application is barred by TMDb §1.C.
