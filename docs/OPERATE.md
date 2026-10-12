# Operating the dataset producer

The daily job builds the dataset: `.github/workflows/daily.yml` runs `./den daily`, which runs every stage
in `STAGES` order over what moved since the live dataset and stops at "ready to publish". `./den stages`
lists what each stage reads and writes; `pipeline/daily.py` says what a day runs, skips and refuses. This
file is what an operator checks after the workflow publishes a ready run, and how to get a run that is not
ready going again.

## Publishing a daily run

A ready scheduled or manually dispatched run publishes itself. After `./den daily` leaves
`checked.json`, the workflow calls `publish-dataset.sh --checked --keyless`: every gate remains in front of
the publish, Cosign binds the exact manifest to `daily.yml` on `refs/heads/main` with GitHub OIDC, the
Sigstore bundle is uploaded as `dataset.meta.json.bundle`, and the manifest is the final commit. The same
checked store, manifest and corpus bundle remain downloadable as `dataset-<run id>` for 14 days.

An owner-requested build uses the same path; only `main` in `oxyc/den-dataset` is allowed to run it:

```sh
gh workflow run daily.yml -R oxyc/den-dataset --ref main -f spend=false
```

A run that does not reach "Ready to publish" uploads its report and recovery inputs, publishes nothing, and
fails visibly. `--checked` refuses unless the store and manifest are the exact bytes the run's check passed,
compares them again with the release as it is now, and uploads the corpus bundle to `corpus-<version>` so
what a successful day bought reaches the next day. The legacy Ed25519 signer remains available for a manual
operator publish during the migration, but it is not part of the daily path.

Before enabling the daily schedule after adding a new durable store input, republish the live generation's
bundle once from the out-dir that built it. For fan picks this bundle must contain `fan-picks.json`; both the
bundle builder and `./den daily` now refuse if the live manifest says the store used fan picks but that input
is absent. `DEN_DAILY_FAN_PICKS_MAX_SPEND_USD` controls the scheduled Gemini ceiling (default `$1.00`); the
day refuses the complete new-title ask set before its first call when the projection would cross it.

## What the daily job spends

`DEN_DAILY_SPEND` is the master switch. The three independent repository variables
`DEN_DAILY_SPEND_TYPESAFE`, `DEN_DAILY_SPEND_FAN_PICKS`, and `DEN_DAILY_SPEND_PREMISE` can each be set to
`false` to stop that purchase without stopping the other two. Unset preserves the enabled behaviour under
the master switch. TypeSafe covers classify, critique, genres/moods, and franchise questions, every day; fan
picks and premise tags use the models `data/models.json` names (below), on the cadence it gives each.

A **weekly** step (both, on Mondays, through the Batch API at half price) submits one job for every title
still waiting for it, and a later run collects the job; a job that expired, failed or ran past 72 hours is
finished online and never resubmitted. Due is worked out from the last submit the paid-answers ledger records,
so a failed Monday is caught up the next day. A waiting title is published without that section, and its
article is fetched again each day it waits. The daily report's table shows each step's cadence, whether it
was due, the titles waiting, pending jobs with their age, what was collected and what expired. The fan-picks
job also carries the re-asks due that week: titles the model did not know, or released within six months of
their first ask, asked again three and six months on (`data/fan-picks-reask.json` seeds the titles asked
before the ledger); a re-ask replaces the picks only when the model now knows the title or names more store
titles. A weekly step needs `--paid-state`: without the ledger it could not find its job again, so it does
not submit one.

The weekly steps are held to the same caps (`pipeline/model_steps.py`). A submit takes as many titles as the
step's daily cap and the month have room for and leaves the rest waiting; with no room at all nothing is
submitted and the step stays due, so the day does not fail. A submitted job's projection counts against the
month (`pendingBatchUSD` in the report) until a run collects it and measures what it cost. Finishing a job
online is paid from that day's allowance and can take several runs; a title already answered is never asked
again. A run without spend still reads a finished job, and asks nothing. A job still owing titles after 14
days is let go, its answers kept and its other titles waiting again. Each submit is recorded before it is
sent, so a run that dies before it sees the job finds it by its label the next day instead of buying it twice.
A premise title no model would tag is asked again three and then six months on, not every week; one the
worklist turns away for good (not a screen work, not narrative, no story-premise section) is not asked again
until it is classified again; one whose article changed waits to be classified again and is never settled.

