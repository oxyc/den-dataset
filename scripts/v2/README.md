# v2 discovery-index work — the pipeline and the rules it enforces

Everything here builds **new** artifacts. Nothing in this directory writes to the plot
index, the t02 labels, or the v1 premise index; v2 ships as separate blobs and separate
manifest keys, and is dropped by deleting them.

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

- **~8.7 docs/s on tag-length documents, independent of chunk size** (16/32/64 all measured
  8.6–8.8). `embed_many` maps `embed_one` serially, so batching removes round-trips, not
  inference. A full 38,460-document premise embed is therefore **~74 minutes** — cheap next
  to the 4–5 hours a whole-plot corpus embed takes, because tag documents are ~250 chars.
- **Responses are cached.** Re-sending 128 identical texts returns in 0.02 s against 14.7 s
  cold. Undocumented, and it makes the cutoff sweep nearly free: variants that share tag
  strings re-embed at cache speed after the first pass.

## Python environment

`out-t02/v2/.venv` (numpy, scipy). It is under a gitignored `out-*/` directory, so it is
local-only; recreate with `python3 -m venv` + `pip install numpy scipy`.
