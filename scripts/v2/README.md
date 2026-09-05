# v2 discovery-index work — the pipeline and the rules it enforces

Everything here builds **new** artifacts. Nothing in this directory writes to the plot
index, the t02 labels, or the v1 premise index; v2 ships as separate blobs and separate
manifest keys, and is dropped by deleting them.

## Where this stopped, and how to resume

The session ran out of model budget. What exists, what does not, and the exact next command:

| stage | state |
|---|---|
| corpus (38,460 wiki-plot titles) | **done** — `v2/corpus/` |
| co-rating ruler (6,000 nPMI cases) | **done** — `v2/eval/reco-cases.json` |
| v1 baseline on it, cross-checked vs DenKit `RecoEval` | **done** — `v2/eval/reco-baseline-dev*.json` |
| premise-ruler generation | **68 of 150 in-scope batches** (`scope.json` says why it stopped) |
| premise triplets, frozen provisional | **done** — `v2/ruler/triplets-provisional.json`, 527+ triplets |
| v1 re-embedded through the live runtime | **done** — `v2/vectors/vectors-premise-v1-realigned.bin` |
| cutoff sweep top-5 / top-8 | top-5 **done**, top-8 running/queued |
| blind judging, pass 1 | **complete** — 17/17 batches, 645/645 cases, 0 problems |
| blind judging, pass 2 | **complete or near** — see `--list-missing` |
| blind judging, pass 3 | **NOT RUN** — two passes give unanimity, which is stricter than 2-of-3 but keeps fewer cases; a third would recover the middle ground |
| confirmed triplet sets | `triplets-provisional.json`, `triplets-1pass.json`, `triplets-final.json` |
| bake-off, Phase 2 tagging, v1-vs-v2 gate | **NOT RUN** — priced at 234–400 M tokens for the full corpus |

**Resume, in order:**

```sh
V=out-t02/v2
P=out-t02/v2/.venv/bin/python

# 1. anything the generation phase still owes (empty if complete)
$P scripts/v2/llm_phase.py --phase $V/ruler/gen --list-missing

# 2. build the three blind judging passes from whatever generation produced
$P scripts/v2/build_judge_batches.py            # 40 cases/batch, a/b shuffled per case

# 3. run pass1..pass3 as subagents against scripts/v2/prompts/judge-triplets.md,
#    resuming each with --list-missing until clean
$P scripts/v2/llm_phase.py --phase $V/ruler/judge/pass1 --verify

# 4. un-blind, keep 2-of-3, report the disagreement rate
$P scripts/v2/consolidate_triplets.py

# 5. rescore every arm on the CONFIRMED ruler — this is what upgrades the
#    provisional findings in the den artifact to verified
$P scripts/v2/score_triplets.py --half dev
```

Everything is idempotent: each batch writes only its own fixed path, coverage is checked
against a manifest id-set, and `--list-missing` is the resume list.

**Two things the judging run established that are worth knowing before you resume.**

*The disagreement rate grew with every sample* — 17.5% at n=40, 20.0% at n=120, **25.2% at
n=445**. Two blind judges disagree about whether a triplet is usable for a quarter of cases.
Treat 25% as a floor, not a settled value: each earlier reading looked stable and each was
superseded upward. It is why unanimity is expensive and why the unanimous sets are small.

*The premise advantage over the plot index grows as the bar rises* — +9.0 pp on the
proposer's own labels, +8.9 after one blind pass, **+11.4 pp under two-judge unanimity, χ² =
4.00, p < 0.05**, and **+11.3 pp on the sealed TEST half** read once. That is the signature of
confirmation removing noise rather than signal, and it is the result the whole ruler exists to
produce.

**The TEST half has now been spent once**, on the premise-vs-plot comparison. Nothing was
tuned from it, so it remains valid for a v2-vs-v1 gate — but the count is one.

## Order of operations

| step | script | output |
|---|---|---|
| corpus | `build_wikiplot_corpus.py` | `v2/corpus/wikiplot-corpus.jsonl` + `corpus-ids.json` |
| ruler A — co-rating | `movielens_join.py` → `movielens_pairs.py` | `v2/eval/reco-cases.json`, `reco-popularity.json` |
| ruler B — premise | `premise_mine_candidates.py` → gen subagents → `build_judge_batches.py` → judge subagents → `score_triplets.py` | `v2/ruler/…` |
| split | `split.py` | a pure function, not a file |
| baseline | `score_reco.py` | `v2/eval/reco-baseline-dev.json` |
| tags v2 | `build_tag_batches.py` → tag subagents → `aggregate_tags.py` | `v2/tags-v2/…` |
| bookkeeping | `llm_phase.py` | coverage + strict parse for any LLM phase |

## The rules these scripts enforce in code

**Only wiki-plot titles reach an LLM.** `build_wikiplot_corpus.py` filters on
`hasWikiPlot is True` and refuses a record whose plot is empty; every batch builder draws
from that corpus and records the assertion in its manifest. The 19,255 enriched rows
carrying TMDB prose in `overview` cannot reach a batch file, because they are not in the
corpus at all. TMDb §1.C bars sending that prose to an AI application.

**Tags stay label-like.** The tag prompt caps a tag at five kebab-case words and says
explicitly why: a sentence-length tag is an abridgement of a Wikipedia plot under CC BY-SA.
`aggregate_tags.py` rejects anything longer. (v1 is not clean here — 2.54% of its 246,307
tag strings run past five words, up to `undercover-officers-hide-their-identity-from-their-
own-men`.)

