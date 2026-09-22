# den-dataset — project instructions

## Use the best inputs we have. Always.

If a better version of an input exists, the artifact is built from it. Do not propose rebuilding from a
known-worse input to make a measurement cleaner — shipping a worse index to isolate a variable is the wrong
trade, every time. Measure the change some other way, or measure it after.

Concretely, as of 2026-09: `out-repass/` holds the **re-ground** Wikipedia plots (199 enriched batches) and
everything the live dataset `5b1c3213b6a1` was built from. Build from it, not from `out-t02`.

## `out-*` is gitignored — deleting it is permanent

`.gitignore:8` excludes `out-*/`. Git history will NOT bring it back. These directories hold the only copies
of enriched plot text, vectors, and LLM run outputs that cost real time and money to produce:

```
out-repass/              the re-ground plots, the paid classify and delta passes, the live generation
out-t02/                 the July generation and its plots
out-premise-v2/          the v2 premise run + its vectors
```

Never delete one to save space without asking, and never assume "it's in git". An input a derived artifact
needs belongs in `data/` (see `data/README.md`); a premise vector blob aligns to the `labels-premise.json`
built beside it, never to a tags file.

## Embed where you serve

Corpus vectors and live query vectors must come from the same `den-embed` **instance**, not the same
version string. `docs/OPERATE.md` "The alignment rule" is the procedure and the current state.

## Docs

- `README.md` — what the dataset is and what is published.
- `docs/OPERATE.md` — how to run it, and current state. No history, no changelog.
- `docs/LESSONS.md` — why the procedure has its shape.
- `AGENTS.md` — where in the code each question is answered.

A fact belongs in exactly one document, and what the code states, a document should not restate.
