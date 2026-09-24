# A map, not a tour

Where each question is answered. If an answer is not here, it is in the file this points at — not in
prose somewhere that can go stale without anything failing.

| Question | Where |
|---|---|
| What runs, and in what order? | `pipeline/__init__.py` — `STAGES`. Or `./den stages`. |
| What does a stage read and write? | That stage's `INPUTS` / `OUTPUTS`, at the top of its module. |
| What builds an artifact? | The stage that declares it in `OUTPUTS`. For one nothing here builds yet, `pipeline/artifacts.py`. Or the producer column of `./den stages`. |
| What does a store section mean? | den-spec `wire/store-v1.md`, then the `store/` module named for its heading. |
| What order are the store's sections written in? | `store/build.py`, and `PROVENANCE` declares the same order. |
| Why was a publish refused? | `pipeline/publish-dataset.sh` and the `pipeline/check_*.py` guards it runs beside it. `pipeline/publish.py` runs it and adds no guard of its own; `./den stage publish --plan` runs every gate and signs and uploads nothing. |
| What does a daily run redo? | `pipeline/changes.py` — the change set since the live dataset, which the stages after it read. |
| Which titles are one franchise, and why? | `pipeline/franchises.py` (the stage), `pipeline/franchise_groups.py` (what Wikidata decides alone), `data/franchise-golden.json` (the gate). |
| Where does a daily run start, with nothing kept? | `pipeline/published.py` — the bundle a publish puts on `corpus-<ver>`, and the out-dir seeded from it. |
| How do I run it? | `docs/OPERATE.md`. `./den run` runs every stage; it skips the paid classify and critique passes without `--spend` and stops before publishing without `--publish`. `./den stage <name>` runs one. `./den daily` is the scheduled job (`pipeline/daily.py`). |

## The part that is still being rebuilt

`pipeline/` holds **fourteen** stages — `./den stages` lists them. What is left outside the order is the side
pass no stage runs but the store reads — the premise tags — and it still answers for itself in
`pipeline/artifacts.py` until it lands. Beside the
stages sit the tools an operator runs by hand, each declared in `guards/operator-tools.json`; a tool with
dependencies the pipeline does not take lives under `tools/` instead.

`lib/` is what a stage needs from OUTSIDE the machine — HTTP with one retry policy, the response cache,
and the upstream clients. It is held to the same reachability rule, entered from the stages that import
it. Its cache key is a contract with ~2.1 GB of bodies already on disk: see `lib/cache.py`.

Three rules keep it from becoming `scripts/v3/`, and all three are enforced rather than written down:

- **Existence means reachability.** A module under `pipeline/`, `store/` or `lib/`, a tool under `tools/`
  or a shell script that nothing reaches is deleted, and a Python or shell file anywhere else is refused.
  `guards/reachable.py` fails CI on either.
- **Declare an artifact once.** `pipeline/check_producers.py` reads its registry off the stage
  declarations. There is no second list, because the second list is what drifted.
- **The stage that writes an artifact owns it.** Its entry in `pipeline/artifacts.py` names no producer;
  the stage names the rule it runs, once, and runs that. An entry that still carries a producer is an
  input whose stage is not ported yet, and it empties when that stage lands.

Tests sit beside the code: `pipeline/store.py` and `pipeline/store_test.py`, no parallel tree.

Soft ceiling of ~400 lines per file. `pipeline/build_store.py` was 1,468 and is the reason the number
is written down; it is now the command line and the publication policy, over a `store/` package with one
module per section group in `wire/store-v1.md`. Nothing there is over 260 lines.

## Enriching genres & moods by hand

An optional pass in which labelling agents write genres & moods into `data/genres-moods-curated.json`,
overriding the automated labeller there (oxyc/den-dataset#56). Nothing in this repo pays for it; the
agents do the labelling. Use the model the user names; Sonnet is the recommended one.

The automated labeller is the `genres_moods` stage: it asks Jev about the titles this file has no genres
& moods for and derives `genres-moods.json` from the answers. A title this pass writes here is never asked
again, so enriching one by hand is what overrides it.

1. `./den genres-moods prepare --out-dir <out-dir>`. It picks titles with no genres & moods (`--missing`,
   the default; or `--keys FILE`, or `--since YYYY-MM-DD`), writes batches of 25 into
   `<out-dir>/genres-moods-enrich/`, and prints the batch count and the instruction for one batch.
2. Give each batch to one agent, with that instruction and its NNN filled in. The instruction points the
   agent at the kit's `SPEC.md` and its `validate.py`.
   - Claude Code: one subagent per batch on the model the user named (`opus`, `sonnet` or `haiku`), several
     in parallel.
   - Codex: work through the batches one at a time with the same instruction.
   - Allow web search only if the user says so, and then give the agents the web version of the
     instruction, which also asks for each batch's `.sources.json`.
3. Give a batch to a new agent if its validator does not print `ok`.
4. `./den genres-moods merge --model <model> [--web] --work <out-dir>/genres-moods-enrich`. It refuses if a
   batch is missing or invalid, or if the result scores more than the tolerance under the baseline in
   `data/eval/quality-floors.json`. On a pass it raises the baseline, and only raises it: a pass that is
   still a little under leaves the baseline where it was.
5. Read the summary it prints: titles added and changed, how many titles each label gained or lost, and ten
   before/after examples. If it looks right, commit `data/genres-moods-curated.json` and
   `data/eval/quality-floors.json` together. For a deliberate drop, rerun step 4 with `--accept-drop`, then
   run `pipeline/eval_taxonomy.py data/genres-moods-curated.json --record --accept-drop` and commit both
   files.
