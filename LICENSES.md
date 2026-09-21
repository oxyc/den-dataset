# Licensing, per asset

The code in this repository is **MIT** (see `LICENSE`). Some of the data it produces is not, because it is
derived from sources with their own terms. A single repository licence would misstate both.

| asset | licence | why |
|---|---|---|
| everything in `scripts/`, `Sources/`, `Tests/` | MIT | ours |
| `data/*.json` — premise tags, eval rulers, corpus ids, alias decisions | MIT — but read the note below on the premise tags | LLM output and our own derivations over text we do not redistribute |
| `data/plots-sidecar-v1.json` | MIT | hashes and lengths; carries no source text |
| release `articles-<date>` — Wikipedia article text | **CC BY-SA 4.0** | it IS Wikipedia text |
| release `data-latest` — labels, vectors, facts, the store | MIT for the derivations; see below | derived signals, not redistributed source text |
| release `corpus-<ver>` | MIT | same |
| release `raw-<date>` — the paid model passes | MIT | our model output |

## The Wikipedia text

`articles-<date>` is verbatim Wikipedia prose. CC BY-SA 4.0 permits redistribution and requires
**attribution** and **ShareAlike**: a work derived from that file must carry the same licence.

Every row names its source revision (`article`, `resolvedArticle`, `revId`), so the citation for any row is
`https://en.wikipedia.org/w/index.php?oldid=<revId>` — the exact revision, and through its history, its
authors.

This is why the prose is not in git. `data/wikipedia-plots-v1.jsonl.gz` used to hold 38,460 plot summaries
here, in a public repository whose only licence file says MIT — which is not a licence Wikipedia text can
be offered under. It is now published on its own tag, under its own terms, with the attribution fields the
licence asks for.

## The premise tags, and why MIT is a judgement rather than a fact

`data/premise-tags-v1.json` and `-v2.json` are short structural descriptors — four to twelve hyphenated
phrases per title — generated per-title from Wikipedia plot summaries, which are CC BY-SA 4.0. Whether an
abstractive description of a text is a derivative work of it is not settled by anything we can point at.

The argument for MIT: the spec that produced them forbade proper nouns and genre words, so a tag names a
STRUCTURE rather than reproducing expression; no plot text survives into the output; and the same
reasoning applies to an embedding, which nobody treats as a derivative work. The argument against: they
could not exist without the source, and they are produced per-title rather than as a corpus-wide
statistic.

We take the first view. It is stated here as a position rather than a fact so that anyone relying on it
can weigh it, and so that the CC BY-SA text itself — which is unambiguous — is not confused with it.
`vectors-premise.bin`, being embeddings of those tags, follows the same reasoning at one further remove.

## TMDB

Nothing here redistributes TMDB Content beyond posters, ids and titles. Enrichment prose is sourced from
Wikipedia specifically so that rule can hold, and `scripts/v2/assert_compliance.py` proves from the batch
files on disk that no TMDB prose reached a model. See `data/README.md`.

## The derived signals

Labels, vectors, facets and the store are derived measurements — embeddings, classifications, counts — not
copies of the text they were computed from, and are offered under MIT with the rest of our work. Where a
derivation is close enough to the source to be a derivative work, the source's terms govern; the article
text is the only such case here, and it is licensed above.
