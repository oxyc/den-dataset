# Premise tags v2 — subagent prompt

Two hard constraints on how you use tools, before anything else:

1. **One command per Bash call.** No `&&`, no `;`, no `|`. Compound commands trigger a
   permission prompt and stall the run.
2. **Never point `grep`, `rg`, `find` or `ls` at a directory tree.** Read files with the
   Read tool. This task needs exactly one Read and one Write, and no searching.
3. **Do not spawn subagents.** Do the work yourself.

## Task

Read `{IN_PATH}` — a JSON array of `{key, title, year, mediaType, plot}`. The plots are
Wikipedia plot summaries (long ones are excerpted head-and-tail, marked `[…]`).

For **every** title, emit a **rich superset of premise tags** describing the film's
structural premise, ordered **most-defining first**.

## What a tag is

A tag is a terse, lowercase **kebab-case** label of 2–5 words naming a structural premise
or trope. It is a *label*, never a sentence.

Good: `reassigned-phone-number`, `messages-to-the-dead`, `heist-gone-wrong`, `time-loop`,
`enemies-to-lovers`, `undercover-cop`, `trapped-in-one-location`, `body-swap`,
`wrongful-imprisonment`, `revenge-quest`, `wise-mentor-sacrifices-himself`,
`arranged-marriage-escape`, `last-job-before-retirement`.

**A tag must never be a sentence-length synopsis.** `young-farmer-joins-rebels-to-destroy-
space-station-after-mentor-dies` is not a tag; it is an abridgement of the plot, and
abridging a Wikipedia plot is a licence violation under CC BY-SA. Five words is the ceiling
and most good tags are two or three. If you find yourself encoding *what happens in this
particular film* rather than *what kind of story it is*, split it into shorter tags.

**No proper nouns.** No character names, place names, countries, franchises, real people or
brands. `jedi-knight-training` is wrong; `warrior-monk-apprenticeship` is right.

**No genre or mood words as tags.** Not `romance`, `thriller`, `drama`, `comedy`, `scary`,
`feel-good`. Those live on another axis of the index and would drown the premise signal.
(They *are* allowed as the `tone-setting` kind below when they describe the *setting or
register the premise inhabits* — e.g. `small-town-claustrophobia`, `courtroom-procedural` —
but never a bare genre word.)

## How many, and with what

Emit **14–20 tags per title**, most-defining first. This is deliberately a *superset*:
downstream a cutoff selects a subset for embedding, and that cutoff is swept cheaply. Your
job is coverage and correct ordering, not economy. Fewer than 14 is acceptable only for a
very thin plot; never pad with restatements of a tag you already emitted.

Each tag carries:

- **`salience`** — an integer 1–5. `5` = a viewer would put this in the first sentence
  describing the film. `1` = present and real, but incidental. Rank order is the primary
  signal and salience is secondary, so keep them consistent: the list must be sorted by
  descending salience.
- **`kind`** — exactly one of:
  - `premise` — the core situation or story engine. *(Most titles have 2–5.)*
  - `trope` — a recognisable recurring story device that is not the core premise.
  - `subject` — what the story is *about* as material: an occupation, a milieu, an event
    type (`deep-sea-salvage`, `competitive-ballroom`, `witness-protection`).
  - `tone-setting` — the register or world the premise inhabits
    (`small-town-claustrophobia`, `bureaucratic-absurdity`, `sword-and-sandal-epic`).

## Base everything on the plot text given

Do not use anything you happen to know about the film. If the plot text does not contain it,
it is not a tag. This matters for provenance: the tags must be derivable from the supplied
Wikipedia plot alone.

## Output

Write a JSON array to exactly `{OUT_PATH}` — one object per title, in input order, all of
them. Nothing else: no prose, no markdown fence, no commentary.

```json
[
  {
    "key": "movie:11",
    "tags": [
      {"tag": "chosen-one-destiny", "salience": 5, "kind": "premise"},
      {"tag": "ragtag-rebels-vs-empire", "salience": 5, "kind": "premise"},
      {"tag": "wise-mentor-sacrifices-himself", "salience": 4, "kind": "trope"},
      {"tag": "stolen-superweapon-plans", "salience": 4, "kind": "premise"},
      {"tag": "princess-in-peril-rescue", "salience": 3, "kind": "trope"},
      {"tag": "desert-planet-upbringing", "salience": 2, "kind": "tone-setting"},
      {"tag": "hired-smuggler-ally", "salience": 2, "kind": "trope"},
      {"tag": "dogfight-assault-run", "salience": 1, "kind": "subject"}
    ]
  }
]
```

Rules that make the output usable:
- Every `key` copied **verbatim** from the input. Never invent or reformat one.
- Emit an object for **every** title in the batch. A missing title fails the batch.
- **Never emit `"tags": []`.** Every title in a batch has a real Wikipedia plot, so there is
  always something to tag. An empty list is what truncation looks like, and it is counted as a
  failure rather than a terse answer. If you are running low on output budget, shorten the
  tags — do not stub the remaining titles.
- `tag` matches `^[a-z0-9]+(-[a-z0-9]+){1,4}$` — lowercase, hyphens only, 2–5 words.
- No duplicate tags within a title.
- Sorted by descending `salience`.
