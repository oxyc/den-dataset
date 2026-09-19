# Jev facet census and v2 decision

Measured 2026-09-19 after the Wikipedia re-ground completed. Inputs and outputs are local under
`out-repass/`; they are generated artifacts, not committed data.

## Corpus integrity

- 47,529 article rows, 47,529 unique `(mediaType, tmdbId)` keys
- 47,529 facet rows, 47,529 unique keys
- no malformed rows, duplicates, missing keys, extra keys, or rows with the wrong axis set
- four unusually token-dense articles required a 25,000-character cap; every other title used its whole article
- total measured Jev spend: approximately $7.52

`validity` found 45,735 `single-work`, 1,221 `source-work`, 522 `franchise-overview`, 33
`season-aggregation`, and 18 `not-a-work` rows. The 1,794 non-single-work rows are evidence for the grounding
repair, not browse facets to publish as if they described the requested screen work.

## Full-corpus v1 census

Entropy is normalized to 0...1 over each Choice distribution. A high value means the model spread probability
across the vocabulary even when it returned an argmax.

| axis | median confidence | median entropy | does not apply | largest value | decision |
|---|---:|---:|---:|---|---|
| era | 0.96 | 0.060 | 0.3% | contemporary 62.1% | keep |
| setting | 0.85 | 0.185 | 2.6% | urban 42.4% | keep |
| scope | 0.71 | 0.350 | 1.0% | single-city 34.5% | keep |
| ending | 0.76 | 0.308 | 1.4% | happy 29.4% | keep |
| pacing | 0.48 | 0.637 | 8.1% | propulsive 29.6% | redesign |
| structure | 0.86 | 0.212 | 3.0% | linear 75.6% | store; weak primary chip |
| conflict | 0.74 | 0.342 | 3.4% | person-vs-person 54.0% | keep |
| ensemble | 0.34 | 0.783 | 29.4% | does-not-apply 29.4% | redesign |
| tone | 0.49 | 0.539 | 0.8% | earnest 34.3% | keep, confidence-gated |

The two weak axes fail differently.

**Ensemble is a vocabulary failure.** No-answer stays between 23% and 33% in every useful article-length band,
is 33.0% for movies but 10.6% for TV, and its no-answer runner-up is spread across every option. “How many people
carry it?” conflates cast size, number of named characters, and narrative focus. V2 asks for narrative focus:
`single-lead`, `dual-lead`, `group-led`, `ensemble-led`. One protagonist with many supporting characters is
explicitly `single-lead`.

**Pacing is both an evidence and vocabulary failure.** No-answer is 63.5% below 1,000 article characters, 27.8%
at 1,000–2,999, 4.1% at 3,000–9,999, and 0.9% at 10,000+. The v1 vocabulary also made `episodic` compete with
tempo even though a work can be both episodic and fast. V2 moves `episodic` to structure and uses only tempo:
`slow-burn`, `measured`, `brisk`, `relentless`.

Structure is reliable metadata but a poor headline filter: 75.6% of the corpus is `linear`. Keep it in the
artifact and query language; do not spend scarce browse-chip space on it by default.

## V2 pilot

`build_facet_pilot.py` selected 230 deterministic titles: 80 ensemble failures, 60 pacing failures split by
article length, 70 clean movie/TV controls, and 20 non-single-work controls. `run_facets.py` evaluated
`prompts/facets-v2.md` for 994,058 input tokens and $0.0418, with 230 written and zero failures.

| measurement | v1 on those rows | v2 |
|---|---:|---:|
| old ensemble no-answer rows still unanswered | 92 | 4 |
| median v2 ensemble confidence on old failures | — | 0.965 |
| ensemble median confidence / entropy overall | 0.34 / 0.783 corpus-wide | 0.91 / 0.173 pilot |
| old pacing no-answer rows still unanswered | 69 | 17 |
| pacing median confidence / entropy overall | 0.48 / 0.637 corpus-wide | 0.56 / 0.537 pilot |
| old structure no-answer rows still unanswered | 27 | 7 |

Moving `episodic` made it 25.2% of structure in the TV-heavy pilot and reduced `linear` to 61.3%. That is a
cleaner statement than calling 52.6% of single-work TV “episodic pacing.”

The unchanged questions provide a repeatability check despite the deliberately difficult sample: exact Choice
agreement was era 97.4%, setting 95.2%, scope 97.4%, ending 88.7%, conflict 96.5%, tone 89.6%, and validity 99.6%.

## Seven-archetype candidate

V2 adds an `archetype` facet inspired by Christopher Booker's seven plots, with neutral labels that avoid genre
collisions: `overcoming-threat`, `rise`, `quest`, `voyage-and-return`, `comic-resolution`, `downfall`, `rebirth`.
`comic-resolution` explicitly means restored order or reconciliation, not comic tone.

On the pilot it had median confidence 0.72, normalized entropy 0.335, and 9.6% no-answer. Restricted to rows Jev
called `single-work`, no-answer was 4.6% for movies and 16.5% for TV. The TV gap is expected: documentaries,
anthologies, and open-ended episodic series often have no whole-series arc. This is good enough to retain the
axis with confidence gating; `does-not-apply` is meaningful rather than a vocabulary accident.

The facet does not save the cost of acquiring or reading plots. It is cheap because the article state is already
paid for and one additional typed question adds little response time and no billed output.

## Pass-two contract

Do not buy a standalone full-corpus rerun just for these changes. The next corpus call should combine:

1. the ten v2 facets, including `archetype`;
2. `validity`;
3. primary genre, subgenres, and moods;
4. the planned score questions;
5. one section-heading audit question per article.

Store full Choice distributions and identify the versioned question set on every row. Publication should omit
narrative facets for non-`single-work` rows until grounding is repaired, confidence-gate soft axes, and retain
structure as a signal without making it a default browse chip.

