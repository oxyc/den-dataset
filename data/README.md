# `data/` — the source material, committed

Everything here is **expensive to regenerate and impossible to reconstruct from the published blobs**. The
release assets on `data-latest` are *derived*; these are the inputs they were derived from.

This directory exists because the provenance kept living in people's heads. Twice in one session the premise
index was described wrongly — "built by a stale embedder", "possibly from TMDB overviews" — because the only
record of how it was made was a spec file in a working directory that was never committed. A vector space
whose source text is unpublished cannot be debugged, audited, or trusted.

**The TMDB rule applies to every file here: no TMDB Content except posters, ids and titles.** Fields that
came from TMDB (`genres`, `voteCount`, `originalLanguage`) are stripped on the way in. Plot text is
Wikipedia; premise tags are LLM output over Wikipedia text; facts are Wikidata.

| file | what it is | cost to rebuild |
|---|---|---|
| `wikipedia-plots-v1.jsonl.gz` | 38,460 Wikipedia plot summaries, one JSON object per line | ~38k live article fetches |
| `premise-tags-v1.json` | 37,533 titles × 4–12 structural premise tags | a full LLM pass over every plot |
| `premise-tags-v1.SPEC.md` | the prompt that produced them | — |
| `corpus-ids.json` | the id set the v2 corpus was built over | cheap, but pins what "the corpus" meant |
| `eval/reco-cases.json` | 6,000 co-rating cases (nPMI), the recommendation ruler | a full co-rating derivation |
| `eval/triplets-*.json` | LLM-judged similarity triplets at 1/2/3 blind passes | several blind LLM judging passes |

## `wikipedia-plots-v1.jsonl.gz`

One object per line: `key` (`mediaType:tmdbId`), `tmdbId`, `mediaType`, `title`, `year`, `plot`,
`plotChars`, `plotSHA`, `shipped`.

`plotSHA` is what makes this useful beyond archival — it identifies the exact plot text a vector was built
from, so a corpus can be checked for drift without refetching. `shipped` records whether the title made the
published index.

Plot lengths are wildly skewed: median 2,515 chars, p90 4,329, max **53,299**. The long tail is mostly
long-running series whose Wikipedia "plot" is a season-by-season recap. Anything that truncates should know
that the first 3,500 characters of such an article is season one.

## `premise-tags-v1.json`

The strings behind `vectors-premise.bin`. Keyed `mediaType:tmdbId` — never a bare id, because this corpus
contains ids that are both a film and a series (movie 95 is *Armageddon*, tv 95 is *Buffy*).

The spec forbade **proper nouns and genre/mood words**, which explains both measured properties of this
index: it cannot serve character search, and it beats the plot index at similarity by **+11.3 pp on a sealed
test half** (χ² = 4.00, p < 0.05 under two-judge unanimity).

`coverageFilled` names 219 titles (166 films, 53 series, *The Dark Knight* among them) whose tags came from
the later v2 tagger rather than the v1 spec. Only `premise` and `subject` kinds were taken from those —
v2 also emits `tone-setting`, which is exactly the mood vocabulary v1 bans.

## `eval/`

The ruler the premise-vs-plot result was measured on, and the judged triplets behind it. Keep these: the
**TEST half has been spent once**, so the count of independent reads is a resource, and rebuilding the ruler
means re-running blind LLM judging.

Two things the judging established, both worth knowing before trusting a number from it:

- **Blind judges disagreed on 25.2% of triplets**, and that rate *grew* with every sample — 17.5% at n=40,
  20.0% at n=120, 25.2% at n=445. Treat 25% as a floor, not a settled value.
- The premise advantage **grew as the bar rose** (+9.0 pp on the proposer's own labels, +11.4 pp under
  two-judge unanimity). That is the signature of confirmation removing noise rather than signal.

## What is deliberately NOT here

- **`out-t02/enriched/`** — the TMDB enrichment. It is TMDB Content (overviews, cast, genres, vote counts)
  and must not be published. It is also cheaply refetched with a key.
- **The vectors and labels themselves** — those are the published release assets on `data-latest`, fetched
  by `scripts/fetch-dataset.sh` (atlas) and `make sync-dataset` (the app). Committing them would duplicate
  ~100 MB that already has a distribution channel.
- **`premise-tags-wip/`** — 624 unmerged batch files, now consolidated into `premise-tags-v1.json` by
  `scripts/build-premise-tags.py`.
