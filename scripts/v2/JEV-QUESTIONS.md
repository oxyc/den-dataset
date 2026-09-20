# The Jev delta pass — what the completed run did not ask

**The corpus pass already ran.** `out-repass/combined-v1-r2.jsonl`: 47,529 titles, `jev-1.13.0`,
2026-09-19, **$20.47**, audited at exact coverage in `combined-v1-r2.bundle-audit.json`. It holds 12 facet
Choices, `validity` (6 target-aware values), `narrative_applicability`, `primary_genre`, 75 `tax__*` Nouls,
4 `score__*` Scores, and a role Choice **per article section** — 27 of them for The Wire.

So this document is a **diff**, not a design. Anything already answered is listed under "do not re-ask"
with the reason, because the expensive mistake now is paying for the state twice.

**Price of the delta.** The state is the whole article and it is what costs: the completed run spent $20.47
on 487M input tokens. A delta pass costs approximately the same again regardless of how few questions it
carries — which is the entire argument for making this list as complete as we can stand.

---

## 1. `subject_of_critique` — a Noul set. The one proven gap.

> *What is this work arguing about?* — asked as independent Nouls, not one Choice.

`institution` · `the-state` · `capitalism-or-market` · `justice-system` · `policing` · `war-or-military` ·
`media` · `religion` · `family` · `class` · `race` · `gender` · `education` · `healthcare` ·
`technology` · `colonialism` · `the-self`

**Why it is needed, measured.** The Wire and Oz are the same kind of show to a human — a sociological study
of a closed American institution with its own economy and moral compromise — and **no feature on disk
connects them**. Every content-bearing axis correctly disagrees; everything they share is generic
prestige-drama form (`bleak`, `ensemble-led`, `continuous`, `Dark & Gritty`, `Thought-provoking`) — which is
exactly the Angel failure mode. Two independent gates keep Oz out: premise rank 4,450 / plot rank 1,134, and
a tonal score of 0.257.

The "use the probability distributions rather than the argmax" idea is **dead, measured**: on this pair
`scope` and `setting` are perfectly one-hot. The Wire's mass on `setting = institution` is **0.000**; Oz's
on `scope = single-city` is **0.000**. No hidden mass. Switching to distributions ranked Oz *worse*
(164 → 176).

**It must be a Noul set, not a Choice**: The Wire critiques policing *and* the justice system *and* class.
A Choice would split the mass and all three would read weak.

**It must ask about the argument, not the premises.** `setting = institution` already exists and covers
1,064 TV titles; its members include Night Court, Saved by the Bell and Are You Being Served?. That is a
location fact. Wording that reproduces it is worthless — the question is what the work is *about*, not
where it is *set*.

---

## 2. Depiction Nouls — the only cleanly novel ask

`depicts_graphic_violence` · `depicts_sexual_content` · `depicts_drug_use` · `depicts_self_harm` ·
`depicts_animal_harm` · `intended_to_frighten`

**Nouls, not Scores, and no profanity question.** A plot summary essentially never describes dialogue
register, so a `language` Score would be a pure genre transform — crime 0.7, family 0.1 — and a model asked
for a number will always produce one. A Noul at least has an implicit abstain in being low.

**Ask what the article says is depicted, not how intense it is.** "A drug-dealing film" and "a film that
shows drug use" are different claims and the question must not conflate them.

TMDB's `release_dates` / `content_ratings` give certificates as hard facts and should be preferred for an
age gate. These Nouls are for what a certificate cannot say: *which* content, per title, at corpus scale.

---

## 3. Audience intent — two Nouls

`made_for_children` · `made_for_teens`

Distinct from safety: a slow French drama is harmless to an eight-year-old and is for nobody's eight-year-
old. Nothing on disk expresses intended audience.

---

## 4. Animation technique — a Noul set

`hand-drawn` · `cg-animation` · `stop-motion` · `anime` · `puppetry` · `rotoscope` · `archival-footage` ·
`live-action`

