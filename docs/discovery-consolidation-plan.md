# Plan — Consolidate the discovery subsystem into a monorepo (post-FP-2)

**Status:** Proposed · deferred until FP-2 (vectors + classification) lands · Created 2026-07-02

## Decision

Merge the **discovery trio** — `den-dataset` (Swift producer), `den-atlas` (Rust server), `den-embed`
(Python query-embed) — into **one repo, `den-discovery`**. Keep `den` (the tvOS app), `den-scout`,
`den-reel`, `den-subtitles` as separate repos. Land the anti-drift fixes (spec + conformance tests +
single quantizer authority + den-embed CI/Dockerfile) **as part of** the consolidation — the monorepo is
their home, not an alternative to them.

## Why (the reasoning that changed the call)

The three are **one subsystem with one binary/schema contract**: the int8 blob layout, the
`dataset.meta.json` schema, and the quantization params must agree across producer → server → query-embed →
app-consumer. Contract-drift is the **recurring bug class** this session kept surfacing (the `finalize`
dim/label guard, the FNV-mislabel footgun, `dataset.meta.json` flat-vs-nested descriptor).

For a **solo, heavily AI-assisted workflow**, repo fragmentation is itself a driver of that drift: an agent
working "the dataset format" across three repos loses context, makes stale cross-repo assumptions, and can't
change the contract atomically. A monorepo gives **one context, atomic cross-cutting changes, and one CI**
where the contract is defined once and mechanically enforced.

### What the monorepo does and does NOT deliver
- **Does:** one source-of-truth spec + golden fixtures; atomic format changes across all components in one
  commit; one CI running cross-language conformance; a single context for AI-assisted work.
- **Does NOT:** "share the quantizer as a module" — the three are Swift/Rust/Python, so there will still be
  three implementations. Co-location alone unifies *nothing*; the **conformance test** (below) is what
  actually pins them. Don't sell the merge on code-sharing it can't provide.

### Why not fold in the app too
`den` is a large, separate concern (UI/player/nav). Only its **consumer-side** contract test shares the
fixtures — so the app stays its own repo and runs its own conformance test against the *published* golden
fixture (`ProducerConformanceTests` already does this). The subsystem that shares the *build/serve* contract
is exactly the trio, and only the trio.

## Prerequisite

**FP-2 finishes first** — the bge-m3 vector build and the classification run. A repo restructure mid-flight is
pure disruption with in-flight scratch state. Do this as a deliberate, clean-slate step afterward.

## Target layout

```
den-discovery/
  spec/                 # THE source of truth: dataset-format.md (versioned) + golden fixtures
  producer/             # was den-dataset (Swift): worklist→enrich→classify→embed→finalize
  server/               # was den-atlas (Rust): serves the blobs (musl→scratch image)
  embed/                # was den-embed (Python): bge-m3 int8 query/corpus embeds
  .github/workflows/    # path-scoped CI per component + one cross-component conformance job
  README.md             # subsystem overview + the contract
```

Deployment is unchanged in spirit: `server/` and `embed/` still build **separate containers** via
path-filtered workflows and push to GHCR under the same image names, so the homelab watchtower keeps working
with at most a name tweak. The Swift `producer/` is a local build tool, not deployed.

## Phases

### Phase A — Contract spec + golden fixtures (the drift-killer core)
Author `spec/dataset-format.md`, versioned, as the single source of truth:
- **Blob layout:** `[int32 count LE][int32 dim LE]` then `count*dim` row-major int8, rows 1:1 with the labels
  artifact, keyed `mediaType:tmdbId`.
- **Quantization:** L2-normalize the dense vector, `round(x*127)`, clamp `[-127,127]` → int8. (Currently in
  `den-embed/server.py` **and** `den-dataset/Embedder.swift` — the spec de-duplicates the *definition*.)
- **Meta schema:** the exact `dataset.meta.json` fields (datasetVersion, taxonomyVersion, embeddingModel,
  dims, count, quantization, labels/vectors file+sha+bytes, gz, timestamps) and the app-side `DatasetDescriptor`
  mapping (flat producer meta ↔ nested app descriptor — the untested seam an audit flagged).
- **Version semantics:** `embeddingModel` + `dims` + `taxonomyVersion` + `datasetVersion` and what a change to
  each triggers (app re-sync rules).
Ship a tiny **golden fixture dataset** (a handful of titles: labels + vectors + meta) checked into `spec/`.

### Phase B — Repo consolidation
- Create `den-discovery`; move the three in as subdirs, preserving history (subtree merges, or a fresh repo
  with a MIGRATION note if history isn't worth the friction).
- Path-scoped CI: a Swift job (`producer/**`), a Rust job (`server/**`), a Python job (`embed/**`), each
  triggered only on its paths so one language's break doesn't block the others.
- Repoint GHCR image builds; update the homelab watchtower config if image names change.
- Archive the three old repos with a pointer to `den-discovery`.

### Phase C — Cross-language conformance in CI (what actually kills drift)
- **Format conformance:** producer emits the golden fixture → server serves it over HTTP → a consumer parser
  reads it → assert byte-identical blob + schema match. One job, runs on every PR touching any component.
- **Quantizer pin:** feed a known float vector to the Python quantizer and the Swift quantizer; assert
  identical int8 bytes. This is the mechanism that keeps the three implementations honest — the thing the
  merge was mis-sold as "a shared module."
- The **app** keeps its own `ProducerConformanceTests` against the published `spec/` fixture, so both sides of
  the contract are pinned to one artifact.

### Phase D — Single quantizer authority
- bge-m3 via `den-embed` is the live path; `den-dataset`'s `Quantizer.int8` is a **FNV-fallback vestige**.
  Deprecate the FNV embedder path so `embed/` is the *sole runtime quantizer*. If a build-time quantize is
  still needed, keep exactly one copy, guarded by the Phase-C quantizer-pin test.

### Phase E — den-embed hardening (pull FORWARD if it hurts before the mono)
- **Dockerfile that bakes the model into the image** (no HuggingFace download at boot → no crash-loop). Today
  `add_custom_model(sources=hf=...)` downloads at startup — a real production liability with **no Dockerfile
  and no CI**.
- CI: run the tests, build + push the image. This is the single highest-ROI item and is **independent of the
  merge** — if the boot-download fragility is biting, do this first, standalone, then absorb it into the mono.

## Sequencing recommendation
1. **Now / anytime:** Phase E standalone (den-embed Dockerfile + CI + baked model) — urgent, mono-independent.
2. **After FP-2:** Phase A (spec + fixtures) → Phase B (consolidate) → Phase C (conformance CI) → Phase D
   (quantizer authority). A–D are a single focused restructuring effort.

## Costs / risks + mitigations
- **Three toolchains in one repo** → path-scoped CI jobs; clear subdir boundaries; per-component READMEs.
- **Deploy cadence differs** → path-filtered image builds; independent tags; watchtower unaffected beyond a
  name tweak.
- **Git history** → subtree merge to preserve, or accept a clean cut with a migration note.
- **Blast radius** (one repo's CI red blocks merges) → path-scoped required checks so a Python failure doesn't
  gate a Rust-only PR.

## Out of scope
- Folding `den` (the app) or the addon repos (scout/reel/subtitles) into the mono — different concerns, no
  shared build/serve contract.
- Any runtime behavior change to the shipped dataset (this is structural + CI only).
