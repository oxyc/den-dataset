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

`franchise-line-decisions-v1.json` is the same for the line pass, in the same shape with its one Noul
answer, `fr__continues_line`. Its private states and answers are `franchise-line-states*.jsonl` and
`franchise-line-answers-v1*.jsonl`. The line pass asks only the titles the franchise pass calls a
separate adaptation (`0.75`), in the same `--spend` run, and the daily job buys both asks when it can buy.

## Derivation thresholds

The typed answers are evidence, not an instruction to merge every borderline candidate:

- an ordinary group choice is accepted at `0.5`;
- choosing a group Wikidata nests inside another listed group (the Eon series inside James Bond, the MCU
  Spider-Man films inside Spider-Man in film) is choosing the bigger group's franchise, in the chosen
  group's era; the answer's weight on the franchise and every listed group inside it counts together;
- a possible catalogue needs average support of `0.8` across its own answered members, preventing a few
  borderline title-level choices from turning a thematic companion set into a franchise;
- a possible shared universe is never merged into a primary franchise (it can be the title's optional
  umbrella instead);
- two groups merge only when titles of both say they are one franchise; a title that is a separate
  adaptation casts no merge vote;
- two groups with no title in common were listed together only because their names share a word, and
  most such pairs are unrelated (Die Hard and *A Hard Day's Night*). They merge only on evidence beside
  the vote: a Wikidata series or media franchise holding titles of both, three or more actors credited in
  both (the 1993 Martin Beck films and the 1997– Beck films), or a title of one whose answer chose the
  other (the Norwegian *Olsenbanden* choosing the Danish Olsen Gang);
- a franchise is one continuity or rights line (the owner's rule, oxyc/den-atlas#92). A separate adaptation
  the line pass says continues the line at `0.75` is one more title of it and starts no era: *Red Dragon*
  (2002) adapts the novel *Manhunter* did, and is the Hopkins films' prequel. Otherwise a `separate
  adaptation` answer at `0.75` is another production and joins no franchise, unless Wikidata keeps it in
  the line: in a series narrower than the franchise with other titles (Eon's 2006 *Casino Royale*), or,
  when the franchise has no such series, named by the franchise's own series item or heading its sequel
  chain. A book series' adaptations or a character link are never a line. A title that stays starts an
  era, with every later title of the era it was in;
- a TV title in a mixed film/TV franchise gets a distinct era when Wikidata supplies no narrower child
  group.

Members and eras are in release order: the year, then Wikidata's finer date within it where it gives one.

These thresholds are pinned by the golden controls, including the full corpus-visible Beck group, the
relocated 1973 *The Laughing Policeman*, the Three Flavours Cornetto trilogy, MCU primary-versus-umbrella
titles, and the separate productions kept apart (the British *Wallander*, the non-Eon Bond films).

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
the unsplit main era uses `<franchise id>:era:main`, and a reboot's era (or a TV title's own era) uses
`adaptation:<corpus key>` of the title that starts it.
Both `eras` and `members` are release ordered, with zero-based `order` made explicit. A franchise's
confidence is the lowest accepted member/merge confidence; an all-Wikidata franchise is `1.0`.
