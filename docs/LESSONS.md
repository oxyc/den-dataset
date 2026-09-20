# What this pipeline has taught, the hard way

Findings that cost real time to learn and are cheap to re-learn wrongly. Nothing here is operating
guidance — `docs/OPERATE.md` is the current state and the procedure. This is why the procedure is
shaped the way it is.

## Running the Haiku classification — and the two ways it silently fails

`enrich` writes batches; nothing in this repo can classify them. Labels come from a Claude Code run over
`DT-classification-prompt.md` (in the den repo), writing `out-t02/votes/batch-<id>-pass<n>.json`, which
`assemble` then aggregates. Both failure modes below were hit in one session, and neither is visible to any
check that was in place at the time.

### Failure 1: a batch can be structurally perfect and still worthless

A run of 999 titles produced, for 11 of 18 batches: valid JSON, exact record counts, real tmdbIds, every
label in-vocabulary, zero fabrications — and **46-70% of titles with no subgenre at all**, one batch with no
moods whatsoever. Density by batch ran 0.35-0.97 subgenres/title against a careful reference run's **1.77**.

Nothing caught it. Counts matched, checksums matched, the JSON parsed. It surfaced only because one title
(*Blake's 7*) appeared in both a validation slice and a production batch and came back
`Science Fiction / Sci-Fi Action / Dystopian` in one and `Drama / nothing` in the other. *Star Trek* had
likewise become plain `Drama`.

So **density is a gate, not a statistic**. Refuse any batch below ~1.2 subgenres/title or above 25% empty.
The fix that worked: smaller slices (20 titles, not 60) and a prompt section stating the expected density
outright — that a reference run averages 1.77 subgenres and 2.09 moods, that repeated empty arrays mean the
plots are being under-read, and that thin runs are rejected. The redo came back at **1.94 / 2.21 with 5%
empty**.

### Failure 2: fabricated ids are invisible to every count

A prior run had Haiku invent tmdbIds in 3 of 12 batches **with correct row counts**. A fabricated id attaches
one title's labels to another; no count, checksum or schema check can see it. So a batch containing even one
is refused **whole** rather than partially salvaged.

`scripts/check-votes.py out-t02` runs all of the below; `--batch N` for one. It reads the vocabulary from
the SHIPPED labels rather than a hardcoded copy, so it cannot drift from the taxonomy. Run it before
`assemble`.

### The checks worth keeping, in order

1. Count in == count out, same order.
2. No tmdbId absent from the input batch (fabrication) and none missing.
3. Every label in the vocabulary — and note the three lists are SEPARATE. Observed confusions: `Adventure`
   and `Mystery` (primary genres) used as subgenres, `Dark Comedy` (a subgenre) filed under moods. Strip
   them; an invalid label is unusable anyway.
4. **Density** — the gate above.
5. Spot-check titles you personally know. This is what caught Failure 1 and is not optional.

### One batch id per batch, and never reuse one

Vote passes are keyed by batch id, so writing a new batch over an existing id leaves the OLD votes on disk
pointing at the new titles. Assembling that pairs each title with a stranger's labels, silently. `enrich`
now takes the highest batch on disk as its floor and refuses to overwrite an existing batch file — see
`EnrichedBatches` — but if you build batches by hand, keep one media type per batch too: vote records carry
a bare `tmdbId`, and movie/TV ids overlap.

## The findings

**Close the vocabulary.** The single highest-leverage finding in the project. Asking an LLM for
open-vocabulary tags gives ~11% agreement between two runs of the same model on the same plot;
asking it to pick from a closed list gives 97.5-100%, with 6 off-vocabulary values in 5,400.
Voting cannot rescue an open vocabulary — a tag must be *named identically* twice to survive, so
2-of-3 voting DELETED content (15.22 tags/title down to 6.34). With a closed list the vote picks
a winner and never empties a slot.

**A closed vocabulary is also what makes errors catchable.** Six independent agents typed a tone
word (`bleak`) into the `ending` axis. Every one was caught, because `bleak` is not in `ending`'s
list and a validator could say so. An open vocabulary would have shipped all six silently.

**Say what to do, not what to avoid.** Listing forbidden words did not stop the `bleak` error.
"Ask how it RESOLVED, not how it FELT" did.

**A correct row count proves nothing.** Observed in this pipeline: fabricated TMDB ids with exact
counts, a duplicated key silently dropping another title, a key-shift where one title carried its
neighbour's tags, and one title dropped by two independent agents on two independent passes.
Verify keys element-by-element against the input, never by length.

**Batch size is a correctness parameter, not a tuning one.** A 100-id SPARQL batch that returns in
~1 s from one client hung to a 60 s timeout from another; 25 advanced steadily.

**Checkpoint what was paid for, and check what the resume actually skips.** A facts scrape froze at
16,500 titles through 22 restarts. It was not rate limiting: an entity-resolution pass ran over the
WHOLE accumulated checkpoint on every restart — 92,036 names, minutes of work redone — so a resumed
run never reached a new batch. Resumability is not just "write as you go"; it is "do not redo what
you already have".

**An artifact whose source text is not committed will be described wrongly.** The premise index was
called stale and TMDB-derived twice in one session, by someone reading file sizes, because its tags
and spec lived in an uncommitted working directory. They are in [`data/`](data/README.md) now.

**Measure before extrapolating from the first sample.** A rate read off the first minute of a run
projected 50 hours for work that took 3; a token estimate from one agent was 40% under. Both were
sampled during a cold start.