**Keys are `movie:123` / `tv:123`, never bare.** The two TMDB id namespaces overlap. v1's
`tags-raw.json` and `premise-ids.json` are keyed by bare id and got away with it only
because the 940 colliding series had already been dropped upstream — verified: zero
colliding bare ids among the 37,533 shipped. `index_io.load_premise_v1_index` re-derives
the media type rather than assuming `movie`, and asserts the ids are unique.

**Every LLM phase is resumable and verified against an id-set.** Fixed per-batch input and
output paths, chosen by the builder and never by the worker; strict JSON parse that reports
the line and column of a failure instead of skipping; coverage checked against
`manifest.json`'s id-set rather than a glob. All three of those failed in the DT-H de-risk
run and cost a re-run.

**TEST is sealed.** `split.py` is a pure function of the title key, so a title is in the
same half in both rulers — splitting each ruler separately would let a title tuned on via
its co-rating case be graded via its premise triplet. `score_reco.py --half test` prints a
warning to stderr saying it is a gate run.

## Licence note on the co-rating ruler

`v2/eval/reco-cases.json` is a transformation of MovieLens ml-32m (GroupLens, University of
Minnesota). Research use; non-commercial without permission; redistributable only under the
same conditions; cite Harper & Konstan 2015, https://doi.org/10.1145/2827872. It is **eval
data and stays in this repo** — it is never bundled into the app artifact, which ships under
different terms.

`files.grouplens.org`'s TLS certificate expired 2026-08-28. The download pinned the leaf
public key (`sha256//0z56B4z9KWSdME/rNfY1HwVlQcCcdU/sBL4WXJdlJmw=`) rather than disabling
verification outright, and every file was checked against the MD5s published in the
dataset's own README — all four match.

## Running an LLM phase

```sh
python3 scripts/v2/llm_phase.py --phase out-t02/v2/ruler/gen --status
python3 scripts/v2/llm_phase.py --phase out-t02/v2/ruler/gen --list-missing --limit 12
python3 scripts/v2/llm_phase.py --phase out-t02/v2/ruler/gen --verify   # exit 1 on any gap
```

`--list-missing` is the resume list: batches with no output, an unparseable output, or an
output covering under 80% of the ids it was given. Re-running those exact batches is
idempotent — each writes only its own fixed path.

## Embedding: it cannot be done on an Apple Silicon Mac

**The published den-embed image is amd64 only, and it cannot run under emulation here.**
ONNX Runtime's prebuilt binaries require **AVX2**, which Rosetta/QEMU does not provide. The
container starts and `/health` answers — health needs no inference — and then the process
dies with an illegal instruction on the *first embed request*, which reads as "connection
refused" from the client. The log line is the only honest signal:

```
WARNING: This CPU does not support AVX2, which is required by ort's prebuilt ONNX Runtime
binaries. The app will likely crash with an illegal instruction error
```

This also means **`scripts/embed-corpus-run.sh` cannot work on this machine at all**, since
it boots that same published container.

So embedding runs **on the homelab box**, against the service that answers live queries:

```sh
ssh root@pve 'incus exec den -- tee /root/tags.jsonl > /dev/null' < docs.jsonl
ssh root@pve 'incus exec den -- sh /root/run-embed.sh'      # nohup inside the container
ssh root@pve 'incus exec den -- tail -4 /root/embed.log'
```

den-embed is not published to the LAN; from inside the `den` container it is
`http://10.89.0.10:8080` (podman network). Note that a `&` or a redirect written into the
`ssh root@pve 'incus exec …'` string binds to the **pve host shell**, not the container —
put the backgrounding in a script file inside the container instead.

### Two measured properties of the service

- **Throughput is chunk-independent** — 16/32/64 all measured 8.6–8.8 docs/s in a
  microbenchmark. `embed_many` maps `embed_one` serially, so batching removes round-trips,
  not inference; this is the same fact behind the ~4–5 h whole-plot corpus estimate.
- **A full 37,314-document premise embed takes 57 minutes** — measured end to end, ~10.8
  docs/s, slightly *better* than the microbenchmark.

  An intermediate reading of 5.0 docs/s during that run was an artefact worth recording,
  because it is easy to repeat. A `&` and a redirect written inside
  `ssh root@pve 'incus exec den -- … &'` bind to the **pve host shell**, so the backgrounding
  fails there while `incus exec` still starts the process *inside* the container. A second
  launch then leaves two clients competing for den-embed's single serial model lock, and each
  sees roughly half throughput. Check `ps -eo pid,args | grep embed_runner` before trusting a
  rate. (The doubled work was harmless: `tsv_to_blob.py` skips identical repeats, and the
  2,976 documents embedded twice came back byte-identical, independently confirming the
  service is deterministic run-to-run.)
- **Responses are cached.** Re-sending 128 identical texts returns in 0.02 s against 14.7 s
  cold. Undocumented, and it makes the cutoff sweep nearly free: variants that share tag
  strings re-embed at cache speed after the first pass.

## Python environment

`out-t02/v2/.venv` (numpy, scipy). It is under a gitignored `out-*/` directory, so it is
local-only; recreate with `python3 -m venv` + `pip install numpy scipy`.
