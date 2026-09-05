# Plot-derived facets — subagent prompt (Phase 3)

Two hard constraints on how you use tools, before anything else:

1. **One command per Bash call.** No `&&`, no `;`, no `|`.
2. **Never point `grep`, `rg`, `find` or `ls` at a directory tree.** Read files with the
   Read tool. This task needs exactly one Read and one Write.
3. **Do not spawn subagents.** Do the work yourself.

## Task

Read `{IN_PATH}` — a JSON array of `{key, title, year, mediaType, plot}`, Wikipedia plot
summaries (long ones excerpted head-and-tail, marked `[…]`).

For **every** title emit one object of **structured facets**. These are browse axes — a user
filtering "period drama, bittersweet ending, one location" — not free-text tags. They are a
different axis from the premise tags and must not restate them.

## Closed vocabularies — pick from these lists ONLY

A closed vocabulary is the entire point of a facet. An open one cannot be a filter chip,
cannot be counted, and drifts across batches. **If nothing fits, emit `null` for that axis.**
Never invent a value, never combine two with a slash.

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

- **`ending`** — how it resolves. Base this on the plot text, which is why the tail is kept:
  `happy` · `bittersweet` · `tragic` · `ambiguous` · `open` (sequel-shaped, unresolved) ·
  `cyclical` (ends where it began) · `unknown` (the plot text genuinely does not say)

- **`pacing`** — the shape of the telling:
  `slow-burn` · `steady` · `propulsive` · `episodic` · `frantic`

- **`structure`** — how the telling is arranged:
  `linear` · `nonlinear` · `framed` (story within a story) · `parallel-strands` ·
  `anthology` · `single-day`

- **`conflict`** — the primary opposition:
  `person-vs-person` · `person-vs-self` · `person-vs-society` · `person-vs-nature` ·
  `person-vs-system` · `person-vs-unknown`

- **`ensemble`** — how many people carry it:
  `solo` · `duo` · `small-group` · `large-ensemble`

- **`tone`** — the register:
  `earnest` · `comic` · `satirical` · `bleak` · `melancholy` · `pulpy` · `dreamlike` ·
  `clinical`

## Confidence

Each axis carries a `confidence` of `high`, `medium` or `low`. Use `low` when the plot text
supports the value only weakly, and `null` when it does not support one at all. **A `null` is
a better answer than a guess** — a wrong facet makes a filter lie, and a user who filters on
"tragic ending" and gets a comedy stops trusting the filter entirely.

## Base everything on the plot text given

Do not use anything you happen to know about the film. If the plot text does not contain it,
it is not a facet. `ending: unknown` is correct and common for a plot that stops early.

## Output

Write a JSON array to exactly `{OUT_PATH}` — one object per title, **in input order, all of
them**. Nothing else: no prose, no markdown fence, no commentary.

```json
[
  {
    "key": "movie:11",
    "facets": {
      "era":       {"value": "far-future",       "confidence": "high"},
      "setting":   {"value": "space",            "confidence": "high"},
      "scope":     {"value": "cosmic",           "confidence": "high"},
      "ending":    {"value": "happy",            "confidence": "medium"},
      "pacing":    {"value": "propulsive",       "confidence": "medium"},
      "structure": {"value": "linear",           "confidence": "high"},
      "conflict":  {"value": "person-vs-system", "confidence": "medium"},
      "ensemble":  {"value": "small-group",      "confidence": "high"},
      "tone":      {"value": "earnest",          "confidence": "medium"}
    }
  }
]
```

Rules that make the output usable:
- Every `key` copied **verbatim** from the input, in input order. Never invent or reformat one.
  Work positionally: tag input object 1, then 2, then 3. A repeated key means a dropped title.
- Emit an object for **every** title. All nine axes present on every object.
- Every `value` is either a string **from the list for that axis** or `null`. A value not on
  the list is dropped downstream and counted as a failure.
- `confidence` is `high`, `medium` or `low` — and may be `null` only when `value` is `null`.
- **Labels only. Never a sentence, never prose.** These are filter chips.
