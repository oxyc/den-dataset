# Franchise artifacts

The franchise stage writes two public, prose-free JSON artifacts. Raw `franchise-states*.jsonl` and
`franchise-answers-v1*.jsonl` stay in the operator's private work directory: they contain Wikipedia text
and provider audit envelopes and are never put in the corpus bundle.

## Durable decisions

`franchise-decisions-v1.json` is the resume state for a stateless daily run. `schema` is `1`; `model` and
`questionsSha256` pin what made the decisions. `decisions` is keyed by `movie:<tmdb id>` or `tv:<tmdb id>`.
Each row contains only:

- `candidates`: stable Wikidata/group ids (or a `characters` tuple of corpus keys);
- `answers`: the typed Choice and two Noul answers;
- `model`: the response model for that call;
- `usage`: exact input and output token counts.

The top-level `usage` totals the rows and records exact spend at the pinned Jev input-token rate. A current
raw answer is audited in full before it can enter this file. On later days its candidates must still equal
the current candidates or the decision is set aside.

## Derivation thresholds

The typed answers are evidence, not an instruction to merge every borderline candidate:

- an ordinary group choice is accepted at `0.5`;
- a possible catalogue needs average support of `0.8` across its own answered members, preventing a few
  borderline title-level choices from turning a thematic companion set into a franchise;
- a possible shared universe is never merged into a primary franchise (it can be the title's optional
  umbrella instead);
- `separate adaptation` makes an era only at `0.75`; when the selected group is a book series, a
  separate adaptation also needs a `0.85` group choice to join at all;
- a TV title in a mixed film/TV franchise gets a distinct era when Wikidata supplies no narrower child
  group.

These thresholds are pinned by the golden controls, including the full corpus-visible Beck group, the
relocated 1973 *The Laughing Policeman*, the Three Flavours Cornetto trilogy, and MCU primary-versus-
umbrella titles.

## Derived franchises schema 2

`franchises.json` is rebuilt from facts plus the durable decisions and held to the franchise golden set.
Its stable serving contract is:

```json
{
  "schema": 2,
  "datasetVersion": "…",
  "franchises": {
    "Q-franchise": {
      "id": "Q-franchise",
      "name": "Display name",
      "source": "wikidata | jev-franchise-v1",
      "confidence": 0.91,
      "eras": [{"id": "Q-franchise:era:Q-era", "name": "Era name", "order": 0,
                "members": ["movie:1"]}],
      "members": [{"key": "movie:1", "year": 1997,
                   "eraId": "Q-franchise:era:Q-era", "order": 0}]
    }
  },
  "titles": {
    "movie:1": {"primary": "Q-franchise",
                 "umbrella": {"id": "Q-universe", "name": "Universe name"}}
  },
  "derivation": {"…": "quality and provenance record"}
}
```

`titles[key].umbrella` is omitted when none is known. It is informational and is never used for More Like This
exclusion. `titles[key].primary` is the exclusion group. Era ids come from the underlying Wikidata group;
the unsplit main era uses `<franchise id>:era:main`, and a separately judged adaptation uses its corpus key.
Both `eras` and `members` are release ordered, with zero-based `order` made explicit. A franchise's
confidence is the lowest accepted member/merge confidence; an all-Wikidata franchise is `1.0`.
