# den-dataset — project instructions

## Use the best inputs we have. Always.

If a better version of an input exists, the artifact is built from it. Do not propose rebuilding from a
known-worse input to make a measurement cleaner — shipping a worse index to isolate a variable is the wrong
trade, every time. Measure the change some other way, or measure it after.

Concretely, as of 2026-09: `out-repass/` holds the **re-ground** Wikipedia plots (38,532 titles, 199
enriched batches). The live corpus was built from the older `out-t02/enriched` plots. Any plot re-embed uses
the re-ground plots.

## `out-*` is gitignored — deleting it is permanent

`.gitignore:8` excludes `out-*/`. Git history will NOT bring it back. These directories hold the only copies
of enriched plot text, vectors, and LLM run outputs that cost real time and money to produce:

```
out-repass/enriched      the re-ground plots — the newest and best grounding
out-t02/enriched         the older plots the live corpus was built from
out-premise-v2/          the v2 premise run + its vectors
out-premise-999/         999 tag strings that exist nowhere else (see #13)
```

Never delete one to save space without asking, and never assume "it's in git".

## Derived artifacts are rebuildable only if their INPUTS are committed

The premise index shipped for months while 999 of its tag strings lived only in a gitignored directory
(#13). A rebuild from a fresh checkout came up short, and the failure looked like a regression to a state
someone had already fixed. Committed sources now: `data/premise-tags-v2.json` (44,531, complete),
`data/premise-tags-v1.json` (37,533, kept because `vectors-premise.bin` is aligned to its exact strings).

## Embed where you serve

Corpus vectors and live query vectors must come from the same `den-embed` **instance**, not the same
version string. See `docs/OPERATE.md` "The alignment rule" — it is the current state and the procedure.
`docs/LESSONS.md` is why. Do not restate either elsewhere; one copy, kept true.

## Docs

- `docs/OPERATE.md` — current state + how to run. No history, no changelog.
- `docs/LESSONS.md` — what the pipeline taught, and why the procedure has its shape.
- `README.md` — reference: what the artifacts are and what is in them.

A fact belongs in exactly one of these. The embedder rule was stated in two and one silently went stale,
which is how a false "Resolved" claim survived next to a correct table.
