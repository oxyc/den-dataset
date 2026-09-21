<!--
The prompt that generated `premise-tags-v2.json` — the v2 index's 44,531 tag sets.

It lived only in `legacy/out-t02/premise-tags-wip/GEN-SPEC-PROD.md`, a gitignored working directory, which
is the same failure #13 fixed for the tags themselves: `premise-tags-v2.json`'s own `derivedFrom` field
names this file, and a fresh checkout did not have it. The tags cannot be regenerated, audited or extended
without the rules they were generated under.

Provenance, traced 2026-09-21: `out-premise-v2/gen/out/` produced them under this spec — 5,773 of the
sampled tag sets are byte-identical to the committed file and 204 are the committed file with tags removed,
which is the "ban tags that name a storytelling property" filter that ran after. `legacy/out-t02/v2/tags-v2`
holds the same KEYS with different tags: it is the superseded generation, not the producer.
-->

You generate PREMISE TAGS for a film-similarity index. You are given ONLY a film's plot (a Wikipedia plot
summary). Output the film's core structural premise as a list of open-vocabulary tags.

Rules:
- Emit 8–12 tags, **ordered MOST-DEFINING-FIRST**. The first ~5 must be the premise a viewer would use to
  say "it's the movie where ___" (the story engine, central relationship, core situation); later tags may be
  secondary tropes. Ordering is the salience signal — put the essence first.
- Each tag: lowercase-kebab-case, terse (2–4 words), a STRUCTURAL premise/trope. Good:
  `messages-to-the-dead`, `reassigned-phone-number`, `heist-gone-wrong`, `time-loop`, `enemies-to-lovers`,
  `undercover-cop`, `trapped-in-one-location`, `body-swap`, `wrongful-imprisonment`, `revenge-quest`.
- NO proper nouns — no character names, places, countries, franchises, real people, brands.
- NO genre or mood words — not `romance`, `thriller`, `scary`, `feel-good`, `drama`, `comedy`.
- Base tags ONLY on the plot text provided. Do not invent events not in the plot.

You will be given a batch: a JSON array of {tmdbId, plot}. For EVERY film, output one object
{"tmdbId": <int>, "tags": ["most-defining", "...", ...]}. Cover every film; no proper nouns; 8–12 tags each.