`animated` is currently a **hard gate** (`similar.rs`, the animated/live-action split), and it starves
animated anchors: Spirited Away returned 10 neighbours of 20 and Inside Out 8, because the gate drops
live-action candidates from a fixed pool. Softening it needs a better input than a boolean — "anime vs
Pixar vs Aardman" is what a viewer means, and one flag cannot say it.

Nouls because stop-motion *is* animated and a documentary is also live-action; an exclusive Choice would
force the model to arbitrate a subset relation.

---

## 5. Do NOT re-ask — already bought, with the reason

| Tempting | Already there |
|---|---|
| Validity / "is this article about this title" | `validity`, 6 values (`correct-screen-work` 44,151 · `source-work` 1,625 · `other-screen-work` 1,539 · …) |
| Per-section extraction audit (#16) | a role Choice per section, with probabilities — 27 sections for The Wire |
| Serialized vs episodic | `continuity` (`continuous` / `episodic` / `hybrid` / `anthology`), 81% publishable |
| The nine/twelve plot facets | all present, full distributions |
| Genre, subgenres, moods | `primary_genre` + 75 Nouls |
| "Store the raw answer" / provenance | already stricter than proposed: `articleRevId`, `articleSha256`, `questionsSha256`, `promptSha256`, `configSha256`, pinned `responseModels` |
| Medium, format, adaptation source | Wikidata: `instanceOf` **100%**, `basedOn`/`basedOnKind` 19.9% (and absence *is* the answer) |
| A "fantastical world" axis | **derivable today** from 12 existing Nouls (vampire, werewolf, zombie, superhero, time-travel, cyberpunk, post-apocalyptic, folk/supernatural/sci-fi horror, sci-fi action, fantasy adventure). Measured: The Wire 0.04, Angel 0.97, Oz 0.06. Already wired and shipping in the rescorer. |

### And the one to refuse outright

**More register Scores.** The draft this replaces proposed six (`institutional_focus`, `moral_ambiguity`,
`naturalism`, `sociological_intent`, `interiority`, `ensemble_breadth`). The four Scores that already exist
falsify the idea: **PC1 = 61.9%** of variance, `intensity ↔ emotional_weight` r = **+0.70**,
`humour ↔ weight` r = **−0.74**. They are one "serious vs light" axis already.

Decisively: **Angel sits 0.03 from The Wire on `intensity` and 0.05 on `emotional_weight`, while Oz sits
0.86 and 0.18 away.** The exact defect the new Scores were proposed to fix, reproduced inside the primitive
proposed to fix it. Asking six more buys another loading on PC1.

---

## 6. Rules for the new questions

Learned from auditing the draft this replaces:

1. **`not-stated` and `other` on every Choice.** Two different conditions — the article is silent, versus
   our vocabulary has no slot — and collapsing them loses the census that justifies the next vocabulary
   change. The existing pass has `does-not-apply`, which is one of the two.
2. **Multi-valued properties are Nouls.** Every new group above is a Noul set for this reason.
3. **Store every value, always**, including zeros; no top-k. Thresholds are applied by `derive`, which is
   cheap to re-run.
4. **Ask the new questions before `primary_genre`** if the pass re-asks it at all. Once a genre token is in
   the output stream every later answer has reason to agree with it, which is the mechanism that turns a
   content question into a genre transform.

## 7. Validate on 500 titles before the corpus

~$0.25 against ~$20. Report, and gate the full run on it:

- `subject_of_critique` **prevalence per value** — if `institution` fires on 30% of TV it has reproduced
  `setting = institution` and the wording must change before the corpus run.
- **Does it separate the pair it exists for?** The Wire and Oz both high on `institution` + `policing` /
  `justice-system`; Angel low on all of them. If not, this pass has no justification.
- **Correlation with the existing four Scores.** Any |r| > 0.8 against PC1 means it is the prestige axis
  again in new clothes.
- **Depiction Nouls against article length**, to catch "measures Wikipedia, not the film".
- **Test–retest on 200 titles**: same questions, two runs, report mean |Δ|.
