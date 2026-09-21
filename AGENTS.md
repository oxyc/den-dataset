# A map, not a tour

Where each question is answered. If an answer is not here, it is in the file this points at — not in
prose somewhere that can go stale without anything failing.

| Question | Where |
|---|---|
| What runs, and in what order? | `pipeline/__init__.py` — `STAGES`. Or `./den stages`. |
| What does a stage read and write? | That stage's `INPUTS` / `OUTPUTS`, at the top of its module. |
| What builds an artifact? | `pipeline/artifacts.py` — one entry per file, naming its producer. |
| What does a store section mean? | den-spec `wire/store-v1.md`, then `scripts/v2/build_store.py`. |
| Why was a publish refused? | `scripts/publish-dataset.sh` and the checks it runs under `scripts/`. |
| How do I run it? | `./den run --dataset-version <ver> --out-dir out` |

## The part that is still being rebuilt

`pipeline/` holds **one** stage today — the store build — and everything else still runs from
`docs/OPERATE.md` under `scripts/` and `scripts/v2/`. That is the migration in oxyc/den-dataset#27, not
a second generation: stages join `STAGES` one at a time, and the old tree is deleted in the commit that
makes the new one authoritative.

Two rules keep it from becoming `scripts/v3/`, and both are enforced rather than written down:

- **Existence means reachability.** A module under `pipeline/` that nothing in `STAGES` imports is
  deleted. `guards/reachable.py` fails CI on one.
- **Declare an artifact once.** `scripts/check-producers.py` reads its registry off the stage
  declarations. There is no second list, because the second list is what drifted.

Tests sit beside the code: `pipeline/store.py` and `pipeline/store_test.py`, no parallel tree.

Soft ceiling of ~400 lines per file. `scripts/v2/build_store.py` is 1,468 and is the reason the number
is written down.
