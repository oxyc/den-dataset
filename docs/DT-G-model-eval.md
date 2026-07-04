# DT-G — Model eval: Opus vs Sonnet vs Haiku for taxonomy classification (t02)

Run 2026-07-03 via subagents on the v2 (`t02`) vocab + prompt, over real enriched records. Decides the
model split for the wiki-plot re-label.

## Set A — extremes (33 titles: 15 famous vc>3k + 18 obscure vc<60)
- **Primary-genre agreement:** Opus=Sonnet **78%**, Opus=Haiku **51%**, all-three 45%.
- **World-knowledge labels (Cult/Art House/Epic) on FAMOUS titles:** all three models got them right
  (Donnie Darko/Lebowski/Fight Club/Rocky Horror/Pulp Fiction → Cult; Tree of Life/Seventh Seal/Mulholland
  Dr → Art House; Lawrence/Ben-Hur → Epic). Even Haiku recognises famous films — the "only Opus knows movies"
  theory is only half true.
- **The tell is CALIBRATION on the obscure tail:** *Stockholmsnatt* (Swedish, vc 15) → Opus correctly
  assigned **nothing**; Sonnet AND Haiku **hallucinated "Cult."** Opus follows the "omit unless you recognise
  it" rule; the smaller models don't.
- **Haiku is the real drop-off** (Sonnet isn't): shallow primary genre (The Matrix→Action, Fight Club→
  Thriller, Big Lebowski→Crime), hallucinated subgenres (**Superhero** on The Matrix, **Supernatural Horror**
  on The Seventh Seal), and a **vocab violation** (used "Epic" as a *mood* — it's subgenre-only).

## Set B — mid-tier (30 titles, vc 150–2,000), Opus vs Sonnet
- **Primary-genre agreement: 93%** (higher than the famous set). Only 2/30 differ, both genuine dramedies.
- **World-knowledge labels: ZERO divergence.** Sonnet matched Opus on every Cult/Art House/Epic/LGBTQ+ call,
  incl. Mac and Me→Cult, Crash→Art House, Shadows in Paradise→Art House (Kaurismäki), Saturn in Opp.→LGBTQ+.
  **No hallucination in the mid-tier** — it's confined to the deep tail (vc<60).
- Subgenre overlap Jaccard 0.57 — differences are stylistic verbosity/granularity (Romance vs Romantic
  Drama), not correctness.

## Verdict
- **Sonnet ≈ Opus** everywhere that matters (93% mid-tier, identical prestige labels, clean vocab). **Don't
  widen Opus** — it's diminishing returns; the money's better on Sonnet coverage.
- **Haiku is too noisy** for head/mid (shallow genre, hallucinated labels, vocab violations). Use only for the
  deep tail where output is post-filtered, or skip it.
- **Split:** Opus top ~2–8k (highest traffic + best calibration) · **Sonnet the bulk** · Haiku deep tail/skip.
- **Cheaper than more Opus:** gate the world-knowledge labels (Cult/Anime/Art House/Epic) on a **min vote
  count (~100)** — the sole failure mode (tail hallucination) fixed by a one-line rule, not a model upgrade.
- Store all runs keyed by `(tmdbId, taxonomyVersion, model)` so t01/TMDb vs t02/wiki and per-model are A/B-able.
