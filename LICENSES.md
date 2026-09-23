# Licensing, per asset

The code in this repository is **MIT** (see `LICENSE`). Some of the data it produces is not, because it is
derived from sources with their own terms. A single repository licence would misstate both.

| asset | licence | why |
|---|---|---|
| everything in `pipeline/`, `lib/`, `store/`, `guards/`, `scripts/` | MIT | ours |
| `data/genres-moods-vocabulary.json` — the genres & moods vocabulary | MIT | ours; a controlled vocabulary we wrote |
| the rest of `data/*.json` — premise tags, eval rulers, corpus ids, alias decisions | MIT — but read the note below on the premise tags | LLM output and our own derivations over text we do not redistribute |
| `data/eval/reco-cases.json` | **MovieLens terms** — research, non-commercial, redistributable only under the same conditions; cite Harper & Konstan 2015, https://doi.org/10.1145/2827872 | a transformation of ml-32m (GroupLens); eval data, never in a release |
| `data/plots-sidecar-v1.json` | MIT | hashes and lengths; carries no source text |
| release `data-latest` — the store and its manifest | MIT for the derivations; see below | derived signals, not redistributed source text |
| release `corpus-<ver>` | MIT | same |
| Wikipedia article text | **CC BY-SA 4.0** | not currently published; see below |

## The Wikipedia text

The article dump is verbatim Wikipedia prose. CC BY-SA 4.0 permits redistribution and requires
**attribution** and **ShareAlike**: a work derived from it must carry the same licence. Every row names its
source revision (`article`, `resolvedArticle`, `revId`), so the citation for any row is
`https://en.wikipedia.org/w/index.php?oldid=<revId>`.

This is why the prose is not in git: a repository whose licence is MIT cannot offer Wikipedia text. It is
also not published today — the one release that carried it was deleted (below), and a republication has
to drop the TMDB fields first.

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

The store is a **public** release asset on a public repo, so "redistribute" is literal here. **It carries
no TMDB-sourced column.** The only TMDB thing in it is the key's `tmdbId`, an identifier, which stays.

The four that used to be there went in oxyc/den#118, and the live `data-latest` store is that rebuild — its
manifest records `storeRebuild: "oxyc/den#118 — card_poster and votes removed, card_title/card_year from
Wikidata"`:

| column | was | now |
|---|---|---|
| `card_title` | TMDB, with a Wikidata label as fallback | Wikidata (`facts.titles`) |
| `card_year` | TMDB | Wikidata (`facts.released`) |
| `votes` | TMDB `vote_count` | gone; a reader that orders by popularity asks TMDB at run time and never stores it |
| `card_poster` | a TMDB poster path | gone; posters are not fetched from TMDB at all |

`build_store.py` is where that is enforced rather than asserted: `PROVENANCE` names the source of every
section, `VENDOR_ALLOWED` — the TMDB-sourced sections a build may still emit — is **empty**, and a build
whose sections are not exactly the declared set refuses to write.

**The store is the only artifact that check covers.** The enrichment asks TMDB nothing since
oxyc/den-dataset#53, and a batch it writes holds no TMDB field — `pipeline/enrich.NOT_WRITTEN`, which the
end-to-end run asserts. Batches written before that, `out-repass/` among them, are TMDB detail records by
construction. Those are local working files and must not be published. Two
releases that published them by hand — `articles-2026-09-19` and `raw-2026-09-20` — were deleted on
2026-09-22 for exactly that reason; nothing in code produced or checked them, so no guard saw them. `genres` are Wikidata Q-ids mapped into TMDB's
genre *id space* — the values are CC0 and the vocabulary is TMDB's. No TMDB prose is redistributed at all:
no overview, no tagline, no review.
Enrichment prose is sourced from Wikipedia specifically so that holds, and
`scripts/v2/assert_compliance.py` proves from the batch files on disk that no TMDB prose reached a model.
See `data/README.md`.

A rating *score* is never published, and a durable artifact is the reason: den-atlas draws the same line
at `src/recommend.rs`, which scrubs vote fields out of replay files because they belong in a live request
or a bounded cache, not in something kept. `votes` was the one column on the wrong side of that line until
oxyc/den#118 removed it.

## IMDb, and popularity

IMDb's [non-commercial datasets](https://developer.imdb.com/non-commercial-datasets/) are licensed for
personal, non-commercial use only and must not be used to build a database, and IMDb's help pages say that
does not extend to public websites. Den publishes this dataset and serves a public site, so **this
pipeline uses none of them** — admission is TMDB's count or Wikidata's Wikipedia count
(`pipeline/floors.py`) — and den-atlas's run-time join of `title.ratings` is being removed on the same
grounds. What the store ships is the
**IMDb id** (99.96% of rows), and it ships as a Wikidata fact — P345, CC0 — like every other identifier
here. Popularity, where a reader needs it, comes from TMDB at run time under TMDB's caching terms.

## The derived signals

Labels, vectors, facets and the store are derived measurements — embeddings, classifications, counts — not
copies of the text they were computed from, and are offered under MIT with the rest of our work. Where a
derivation is close enough to the source to be a derivative work, the source's terms govern; the article
text is the only such case here, and it is licensed above.
