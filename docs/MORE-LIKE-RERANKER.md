# Jev More Like This gate

Issue #132 does not authorize a corpus precompute merely because the title-and-year prior beat plot cosine.
The article-aware question shape must first improve on that prior on the already measured step-9d ruler.

`tools/rulers/more_like_gate.py` freezes that experiment. It performs no call unless `run --spend` is given.
The preparation step requires an eval-only export of the 2,100 step-9d cases; MovieLens-derived rows stay out
of the published dataset and out of git.

The export is one JSON object with schema `issue18-step9d-export-v1`, the step-9d comment URL as
`sourceComment`, and `cases`. Each case has:

- a unique `pairId`;
- `anchor`, `positive`, and `negative`, each with `key`, `title`, and `year`;
- the stored Jev `titleYear` positive and negative Nouls;
- the measured `plot` positive and negative cosine values;
- `controls.yearGapEqual=true`, `controls.seedGenreEqual=true`, and the absolute
  `controls.popularityLogGap` (at most 0.12).

Preparation selects exactly 512 cases by a fixed hash of `pairId`. It joins current Wikipedia articles,
uses the lead and extractor-selected story sections, caps each work's evidence equally, blinds positive and
negative as candidate A/B, and writes exact state, question, input, and selection hashes:

```sh
python3 tools/rulers/more_like_gate.py prepare \
  --ruler /private/path/step9d-export.json \
  --articles out-repass/articles.jsonl \
  --work /private/path/more-like-gate

python3 tools/rulers/more_like_gate.py run \
  --work /private/path/more-like-gate \
  --out /private/path/more-like-gate/answers.jsonl
```

The second command is the dry run. Read its exact call and state-character counts before replacing it with
`--spend`. The paid path is pinned to `jev-1.13.0`, resumes append-only, and defaults to a hard $1 spend
cap. Before each request it reserves a deliberately pessimistic ceiling: every UTF-8 byte of the exact JSON
request counted as one token, plus 1,024 tokens for the provider wrapper (the measured fixed component was
about 265). Usage returned by completed calls is stored per row and counted again after a resume. The dry plan
reports whether all remaining calls fit even if every one reaches that ceiling; raise the explicit cap only
after reading that number.

Each call carries one anchor and two blinded candidates. Jev returns six bounded Nouls and one bounded
Choice per candidate; it generates no prose. The overall Noul is preregistered as the only ranking score.
The other five axes and the verdict are diagnostics and cannot be tuned into a better result after the run.

```sh
python3 tools/rulers/more_like_gate.py score \
  --work /private/path/more-like-gate \
  --answers /private/path/more-like-gate/answers.jsonl
```

The gate passes only when the pair-bootstrap 95% lower bound is at least 0.70 for article AUC, above zero
for article minus title-and-year, and above zero for article minus plot. A failure means no full precompute.
A pass only makes that precompute eligible: `rail-eval`, `rail-ab`, row-shape cases, movies/TV reporting, and
deterministic fallback remain required. The gate itself measures movies with at least 50 MovieLens likes;
it establishes nothing about TV or the long tail.
