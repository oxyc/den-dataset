# The delta pass — what the classify pass did not ask

`run_delta.py` asks a second, smaller question set (`delta_questions.py`) over the same article states the
classify pass sent (`FACETS-V2.md`), reusing its runner, manifest, lock and breaker. It asks no per-section
questions: the section roles are already bought. Its shards are `out-repass/delta-v2*.jsonl`, which the
corpus join reads into `critique`, `depicts`, `audience` and `technique`. It runs outside the stage order and
buys only with `--spend`.

The question wording, and the trap each group is written to avoid, live in `delta_questions.py`. This file
holds what the code cannot: why these groups, and what was deliberately left out.

## Why these four groups

- **`subject_of_critique`** (17 Nouls: institution, the-state, policing, class, …). The one gap measured on
  disk: *The Wire* and *Oz* are the same kind of show to a viewer, and no existing feature connected them —
  everything they shared was generic prestige-drama form, which *Angel* shares too. Reading the facet
  distributions instead of the argmax did not help: on this pair they are one-hot. It asks what a work
  **argues about**, not where it is **set**: `setting = institution` already exists and includes *Night
  Court* and *Saved by the Bell*.
- **Depiction Nouls** (graphic violence, sexual content, drug use, self-harm, animal harm) and
  `intended_to_frighten`: *which* content a title shows, per title, which a certificate cannot say. Nouls
  rather than Scores, and no profanity question: a plot summary almost never describes dialogue, so a
  number would be a genre transform.
- **Audience intent** (`made_for_children`, `made_for_teens`): harmless is not the same as made for.
- **Technique** (hand-drawn, CG, stop-motion, anime, puppetry, rotoscope, archival footage, live action):
  `animated` is a hard gate in den-atlas's similarity and starves animated anchors; "anime vs Pixar vs
  Aardman" is what a viewer means. Nouls, because stop-motion is also animated.

## Deliberately not asked

| Tempting | Already there |
|---|---|
| Is this article about this title | `validity`, 6 target-aware values |
| Per-section extraction audit | the classify pass's role Choice per section |
| Serialized vs episodic | `continuity` |
| Genre, subgenres, moods | `primary_genre` + 75 Nouls |
| Medium, format, adaptation source | Wikidata `instanceOf` (100%), `basedOn` (absence is the answer) |
| A "fantastical world" axis | derivable from 12 existing Nouls (vampire, superhero, time travel, …) |
| More register Scores (moral ambiguity, naturalism, …) | the four existing Scores are already one "serious vs light" axis: PC1 is 61.9% of their variance. *Angel* sits 0.03 from *The Wire* on intensity while *Oz* sits 0.86 away, so more of them would load on the same axis. |

## Rules for new questions

1. **`not-stated` and `other` on every Choice** — the article is silent vs. the vocabulary has no slot.
   Collapsing them loses the census that justifies the next vocabulary change.
2. **Multi-valued properties are Nouls.** A Choice splits the mass and every true value reads weak.
3. **Store every value, including zeros.** Thresholds belong to whatever derives from the answers, which is
   cheap to re-run.
4. **Ask content questions before `primary_genre`**, if genre is asked at all: once a genre token is in the
   output, later answers have reason to agree with it.
5. **Pilot on ~500 titles first.** Check each value's prevalence (a critique label firing on 30% of TV has
   reproduced a setting), whether it separates the pair it was written for, and its correlation with the
   existing Scores. The first critique wording fired on 1 title in 500; `delta_questions.py` records the fix.

**The cost is the questions, not the article.** Jev bills question text as input at the article's rate; the
classify pass spent ~377M of its 487M input tokens on question text. A delta pass costs roughly its question
text times the corpus, so fewer, shorter definitions are the saving.
