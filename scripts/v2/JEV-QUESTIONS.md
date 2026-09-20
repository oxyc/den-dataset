# The one-pass Jev question set

**Status: draft for audit. Not a task to start — Jev is waitlisted (#14).**

## Why one pass, and why ask more than we need

The state is the whole Wikipedia article; the questions are nearly free. #14 prices the corpus at **$2.24
for facets alone, $4.67 for labels alone, and $5.51 for both in one call with shared state**. So a second
pass later does not cost a little more — it costs the entire state again, plus the risk that the corpus has
moved underneath it and the two halves no longer describe the same reading.

That inverts the usual discipline. The expensive mistake is not asking something we never use; it is
**failing to ask something we later want**. So this set deliberately includes axes nothing reads today.

Two rules follow, and both are load-bearing:

1. **Store the raw answer, never the thresholded one.** Every `Choice` keeps its full distribution, every
   `Noul` its probability, every `Score` its value. Today's `confidence >= 0.55` cutoff is a decision we
   already want to revisit — `similar.rs` uses it as a hard gate — and re-deriving a threshold from stored
   values is free while re-asking is not.
2. **Record what produced the answer**: model id, prompt version, article `revid`, and the date. Without
   the revid we cannot tell a changed answer from a changed article, which is the mistake the corpus/query
   embedder drift (#21) made in a different register.

## The state

The article's **whole prose, every section, no clamp** — the position argued in #14. Not the extracted
plot. Three reasons, all still true:

- **Validity needs the context the extractor strips.** #16: 940 titles carry another title's plot because
  `ownArticleSufficient` falls through to the P144 source work. Handed prose about Heathcliff, a classifier
  cannot know it came from the novel's article; handed the article, the lead sentence settles it.
- The article carries country, animated-vs-live-action, miniseries-vs-series and release format, which
  today come from Wikidata and are missing wherever Wikidata is thin.
- The extractor stays for the embedder, which genuinely needs it: the lean CC0 doc measures 0.091 against
  0.018 discrimination, and embedding Reception text would cluster titles by critical consensus.

---

## A. Validity — asked first, gates everything after it

| id | primitive | values |
|---|---|---|
| `article_is_about_this_title` | Noul | — |
| `article_subject` | Choice | `this-title`, `source-work`, `franchise`, `person`, `other` |
| `describes_a_different_medium` | Noul | — |

**Why.** This is the #16 fix and it is worth the whole pass on its own. A title whose article is the
novel's gets every downstream answer wrong in a way nothing detects — the labels look plausible, the
vectors cluster it with the other adaptations, and only a human reading both notices.

---

## B. Identity and medium — facts the article states and Wikidata often lacks

| id | primitive | values |
|---|---|---|
| `medium` | Choice | `live-action`, `animated`, `mixed`, `documentary-footage`, `stop-motion` |
| `format` | Choice | `feature`, `short`, `series`, `miniseries`, `tv-movie`, `special` |
| `is_adaptation` | Noul | — |
| `adapted_from` | Choice | `novel`, `short-story`, `comic`, `manga`, `play`, `game`, `true-events`, `film`, `series`, `none` |
| `is_sequel_or_remake` | Choice | `standalone`, `sequel`, `prequel`, `remake`, `reboot`, `spin-off` |

**Why `medium` is a Choice, not a boolean.** `animated` is currently a hard gate in `similar.rs:35-37` and
it starves animated anchors: Spirited Away returns 10 neighbours and Inside Out 8, against a screen sized
for 20. A gate that coarse needs a better input than a boolean before it can be softened.

---

## C. Labels — the shipped taxonomy, asked as the shape it actually is

| id | primitive | values |
|---|---|---|
| `primary_genre` | Choice | the shipped 17 |
| `subgenres` | Noul × 59 | the shipped 59 |
| `moods` | Noul × 16 | the shipped 16 |

**Multi-label belongs in Nouls, not one Choice** (#14): a Choice distribution sums to 1, so a film honestly
both Heist *and* Revenge splits its mass and both read weak. Nouls ask independently, which is what is true.

Keep the vocabularies exactly as shipped so this pass can be diffed against the current labels rather than
replacing them blind. Vocabulary changes are a separate decision from re-labelling.

---

## D. Plot facets — the nine axes that already exist and are 5% filled

Vocabularies taken verbatim from `plot-facets-c85c707b0b18.json`, which defines all nine and fills 2,456 of
47,539 titles. **The Wire's entry is null.**

| axis | primitive | values |
|---|---|---|
| `era` | Choice | prehistoric, ancient, medieval, early-modern, 19th-century, early-20th-century, mid-20th-century, late-20th-century, contemporary, near-future, far-future, timeless |
| `setting` | Choice | urban, suburban, rural, small-town, domestic, institution, wilderness, sea, space, underground, virtual, road |
| `scope` | Choice | single-location, single-city, regional, national, global, cosmic |
| `ending` | Choice | happy, bittersweet, tragic, open, ambiguous, cyclical, unknown |
| `pacing` | Choice | slow-burn, steady, propulsive, frantic, episodic |
| `structure` | Choice | linear, nonlinear, framed, parallel-strands, single-day, anthology |
| `conflict` | Choice | person-vs-person, person-vs-self, person-vs-system, person-vs-society, person-vs-nature, person-vs-unknown |
| `ensemble` | Choice | solo, duo, small-group, large-ensemble |
| `tone` | Choice | earnest, comic, satirical, pulpy, bleak, melancholy, dreamlike, clinical |

**Why this section is the point of the exercise.** The measured defect in More Like This is that the rail
cannot tell register from subject matter. Angel holds 12th on The Wire because it carries both of The
Wire's moods (`Dark & Gritty`, `Thought-provoking`) and shares no subgenre; the tonal term cannot see the
difference. `conflict = person-vs-system` + `ensemble = large-ensemble` + `scope = single-city` describes
The Wire exactly and describes Angel not at all. Oz and Deadwood pass that test and are currently absent.

---

## E. Register scores — continuous, because these are not categories

| id | primitive | 0 … 1 |
|---|---|---|
| `institutional_focus` | Score | one person's life … how a system works |
| `ensemble_breadth` | Score | one protagonist … no protagonist |
| `moral_ambiguity` | Score | clear right and wrong … no clean side |
| `naturalism` | Score | stylised/heightened … documentary-plain |
| `sociological_intent` | Score | tells a story … argues about society |
| `interiority` | Score | external action … inner life |
| `intensity` | Score | gentle … relentless |
| `humour` | Score | none … constant |
| `weight` | Score | light … heavy |
| `complexity` | Score | follow it half-asleep … demands attention |

The last four are #14's own list. The first six are this exercise's finding: they are what separates The
Wire from a cop show, and a `Choice` would force a false cut through a continuum.

---

## F. Content signals — asked once so they never need a pass of their own

| id | primitive | 0 … 1 |
|---|---|---|
| `violence`, `sexual_content`, `language`, `substance_use`, `frightening` | Score × 5 | absent … pervasive |
| `family_suitable_floor_age` | Choice | `0`, `7`, `12`, `15`, `18` |

Nothing reads these today. They are the clearest case of the rule above: a kids-safe filter is an obvious
future ask, and adding it later would cost the entire corpus state again.

---

## G. Deliberately NOT asked

- **Cast, crew, studio, country, release date, runtime.** Wikidata has them, they are facts rather than
  judgements, and a model reading prose will hallucinate a plausible one where the article is silent.
- **Popularity, quality, ratings.** Not properties of the work, and `facets.bin` carries vote counts.
- **Free-text summaries.** Jev is decision-only; prose answers are not what it is for, and a stored summary
  invites someone to embed it, which would be a third document shape.
- **Anything derivable from the answers above.** Store the primitives, derive in code, where a mistake
  costs a re-run of `derive`, not a re-run of the corpus.

## Storage contract

One record per title, in the pass's own artifact, never merged into `labels-t02.json`:

```json
{
  "mediaType": "tv", "tmdbId": 1438,
  "article": "The Wire", "revid": 1234567890,
  "model": "jev-…", "promptVersion": "q1", "askedAt": "2026-…",
  "validity": {"article_is_about_this_title": 0.99, "article_subject": {"this-title": 0.98, …}},
  "labels": {"primary_genre": {"Crime": 0.71, "Drama": 0.24, …},
             "subgenres": {"Police Procedural": 0.93, …},
             "moods": {"Dark & Gritty": 0.95, …}},
  "facets": {"conflict": {"person-vs-system": 0.88, …}, …},
  "scores": {"institutional_focus": 0.94, …}
}
```

Full distributions, every axis, every title — including the ones that lose. The argmax and the 0.55 cut are
applied downstream by `derive`, which is cheap to re-run and is where the thresholds belong.

## Open questions for the audit

1. Is any question unanswerable from an article, such that the model will confabulate rather than abstain?
2. Do any two axes collide (`setting = institution` vs `conflict = person-vs-system`; `pacing = slow-burn`
   vs mood `Slow-burn`), and does that cost us or merely duplicate?
3. Is the Score set too large to be answered consistently — would the model anchor them all to the same
   latent "seriousness"?
4. Does anything here need a `Choice` where it is written as a `Noul`, or the reverse?
5. What is missing that will be expensive to add later?