Each step projects the complete request set before its first call. The per-day ceilings are
`DEN_DAILY_TYPESAFE_MAX_SPEND_USD`, `DEN_DAILY_FAN_PICKS_MAX_SPEND_USD`, and
`DEN_DAILY_PREMISE_MAX_SPEND_USD` (each defaults to `$1.00`). The all-step monthly ceiling is
`DEN_DAILY_MAX_SPEND_USD_MONTH` (default `$10.00`). Every run that reaches its report, including one a
later gate refuses, publishes its measured
`daily-report-<run id>.json` on the `daily-reports` release; a run downloads and strictly parses those
public reports, sums `spend.totalUSD` for the UTC calendar month, and refuses a step whose projection would
cross either ceiling. An unreadable report is fatal rather than counted as zero.

The daily TypeSafe step is the exception to refusing: a day with more new titles to classify than its cap
or the month has room for (`Ledger.allowance`, at $0.00082 a title: classify, genres & moods and franchises together) classifies the first that fit, in the
store's row order (movies, then series, by id), and holds the rest (`changes.hold`, `heldForSpend` in the
report). A held title is out of the change set, so no later stage touches it, and the corpus join keeps it at
its published source (`held_source`) so a plot never ships without its vector. The corpus marks such a source
`heldForLabels`, so the next day's seed does not count the title seen, and a ready run that held titles has
not carried the week's slice, so the next day is a catalogue day that finds them again. A budget that fits not
even one title (a month with no room, a cap below one title's cost) still refuses the day.

The report records projection, cap, measured cost, provider token counts, today's total, and the
month-to-date total per step. Premise generation is batch-resumable, audits exact media-type-qualified keys,
applies the placeholder and storytelling-property guards, and repairs only rows that fall below the 8-tag
floor after invalid tags are removed. Its merged strings, vectors, labels, coverage, and provenance travel
in the corpus bundle together.

## Switching a model

Every paid text step asks through `lib/llm.py`, and `data/models.json` says who answers each one: provider
(`anthropic`, `openai`, `gemini`), model, `online` or `batch`, `cadence` (`daily`, or `weekly` with a `day`),
titles per call, thinking level and a fallback for titles the model refuses. Switching a step's model is a one-line PR to that file; the daily job reads it.
A model must have a price in `lib/llm.py` (`PRICES`) before it can be configured, because the spend caps
read that table. For a one-off run, `DEN_MODELS=<file>` points at another config. To compare models before
switching, `tools/bakeoff.py <step> --models provider:model … --out <dir> --max-spend-usd <cap>` runs one
sample through each and writes the table (`--help` lists each step's inputs).

The keys are `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and `GEMINI_API_KEY`, read from the environment. The
`claude-cli` and `codex-cli` providers run on the owner's monthly plans, for local backfills and bake-offs
only; they refuse to run in GitHub Actions.

Answers bought by hand go where the daily job reads them. `tools/fan_picks.py backfill --work <full-run dir>
--keys <keys.json> --max-spend-usd <cap> [--provider claude-cli --model claude-sonnet-5]` asks the fan-picks
fallback about titles the primary refused and writes `data/fan-picks-backfill.json`. Commit it; every daily
run matches those answers into the titles that still have no picks, so the next publish carries them.

The publish must run from the repo root: its ownership guard resolves producer paths and `git ls-files`
against the working directory. Overrides are environment variables (below), and reach the guards either way.

## When a run is not ready

**A failed day reports itself.** The daily workflow's last step (`pipeline/daily_issue.py`, workflow permission
`issues: write`) keeps one open issue labelled `daily-failing` for "the daily run is failing":

| The run ended | What the step does |
|---|---|
| Not ready (a stage or gate refused, a step failed, cancelled or timed out) | Opens the issue, or edits its count and comments on it: date, run link, the `daily: NOT ready — …` verdict line, consecutive failed runs. |
| Handed the day to another attempt (blocklisted or canary-failed CPU) | Nothing; the new run reports. |
| Handed on six times (the last attempt also on a bad CPU) | A failure like any other, and the run ends red, not cancelled: no run did the day's work. |
| Ready | Closes the issue with a comment linking the run. |

Only the verdict, the link, the count and a CPU model name are posted (the repo is public), with URL queries
and `key=` values scrubbed from the verdict; the logs stay in the run. A CPU that fails the canary without
being on `data/embed-cpu-blocklist.txt` also gets an `embed-cpu-blocklist` issue, "Add `<model>` to the embed
CPU blocklist", with the exact line to add; open the PR that adds it and close the issue with it. Nothing is
committed automatically. The step does not see a run that never started (the schedule off, the job's `if`),
a runner that died, or a cancelled run whose cleanup steps did not run.

Start with the run's summary, which is `out/daily-report.md`: what moved, which stage was skipped for want
of a credential, what was bought, and the refusal. `daily-report.json` carries the same, and
`changes/plan.json` is the change set; all three are in the `daily-report-<run id>` artifact.

Nothing a failed run paid for is lost: the job uploads the paid-answers ledger (`pipeline/paid.py`) to the
`paid-state` release after every run, and the next run lays its Jev shards back and reuses its fan-pick and
premise answers before asking anything. Rerunning a failed day buys nothing it already bought. The ledger is
`paid-state-<run id>-<attempt>.json.gz`, the newest of them; each run uploads its own before deleting the
older ones. A run that cannot read it fails rather than start without it, and a published run prunes what the
publish carries. The release is public, so the ledger carries no TMDB title or year and no article text.

The ledger also carries the below-floor verdicts (`judged`): which titles the weekly catalogue pass
(`--revisit-weeks`) already judged short of every admission floor, so the next pass asks only about what is
new, what changed and what has gone stale. The out-dir is seeded with none (`pipeline/published.py`), so
without them each weekly run judged all ~43,000 unpublished titles at the lowest vote floor again, and none
finished. A verdict is kept as the day it was made, the floors, and which of the two TMDB floors the count
met (not the count, which is TMDB's); it holds for 56 to 111 days (`enrich.CARRY_DAYS`, spread by title so one
pass's verdicts do not all expire in the same later one) and is asked again sooner when the floors change or
the count moves across a TMDB floor. A publish does not carry a verdict, so `prune` leaves them. The daily
delta trusts only today's verdicts. The catalogue pass drains each media for at most 90 minutes
(`fetch.CATALOGUE_SECONDS`) and saves its verdicts after every batch, so the first pass, or one after the
ledger was lost, makes progress over several weeks instead of running into the job's time limit and keeping
nothing; the stage log says how many titles it left.

`judged` holds a second kind of verdict, in the same map and on the same terms: **plotless**. A title that
clears the admission gate and has no Wikipedia plot never ships ("no Wikipedia plot, no title"), and before
this the weekly pass found the same ~6,800 of them again every week, because only shipped titles are known to
the worklist. The weekly pass now records the day and why (`noPlot`: `noArticle`, `noSection`, `belowFloor` or
`sourceIsScreenWork` or `sourceTaken`, a source work another title already holds; not `fetchFailed`, which is
asked again) and skips the title until the verdict expires, 56
to 111 days later, so one that has gained an article is eventually read. It states no vote count or floors,
which cannot change the answer. Each verdict is stamped with `enrich.PLOT_RULES` and counts as expired under
any other value (an unstamped one too), so it never outlives a change to the rules that produced it.
**Bump `PLOT_RULES` whenever the choice of Wikidata item for a TMDB id, the plot headings, the
other-language or source-work fallbacks, or the section-length floor (`WIKI_PLOT_FLOOR`) change**; the next
weekly pass then asks every parked title again. A title that gains a plot loses its verdict; the daily delta neither trusts nor
records one, because it asks about releases still gaining articles.

The report separates the titles the change set calls `added`: `counts.addedPlotted` can still be classified,
`counts.addedPlotless` (listed in `addedPlotless`, the summary's "Added without a plot (parked)") are admitted
and parked. `added` is still every new title, which the later stages read.

A publish made by hand — anything but the workflow's own publish step — leaves the ledger unpruned, so its
old shards would be laid back beside the published rows. Prune it the same way afterwards, uploading it under
the newest asset's name with its attempt number raised, so it sorts newest, before deleting the old one:

```sh
gh release download paid-state -p 'paid-state-*.json.gz' -D paid --clobber   # e.g. paid-state-123-1.json.gz
python3 pipeline/paid.py prune paid/paid-state-123-1.json.gz
mv paid/paid-state-123-1.json.gz paid/paid-state-123-2.json.gz
gh release upload paid-state paid/paid-state-123-2.json.gz
gh release delete-asset paid-state paid-state-123-1.json.gz --yes
```

To reproduce a run locally, lay out what the job started from and run the same command:

```sh
gh release download data-latest -p dataset.meta.json -D out/published --clobber
gh release download corpus-<datasetVersion> -D out/published --clobber
gh release download paid-state -p 'paid-state-*.json.gz' -D paid --clobber   # one asset: the ledger
cp paid/paid-state-*.json.gz paid/paid-state.json.gz
./den daily --out-dir out --paid-state paid/paid-state.json.gz
./den stage publish --out-dir out --plan     # every gate again, against the same published manifest
```

A local run given the ledger writes back to that copy, never to the release.

`./den daily` reads `TMDB_API_KEY` (and `TYPESAFE_API_KEY` with `--spend`) from the environment or
`den.env`; `DEN_EMBED_URL` must name a den-embed whose canary answers match (next section).

| The run stopped at | What it means, and what to do |
|---|---|
| "The live dataset" step | No `corpus-<version>` release: the live dataset was published without its bundle, and a day never starts from nothing. Build the bundle from the out-dir the live dataset was built from — its corpus records each title's source — and publish it as a daily run: `./den daily --out-dir <that dir>`, then `pipeline/publish-dataset.sh <that dir> --checked`. |
| `worklist` skipped | No `TMDB_API_KEY` secret: no new titles were discovered. |
| `fetch` | See "Wikimedia Enterprise" and "Re-running one fetch batch" below. |
| `embed`: canary | The den-embed the job ran is not in the published space. See "The alignment rule". |
| `embed`: identity or composition | `index/embedder.json` or `index/composition.json` disagrees with the service or the document shape, or is missing from a store with rows. See "Recovering a store's composition". |
| `plot_length`: `pre-transform-baseline` | The live generation predates `index/plot-length-transform-v1.json`. Crossed at `791b40086762`; it recurs only if a generation is published without the transform in its bundle. A full rebuild must create and bundle the transform and `vectors-bge-m3.raw.bin`. Not retryable. |
| `facts` | See "Wikidata" below. |
| `franchises` | The derived franchises did not clear `data/franchise-golden.json`'s floors. `docs/FRANCHISES.md`. |
| `changes`: post-baseline plot or article | A manual source refresh wrote new prose beside old model artifacts. See "Operator-only source work". |
| `publish` | A publish gate refused. The table below. |

### Publish gates

Each gate is in `pipeline/publish-dataset.sh` with the incident that put it there.

| Refusal | What to do |
|---|---|
| record count / coverage | A blob would lose records or fall behind the corpus. Find the stage that lost them; `DEN_ALLOW_DROPPING_BLOBS=1` only for a deliberate shrink — it also switches off the "could not read the published manifest" guard. |
| `datasetVersion … is already published with a DIFFERENT store` | The corpus is unchanged and the store is not. If the writer changed deliberately: `DEN_STORE_REBUILD='what changed'`, recorded in the manifest as `storeRebuild`. If the corpus changed, it wants a new `datasetVersion`. |
| `store would lose sections` | Restore the missing store input. For a deliberate format retirement only, set `DEN_ALLOW_DROPPING_STORE_SECTIONS='why these sections are retired'`; the reason is printed and recorded as `droppedStoreSections`. |
| ownership (no producer, or a changed store input) | An artifact nothing committed here builds, or a store built from an input that has since moved. Rebuild; `DEN_ALLOW_UNOWNED_ARTIFACTS=1` / `DEN_ALLOW_STALE_STORE_INPUTS=1` when deliberate. |
| dead generation | A declared file carries another generation's version in its name. Rebuild it or drop the key. |
| manifest contradicts its files | A count or byte size in the manifest disagrees with the file. Re-stamp from the files. |
| shared-article grounding | Titles grounded on one article regressed. `DEN_ALLOW_SHARED_PLOTS='why'` when deliberate. |
| alias gate | An alias is another title's name. Decide it in `data/alias-decisions.json`. |
| Wikidata item gate | Several items claim one TMDB id and nothing chooses. Decide it in `data/wikidata-item-decisions.json` (below). |
| award merge gate | A ceremony reaches the corpus twice. Merge it in `data/award-ceremony-merges.json`. |
| quality gate | The genres & moods score fell more than the tolerance under `data/eval/quality-floors.json`. The baseline moves only when recorded: `pipeline/eval_taxonomy.py out/labels-t02.json --record` after an improvement ships, with `--accept-drop` for a deliberate drop. Commit the result. |

## The alignment rule (do not break this)

**The corpus and every live query must be embedded in the same space.** int8 dot products are meaningless
between vectors from different ones, and nothing about the result looks wrong when they are:
oxyc/den-dataset#21 was a corpus half-embedded by two den-embed builds, measured at 6.3/10 top-10 overlap,
found by hand months later.

**1. The same image, on an AVX2 host** — the digest the box serves (`DEN_EMBED_IMAGE`), at
`MAX_TOKENS=1024`. ONNX Runtime picks its kernels from the CPU's flags, so the host's instruction set is part
of the space. The box's Intel CPU and a hosted runner's AMD EPYC 7763 (both AVX2 only) return
byte-identical vectors (24/24, #27). An AMD EPYC 9V74 or 9V45 runner (AVX-512, VNNI) moves every vector,
cosine 0.979 — measured 2026-10-11 after four dailies the canary refused. Hosted runners come in several
kinds and take no CPU request, so the daily job hands the day to a new run (up to six tries, then the day is
skipped) on a CPU listed in `data/embed-cpu-blocklist.txt`, and on any other CPU runs the canary before
anything is bought. A CPU that fails it names itself in the run's notice and in an `embed-cpu-blocklist` issue; add it to the blocklist. Then it
embeds in its own container. The published image cannot run on an
Apple Silicon Mac at all: ONNX Runtime needs AVX2, which emulation lacks, so `/health` answers and the first
embed dies with an illegal instruction ("connection refused" from the client). Whatever the host, the canary
(2.) is what decides.

**2. The canary, not a version string, says which space a service is in.** `embeddingModel`, `dims`,
`vectorEpoch` and `embedderRuntime` have all stayed the same across builds whose output moved.
`data/embed-canary.json` holds fixed texts and the exact int8 vectors the service must return; every path
that writes a vector checks them before opening its output, byte-identical or refuse:

```sh
pipeline/embed_canary.py --url http://den-embed:8080        # exit 0, or exit 2 and a report per case
```

The verified identity is stamped into `dataset.meta.json` as `embeddingSpace`. `--regenerate` is only for
when the space is MEANT to move (model change, `VECTOR_EPOCH` bump, settings changed with a full re-embed) —
never to get a failing run going, because the space id is published.

**3. `MAX_TOKENS=1024`.** den-embed defaults to 512 and truncates silently, server-side. At 512 the corpus
loses half its plot prose. This setting is what has actually caused a space difference, twice, and both
times it was first diagnosed as something else (an instruction set, then a thread pool). A comparison
against a container at the image's default cap is comparing caps.

**den-embed is held on the box** (`/etc/den/hold/den-embed`), so the daily `den-update` never moves it. Moving
it is a corpus decision: re-embed, then `den-update den-embed`, then point `DEN_EMBED_IMAGE` at the new digest.

**Embedding on the box instead**, when the job's container cannot: write the documents here, embed there,
bring the vectors back.

```sh
./den stage embed --out-dir out --dump-docs out/docs.jsonl
#    Copy docs.jsonl, pipeline/embed_docs.py, pipeline/embed_canary.py and data/embed-canary.json into
#    the container's /tmp (`ssh root@pve 'incus exec den -- tee /tmp/<name>' < <file>`), then:
ssh root@pve 'incus exec den -- podman run --rm --network den \
    -v /tmp/embed_docs.py:/embed_docs.py:ro -v /tmp/embed_canary.py:/embed_canary.py:ro \
    -v /tmp/embed-canary.json:/canary.json:ro -v /tmp/box:/w:z \
    docker.io/library/python:3.12-slim python /embed_docs.py \
        --docs /w/docs.jsonl --out-dir /w/out --url http://den-embed:8080 --canary /canary.json'
python3 pipeline/import_box_vectors.py --vectors box/vectors.jsonl --labels out/genres-moods.json \
    --out-dir out/index --embed-space box/embedding-space.json \
    --embedder-health '{"model":"bge-m3","dims":1024,"vector_epoch":1,"runtime":"…","max_tokens":1024}'
```

A `&` or redirect inside `ssh root@pve 'incus exec den -- …'` binds to the **host** shell, not the
container. Put backgrounding in a script inside the container; a second accidental launch halves throughput,
since den-embed serialises on one model lock.

**What the embed stage refuses.** It composes from `genres-moods.json`, so a title with no genres & moods is
skipped as `missingLabel`. A title whose genres & moods changed keeps its old vector until
`--reembed-changed` re-embeds it; `finalize` ships the new genres & moods either way, and refuses a vector
whose title has none. The first run in an out-dir records the service's identity and the document shape
(`index/embedder.json`, `index/composition.json`); later runs refuse a service or shape that differs, and a
store with rows but no identity record refuses too. The plot cap is 3,500 characters and keeps the head
only. No cap above ~4,000 characters can reach the embedder at 1024 tokens, so raise the cap and
`MAX_TOKENS` together or not at all.

## Recovering a store's composition

A store with rows and no `index/composition.json` refuses a top-up, because how its documents were composed
cannot be known — and guessing writes the guess down as a fact. Recover it instead; it takes minutes. The
shipped cc0b store's values are `{"docShape":"lean","dropDirector":true,"plotCap":3500}` and apply to that
store only.

Compose probe documents with `pipeline/compose.py` (`capped_plot(plot, cap)`; `lean(...)` with a
`Directed by …` clause prepended for the director variant) and embed them with `lib/denembed.embed_many`:

1. **Pick probes**: ~10 titles whose plot is far longer than any candidate cap and that carry no Wikidata
   director, plus 2 short-plot titles that do carry one (the cap cannot touch those, so they isolate the
   director flag).
2. **Settle the shape first** on the short-plot titles, with and without the director clause. Exactly one
   matches.
3. **Sweep the cap** on the long titles with the shape fixed. At a 1024-token service the answer is an
   integer in (0, 3596].
4. **Compare exact bytes** against the shipped blob, looking rows up BY KEY (`(media << 32) | tmdbId`), not
   by position. Adjacent caps sit at cosine 0.95–0.98, which reads as "about right" and is wrong in every
   byte.

`capped_plot` snaps to the last `". "`, so each probe pins an interval; intersect them.

## Wikidata

**Throttling.** The facts stage's per-batch queries fall back to QLever's Wikidata endpoint
(`https://qlever.dev/api/wikidata`) when WDQS answers 429, 5xx or times out; WDQS is then asked once rather
than retried, because its `Retry-After` is two minutes. `DEN_SPARQL_PREFER=qlever` asks QLever first —
use it while WDQS is throttling, when a pass would otherwise take a day. QLever indexes the weekly dump, so
an edit from the last few days may be missing. Entity names, person traits, IMDb ids, franchises, awards and
source kinds stay on WDQS. The pass's closing JSON (`answeredBy`) counts which endpoint answered. A batch
Wikidata fails is dropped whole and the merge refuses a pass that skipped one; `pipeline/facts-run.sh out`
loops until nothing is skipped.

The enrich stage's lookups (`lib/wikidata.py`: the mapping, languages, genre/type labels, origin countries
and the claimant lookups) use the same fallback. QLever has no `SERVICE wikibase:label`, so it is sent the
labels as `rdfs:label` patterns (`en`, then `mul`, bound to the same variable); the answer is kept under the
WDQS query text, so the cache keys are unchanged. Three lookups stay on WDQS because QLever's 30 s limit
(answered as a 429 `Operation timed out`) was measured to cut them off at every batch size: the source-work
class walk, the target labels, and the Wikipedia count. So does the doc-facts scrape, whose names QLever
orders differently. The ids of a batch go out in chunks of 150 (`TMDB_CHUNK`), whatever `fetch` hands
`enrich`: the language query for the 200 most-linked films runs past WDQS's 60 s every time and takes under a
second at 175. A batch that times out on every endpoint it is asked of is asked again as two halves,
down to ten ids, and the answers are merged (the log says `wikidata: N ids failed … asking A and B`); a
refusal of the query (a 4xx other than QLever's timeout) is not split, and neither is a 429 throttle.

**How many titles a facts request carries.** The scrape asks each property (31 of them, plus the titles hop's
two requests) once per batch of 150 titles (`facts.SCRAPE_BATCH`, = `TMDB_CHUNK`), so a stage's time is its
batch count times ~30 requests, not its title count. WDQS answers a request in 0.2-1.3 s whether it carries 25
ids or 150; the weekly revisit slice (6,692 titles) took 3 h 10 min at 25 ids. 150 is as far as the language
query goes (it times out at 200); a request that still times out is asked again as halves. Each answer is
cached under its own query text, so changing the batch size re-asks cached bodies once; the revisit slice
reads no cache anyway.

**The delta pass.** The facts are two passes merged: the titles in `labels-t02.json` (stamped `hasVector`)
and `out/facts-delta-ids.txt` (not stamped, so den-atlas's `/recommend` never lets a vectorless record into
an ANN path). `./den daily` writes the list — every title the live facts carry that the new labels do not,
plus the ids it already names — and the stage refuses without it: a merge missing the delta pass once
dropped 137 titles and only `/recommend` noticed. An id added by hand (`movie:1` / `tv:2`, one per line)
stays until it has a vector.

**Ambiguous items.** About one title in 560 has its TMDB id claimed by two Wikidata items (series 2559:
"Boon" and "Bonn"). Every stage that asks Wikidata by TMDB id answers from ONE of them, chosen by
`lib/wikidata.resolve`: first `data/wikidata-item-decisions.json`, then the item stating no other TMDB id,
then the item with an English article. A title nothing chooses for ships with no Wikidata fields; the facts
stage counts it (`ambiguousItems`), and the publish refuses until it is decided in that file. A decision for
an id that is no longer contested is refused as stale.

**The names table.** `names.json` holds Wikidata's name, original title and year for every film and series
with a TMDB id. The store keeps the ones it has no row for, as `wd_name_*`. The daily job rebuilds the table
from QLever just before the store stage, but only when the day builds a new generation; a day that rebuilds
the live generation keeps the bundled copy, so its store stays byte-identical. A failed fetch keeps the
carried copy and is named in the report. Contested ids follow the rule above, except that an id nothing
chooses for is left out instead of refusing the publish. Build it by hand with
`python3 pipeline/names.py --out <out-dir>/names.json`.

## Wikimedia Enterprise

With `WIKIMEDIA_ENTERPRISE_USERNAME` / `_PASSWORD`, the fetch stage mints a 24h bearer and `lib/plot.py`
reads the pre-sectioned plot from Enterprise, falling back to the public `action=parse` API on any miss.
Enterprise reads are not cached and name no page, so they record no revision id and cannot see a redirect.

The account allows 50,000 on-demand requests a month, shared by every machine holding the credentials, and
an overdrawn month answers 429 like a throttle. `lib/enterprise.py` checks the account's own count before
the first request and every 100 after, and stops at `limit - DEN_ENTERPRISE_RESERVE` (default 500). Batch
reports count `plotsFromEnterprise` / `plotsFromActionApi`; that, not the bearer being held, says which
source a run used. Never use a Wikimedia dump: stale, and hundreds of GB against a working set of a few
hundred MB.

**Re-running one fetch batch** by hand, credentials already in the environment:

```sh
python3 -m pipeline.enrich --worklist out/worklist-movie.json --out-dir out --limit 150
```

A title is admitted when its TMDB vote count clears its TMDB floor **or** the number of Wikipedias with an
article on it clears its Wikipedia floor, per tier (`pipeline/floors.py`). A batch report's
`admittedByTmdb` and `admittedByWikipedias` say which. A failed Wikipedia-count lookup aborts the batch like
a failed mapping.

## Operator-only source work

Neither the daily nor the weekly run surveys existing Wikipedia prose: new titles are automatic, and the
weekly option revisits cheap facts and doc-facts only. So Wikipedia rewording never authorizes premise tags,
Jev or embedding work. What follows is by hand, and never part of the job.

### The source refresh

`./den stage fetch --refresh --out-dir out` drains the worklists as usual, then asks Wikipedia for the current
revision of every grounded title's article, 50 a request, and re-fetches only the titles whose revision moved,
whose page is gone, or whose revision is unknown. `--plan` does the asking and reports the counts; it drains
nothing and fetches nothing. Re-fetched records land in new batches for inspection, but `den stage changes`
refuses any post-baseline plot bytes or article identity change before writing worklists or tombstones:
publishing new prose beside old model artifacts is incoherent. den-dataset#145 tracks the deliberate
correction transaction that will replace this manual gap. A title whose article was edited outside its plot
is in neither list.

### A reviewed source correction

`pipeline/source_corrections.py` is the deliberately separate transaction for a correction. It calls no
Wikipedia, model, embedding, or publishing service. In particular, preparing a transaction is not permission to
apply it, applying it is not permission to spend, and a transaction with missing receipts cannot publish.

Prepare it from four inspected JSON files: the live baseline enriched row, the candidate enriched row, the exact
candidate article input (the whole row a classify pass will read), and a non-empty review entry:

```sh
python3 pipeline/source_corrections.py prepare --root out/corrections \
  --baseline baseline.json --candidate candidate.json \
  --article-input candidate-article.json --review review.json
```

The resulting `awaiting` directory freezes all four files. Its evidence names the key, revision,
article/language identity, plot digest, article-input digest, candidate digest, baseline digest, and review-entry
digest. An approval is a separately controlled JSON object with `decision: approve`, that exact `key`, `evidence`,
and `reviewEntrySha256`; extra audit fields such as reviewer and time may be present. Application requires the
source to be observed again and given as `--observed` plus `--article-input`. If either differs, application writes
nothing to `enriched/`, leaves the reviewed transaction awaiting, and queues the new bytes as another `awaiting`
transaction.

```sh
python3 pipeline/source_corrections.py apply out/corrections/movie-7-… \
  --enriched-dir out/enriched --approval approval.json \
  --observed candidate-now.json --article-input candidate-article-now.json
```

Application writes one reserved numeric batch and moves to `appliedPending`. A retry accepts that batch only when
its bytes are exact, covering a kill after the batch rename but before the state rename. It never runs the derived
passes. Each later pass must emit a receipt for `attach-proof` that binds the transaction's key and
`sourceDigestSha256`; classify, critique, genres/moods, and premise also bind `articleInputSha256`. The composed
document receipt binds the digests of all four receipts and its exact text, and the vector receipt binds that
document digest. A lost-plot correction instead requires both a withdrawal receipt and a watcher receipt retaining
the old article/language, so a regain can become a new review candidate.

`python3 pipeline/source_corrections.py gate TRANSACTION` is read-only and refuses until the exact required set is
present. Receipts alone cannot satisfy the real publisher: `check_source_corrections.py` discovers every durable
`appliedPending` transaction and resolves each receipt to a restricted native output name under the out-dir,
checks that file's hash, then checks its title row against the frozen article revision/hash (and the aligned
document/vector hashes). The publisher runs that check before its other mutations, stamps the complete native
artifact map into `dataset.meta.json`, and refuses on any missing or old row. After the manifest upload—the
publication commit point—it records the publisher's dataset version, a `maxBatchId` that includes the reserved
batch, and that exact gate result; only then does state become `published`.
The ordinary change-set refusal remains in force for all unexplained post-baseline source batches; this tool does
not make an old manual refresh canonical merely because its files exist.

### Premise tags for a daily increment

Premise generation is never triggered by a Wikipedia revision or rewording, and preparing or publishing a
daily run does not authorize buying tags. Prepare it only from a change plan with a published baseline; the builder
admits `added` and legitimately `regained` keys and rejects a first-generation plan.

```sh
pipeline/build_premise_worklist.py \
  --combined out/combined-v1-r2.jsonl --articles out/articles.jsonl \
  --changes out/changes/plan.json --token-ceiling 50000 --out-dir out/premise-increment
```

This makes no model call and records `generationAuthorized: false`. When a generation is deliberately run,
validate every response batch with `pipeline/validate_premise_batch.py` before `merge_premise_tags.py` can
append anything.

### Correcting shipped premise tags

Re-tag the rows and merge them into `data/premise-tags-v2.json` with `merge_premise_tags.py --overwrite`.
The next daily run with `DEN_EMBED_URL` carries every committed row that differs from the published copy
into its out-dir and re-embeds exactly those vectors; the report's `premiseCorrections` counts them. Without
an embedder it carries none of them and says so, so strings and vectors never part.

### KAVI maturity backfill

KAVI permits its IKLU API to be used by external services, but broad responses stop at 100 rows and expose no
working pagination. Build the optional movie snapshot in resumable one-day windows; the collector refuses a day
that reaches the cap rather than treating a possibly truncated answer as complete:

```sh
python3 tools/kavi_maturity.py --out out/maturity-sources/kavi-movies.jsonl \
  --begin 2012-01-01 --end 2026-10-12 --pause-ms 250
```

The adjacent `.state.json` records completed dates. Re-running the same command asks only unfinished dates and
deduplicates IKLU ids already appended. `den stage maturity --out-dir out` consumes the snapshot when present;
without it KAVI remains unknown and the other public authorities still run.

### A first generation

The daily job refuses `--spend` without a live baseline: a first generation is the whole corpus (~$20 of
classification for 47,529 titles), bought by hand with `./den run --out-dir out --spend`, never by a timer.
Each paid stage takes `--plan` first (`./den stage classify --out-dir out --plan`); `docs/FACETS-V2.md` and
`docs/FRANCHISES.md` say what they buy.
