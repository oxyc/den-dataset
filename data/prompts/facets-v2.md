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

- **`ending`** — how the main bounded narrative resolves, never the work's overall tone; use `does-not-apply` for a non-narrative or open-ended program:
  `happy` · `bittersweet` · `tragic` · `ambiguous` · `open` (sequel-shaped or unresolved) ·
  `cyclical` (ends where it began) · `unknown` (the article genuinely does not say)

- **`pacing`** — the bounded narrative's dominant tempo; ignore whether it is episodic or serialized, and never use broadcast frequency or production history as tempo:
  `slow-burn` (deliberate accumulation toward a later payoff) ·
  `measured` (even and unhurried without a pronounced slow-build payoff) ·
  `brisk` (frequent forward movement with limited downtime) ·
  `relentless` (sustained high urgency or action with few pauses) ·
  `does-not-apply` (required when the article does not reveal tempo or the work is a talk, variety, game, news, reality or other program without a bounded narrative)

- **`chronology`** — the dominant arrangement of presented story time, not article order or broadcast history; choose `framed` first when an explicit enclosing frame is central, otherwise `parallel-strands` for coequal intercut strands, otherwise `nonlinear` for substantially rearranged time, and use `linear` only when the article supports mainly chronological presentation:
  `linear` (events are presented mainly in chronological order) ·
  `nonlinear` (chronology is substantially rearranged through flashbacks, loops or reverse order) ·
  `framed` (the main story is recounted inside an explicit present-day or storyteller frame) ·
  `parallel-strands` (multiple timelines or storylines are intercut as coequal strands) ·
  `does-not-apply` (required for a non-narrative program or when the article does not establish presentation order)

- **`continuity`** — how narrative units connect, independent of chronology or tempo; use `does-not-apply` for a program without narrative units:
  `continuous` (one main narrative progresses continuously; the ordinary film or serialized series case) ·
  `episodic` (successive substantially self-contained incidents or episodes) ·
  `hybrid` (self-contained units and a continuing main arc are both substantial) ·
  `anthology` (separate, non-interacting stories or segments; intersecting plots in one shared story are not an anthology)

- **`timespan`** — how much story time the main bounded narrative covers, never runtime, years on air or production history; use `does-not-apply` for a non-narrative program:
  `single-day` · `several-days` · `weeks-or-months` · `years` ·
  `multi-generational` (the story follows several family or social generations, not merely one long-lived character)

- **`conflict`** — the primary opposition:
  `person-vs-person` · `person-vs-self` · `person-vs-society` · `person-vs-nature` ·
  `person-vs-system` · `person-vs-unknown`

- **`ensemble`** — the narrative focus, not cast size; supporting characters do not turn one clear protagonist into an ensemble, and a host or presenters do not form a narrative ensemble:
  `single-lead` (one clear protagonist carries the main arc, however many supporting characters appear) ·
  `dual-lead` (two comparably central protagonists, often a central pair or relationship) ·
  `group-led` (roughly three to five comparably central characters share one main arc) ·
  `ensemble-led` (many coequal characters or several storylines, with no durable single center)

- **`tone`** — the dominant register, never the shape of the ending:
  `earnest` · `comic` · `satirical` · `bleak` · `melancholy` · `pulpy` · `dreamlike` ·
  `clinical`

- **`archetype`** — the dominant whole-story arc, not genre; assign an archetype only to a bounded fictional or dramatized protagonist arc:
  `overcoming-threat` (confront and overcome or escape a dangerous antagonist or force) ·
  `rise` (advance from deprivation or obscurity toward status, capability or success) ·
  `quest` (pursue a concrete goal, object, person or destination through obstacles) ·
  `voyage-and-return` (enter an unfamiliar world or situation and return changed) ·
  `comic-resolution` (confusion or division resolves through reunion, reconciliation or restored order;
  this does not mean the work is humorous) ·
  `downfall` (the protagonist's flaws or choices drive irreversible ruin) ·
  `rebirth` (a trapped, diminished or morally lost protagonist undergoes renewal) ·
  `does-not-apply` (required for documentaries, reality, talk, variety, game or news programs, anthologies, open-ended series, or several equal arcs; never map a documentary subject's life or a program's history onto an archetype)
