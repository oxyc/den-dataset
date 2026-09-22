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
| Why was a publish refused? | `scripts/publish-dataset.sh` and the checks it runs under `scripts/`. `pipeline/publish.py` runs it and adds no guard of its own. |
| How do I run it? | `./den run --dataset-version <ver> --out-dir out` — which ENDS IN A PUBLISH. `./den stage <name>` runs one step. |

## The part that is still being rebuilt

`pipeline/` holds **five** stages today — the classify pass, the embed pass, the corpus join, the store
build and the publish — and everything else still runs from `docs/OPERATE.md` under `scripts/` and
`scripts/v2/`.
That is the migration in oxyc/den-dataset#27, not a second generation: stages join `STAGES` one at a
time, and the old tree is deleted in the commit that makes the new one authoritative.

Three rules keep it from becoming `scripts/v3/`, and all three are enforced rather than written down:

- **Existence means reachability.** A module under `pipeline/` that nothing in `STAGES` imports is
  deleted. `guards/reachable.py` fails CI on one.
- **Declare an artifact once.** `scripts/check-producers.py` reads its registry off the stage
  declarations. There is no second list, because the second list is what drifted.
- **The stage that writes an artifact owns it.** Its entry in `pipeline/artifacts.py` names no producer;
  the stage names the rule it runs, once, and runs that. An entry that still carries a producer is an
  input whose stage is not ported yet, and it empties when that stage lands.

Tests sit beside the code: `pipeline/store.py` and `pipeline/store_test.py`, no parallel tree.

Soft ceiling of ~400 lines per file. `scripts/v2/build_store.py` was 1,468 and is the reason the number
is written down; it is now the command line and the publication policy, over a `store/` package with one
module per section group in `wire/store-v1.md`. Nothing there is over 260 lines.
