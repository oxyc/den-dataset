Two shell rules, first:

- One command per Bash call. No `a && b`, `a; b` or `a | b`.
- Never point `grep -r`, `rg`, `find` or `ls` at a directory. Read files with the Read tool.

Do not spawn subagents.

# Your task

Classify one batch of film/TV works. `NNNN` below is the batch number you were given.

1. Read `/Users/cindy/Projects/Personal/den-dataset/out-repass/classify/SPEC.md` — the full instruction
   set. Follow it exactly.
2. Read `/Users/cindy/Projects/Personal/den-dataset/out-repass/classify/vocab.json` — the ONLY labels you
   may use.
3. Read `/Users/cindy/Projects/Personal/den-dataset/out-repass/classify/in/batch-NNNN.json` — your input.
4. Write `/Users/cindy/Projects/Personal/den-dataset/out-repass/classify/out/batch-NNNN.json` — a JSON
   array with EXACTLY one object per input work, in input order.

Count the input works before you start and count your output objects before you finish; the two numbers
must match. A batch that returns fewer rows than it was given is rejected outright and re-run — dropping a
work is the worst failure here. Echo `key` verbatim; never reconstruct it from `tmdbId`.

Report only: batch number, input count, output count. No per-title listing.
