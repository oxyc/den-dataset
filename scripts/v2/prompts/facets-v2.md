# Plot-derived facets — Jev v2 candidate vocabulary

Read one Wikipedia article about a film or television series. Answer every axis from the article text only.
These are closed-vocabulary browse facets, not premise tags. Pick the dominant answer. If the article does not
support an answer, use `does-not-apply`; do not guess from outside knowledge.

## Closed vocabularies

- **`era`** — when the story is set, not when it was made:
  `prehistoric` · `ancient` · `medieval` · `early-modern` · `19th-century` ·
  `early-20th-century` · `mid-20th-century` · `late-20th-century` · `contemporary` ·
  `near-future` · `far-future` · `timeless`

- **`setting`** — the dominant physical world:
  `urban` · `suburban` · `small-town` · `rural` · `wilderness` · `sea` · `space` ·
  `underground` · `institution` (school, prison, hospital, barracks) · `domestic` ·
  `road` (travel is the setting) · `virtual`

- **`scope`** — how much world the story covers:
  `single-location` · `single-city` · `regional` · `national` · `global` · `cosmic`

- **`ending`** — how the main story resolves:
  `happy` · `bittersweet` · `tragic` · `ambiguous` · `open` (sequel-shaped or unresolved) ·
  `cyclical` (ends where it began) · `unknown` (the article genuinely does not say)

- **`pacing`** — the story's dominant tempo, independent of whether its structure is episodic or serialized:
  `slow-burn` (deliberate accumulation toward a later payoff) ·
  `measured` (even and unhurried without a pronounced slow-build payoff) ·
  `brisk` (frequent forward movement with limited downtime) ·
  `relentless` (sustained high urgency or action with few pauses)

- **`structure`** — how the telling is arranged; `episodic` belongs here, never under pacing:
  `linear` · `nonlinear` · `framed` (story within a story) · `parallel-strands` ·
  `episodic` (successive substantially self-contained incidents or episodes) ·
  `anthology` (separate stories with different central characters or worlds)

- **`conflict`** — the primary opposition:
  `person-vs-person` · `person-vs-self` · `person-vs-society` · `person-vs-nature` ·
  `person-vs-system` · `person-vs-unknown`

- **`ensemble`** — the narrative focus, not the number of credited or mentioned characters:
  `single-lead` (one clear protagonist carries the main arc, however many supporting characters appear) ·
  `dual-lead` (two comparably central protagonists, often a central pair or relationship) ·
  `group-led` (roughly three to five comparably central characters share one main arc) ·
  `ensemble-led` (many coequal characters or several storylines, with no durable single center)

- **`tone`** — the dominant register:
  `earnest` · `comic` · `satirical` · `bleak` · `melancholy` · `pulpy` · `dreamlike` ·
  `clinical`

- **`archetype`** — the dominant whole-story arc, using neutral labels inspired by Booker's seven plots:
  `overcoming-threat` (confront and overcome or escape a dangerous antagonist or force) ·
  `rise` (advance from deprivation or obscurity toward status, capability or success) ·
  `quest` (pursue a concrete goal, object, person or destination through obstacles) ·
  `voyage-and-return` (enter an unfamiliar world or situation and return changed) ·
  `comic-resolution` (confusion or division resolves through reunion, reconciliation or restored order;
  this does not mean the work is humorous) ·
  `downfall` (the protagonist's flaws or choices drive irreversible ruin) ·
  `rebirth` (a trapped, diminished or morally lost protagonist undergoes renewal)

## Boundaries that matter

- A supporting cast does not turn a single protagonist into an ensemble. Count shared narrative focus.
- `episodic` and `anthology` describe structure, not tempo. An episodic work can still be slow or relentless.
- `comic-resolution` describes the shape of the resolution, not comic tone or comedy genre.
- Tone words never belong in `ending`; `bleak` and `melancholy` are tone values.
- For an anthology, documentary, open-ended series, or work with several equally strong arcs, `archetype` may
  genuinely be `does-not-apply`.

