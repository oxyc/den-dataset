# `data/` — the committed inputs

What is here is expensive or impossible to regenerate, and cannot be reconstructed from the published
store. An artifact whose source is not committed gets described wrongly: the premise index was twice called
stale or TMDB-derived by someone reading file sizes, because its tags and prompt lived in a gitignored
directory.

Nothing here carries TMDB content beyond ids. Plot-derived files are LLM output over Wikipedia text; facts
are Wikidata. Licences per file are in [`LICENSES.md`](../LICENSES.md).

| file | what it is | to rebuild it |
|---|---|---|
| `genres-moods-vocabulary.json` | the genres & moods label names, per family | ours; hashed into every classify shard's manifest as `taxonomySha256` |
| `genres-moods-definitions.json` | what each of those labels means — what a labeller is asked | — |
| `prompts/facets-v2.md` | the plot-facet axes the classify pass asks, parsed into its questions | ours; hashed into every classify shard's manifest as `promptSha256` |
| `prompts/facets-v1.md` | the nine-axis v1 facet prompt its census was run under | — |
| `genres-moods-curated.json` | genres & moods for 47,539 titles, with each title's source | not rebuildable ([#56](https://github.com/oxyc/den-dataset/issues/56)); extended by `./den genres-moods` (`AGENTS.md`) |
| `genres-moods-rule.json` | the per-label thresholds the `genres_moods` stage derives with, fitted on golden half A | ~$0.31 of Jev plus the fitting |
| `premise-tags-v1.json` | 37,533 titles × 8–12 structural premise tags, the first generation | a full LLM pass |
| `premise-tags-v2.json` | 44,697 titles — every title the premise index covers, plus 166 appended 2026-09-22 and not yet embedded | a full LLM pass; later runs append with `merge_premise_tags.py --into` |
| `premise-tags-v1.SPEC.md`, `-v2.SPEC.md` | the prompts those tags were generated under; the tag files' `derivedFrom` cites them | — |
| `plots-sidecar-v1.json` | 38,460 plot identities (`plotSHA`, length, shipped) and no prose | ~38k article fetches |
| `embed-canary.json` | fixed texts and the exact int8 vectors den-embed must return for them | only when the space is meant to move (`docs/OPERATE.md`) |
| `alias-decisions.json` | keep/drop judgements on alternate titles that collide with another title's name | by hand |
| `iconic-studios.json` | the studios a viewer browses by, each with every Wikidata item that is the same studio; the store writes the ones the corpus credits | by hand ([den#132](https://github.com/oxyc/den/issues/132)) |
| `award-ceremony-merges.json` | awarding bodies Wikidata splits across an organisation and its "Awards" group, each `from` filed under its `into` before the store's ceremony table is built; `check-award-merges.py --gate` refuses a stale one | by hand ([den#135](https://github.com/oxyc/den/issues/135)) |
| `wikidata-item-decisions.json` | which Wikidata item answers for a title whose TMDB id several items claim, where no rule decides | by hand |
| `implementation-lineage.json` | the superseded source and input digests a paid classify shard may still be audited on, each with the reason its rows did not move | by hand, after reading the diff |
| `classify-queue.json` | titles whose labels were not read from the plot the corpus now holds | derived |
| `eval/golden-large.json` | 2,568 hand-labelled titles, the genres & moods quality ruler | by hand |
| `eval/quality-floors.json` | the scores a publish is held to (`scripts/eval-taxonomy.py`) | recorded, not rebuilt |
| `eval/reco-cases.json` | 6,000 MovieLens co-rating cases (nPMI), the recommendation ruler | the co-rating derivation |
| `eval/triplets-*.json` | blind-judged premise-similarity triplets at 1, 2 and 3 judging passes | several blind LLM passes |

## Premise tags

Short kebab-case tags per title, most defining first — `heist-gone-wrong`, `messages-to-the-dead` — naming the story's
structure. The prompts forbade proper nouns and genre or mood words. That is why the premise index cannot
serve character search, and why it beats the plot index at "more like this" (+11.3 pp on the sealed test
half, below).

- **v2 is the one to embed from.** It is complete for the index; before 2026-09-19, 999 of its strings
  lived only in a gitignored directory and a rebuild came up short ([#13](https://github.com/oxyc/den-dataset/issues/13)).
- **v1 is kept as its generation's record.** Its `vectorsPublished` / `vectorsMissingFor` fields describe
  the July blob, not today's.
- **A premise vector blob aligns to the `labels-premise.json` built beside it**, never to a tags file. The
  live one has 44,531 rows.
- Keys are `mediaType:tmdbId`, never a bare id: movie 95 is *Armageddon*, tv 95 is *Buffy*.

## Plots

The prose is not in git: Wikipedia text is CC BY-SA 4.0, and this repo's licence is MIT. The sidecar keeps
what anything reads — `plotSHA` identifies the exact text a vector was built from, so drift can be checked
without refetching. The old file is untracked rather than purged from history, because rewriting a public
repo's history breaks every clone and quoted SHA while GitHub keeps the objects anyway.

Plot lengths are heavily skewed: median ~2,500 characters, max 86,443 after the September re-ground. The
tail is long-running series whose plot is a season-by-season recap, so the first 3,500 characters of such
an article is season one.

## `eval/` — the rulers

**Co-rating** (`reco-cases.json`): MovieLens ml-32m nPMI pairs, covering 86.5% of shipped movies and no
series (ml-32m has no TV). Eval-only under MovieLens's research licence; never bundled into a release.

**Premise triplets** (`triplets-*.json`): an anchor, a positive and a hard negative. Two titles share a
premise when a viewer would say the same "it's the one where ___" for both — the story engine, not the
genre, tone or setting. A judge sees plots only (no tags, labels or popularity) and answers six yes/no
axes: situation, engine, goal, relationship, obstacle, device. A positive is a **twin** (4+ axes, one of
them situation or device); a hard negative is **unrelated** (0–1) yet confusable (same genre, era or
subject). An anchor with no twin yields no triplet. The axis count is **not** a difficulty gradient —
premise accuracy was flat across 4, 5 and 6 — so never weight by it.

Both rulers split DEV/TEST by `scripts/v2/split.py`, a hash of the key, so a title is in the same half in
both. **TEST has been read once**, for the premise-vs-plot result; sweep on DEV, commit the setting, then
read TEST. Compare two arms with `paired_triplets.py` (McNemar on the discordant cases): at ~150 triplets
one flipped case moves accuracy 0.66 pp, and a tie fails, because replacing a working system has its own
risk.

What the judging established:

- **Blind judges disagree on at least 25% of triplets**, and the rate rose with every sample (17.5% at
  n=40, 25.2% at n=445).
- **The premise advantage grew as the bar rose**: +9.0 pp on the proposer's labels, +11.4 pp under two-judge
  unanimity (χ² = 4.00, p < 0.05), +11.3 pp on TEST. That is confirmation removing noise, not signal.
