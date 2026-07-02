# Taxonomy (t01) + golden eval-set backlog

Findings from the 2026-07-02 audit of `Sources/DenDataset/Taxonomy.swift` (`Taxonomy.current`, t01) and the
golden sets (`den` app repo: `Tests/DenKitTests/Fixtures/taxonomy/golden.json` [53], `golden-large.json`
[2,623]). The vocabulary is drift-free and downstream-correct; these are improvements for a **t02** pass and
for making the F1 gate trustworthy. Nothing here blocks a t01 run — but a taxonomy change is cheaper *before*
the full 57k classification than after (a bump triggers targeted reclassification).

## Golden set — needed before the F1 gate is trustworthy
- **`golden.json` (53) is a strict subset of `golden-large.json`** — 100% movies, 10/18 genres, no TV. Keep it
  as a smoke test; **exclude it from the gate**; note the subset relationship in DT-B (currently undocumented).
- **Ungradeable labels (0 golden positives → recall never validated):** Animation (primary); the 5 emergent
  themes Giallo / Hong Kong Action / Narco / Samurai / Telenovela; the **entire regional axis** (13 labels —
  `GoldenTitle` has no `regional` field, so it is structurally unscoreable). ~19 of ~93 labels.
- **Actions:** add ≥25–30 golden examples each for Animation + the 5 emergent themes; add a `regional` field to
  `GoldenTitle` and label it (or record in DT-B that regional is intentionally recipe-only + out of F1 scope);
  top up <15-support labels (Medical Drama, Mockumentary, Cyberpunk, History) to ~30.
- **Gate change:** per-family F1 with a **minimum-support guard** — skip/relax a label whose golden positives
  < N so a tiny-support label can neither fail nor pass the whole run. (Macro-F1 currently lets a 3-title label
  swing the mean.)
- Distribution skews (judgment): 60% of golden-large is 2010+; moods dominated by Feel-good/Dark & Gritty; no
  language/region signal. Rebalance only if the eval should mirror the 57k catalogue rather than "popular now".

## Taxonomy vocabulary — coverage gaps (t02 candidates)
- **Sitcom / multi-cam comedy subgenre** — the biggest TV hole (Seinfeld/Friends/The Office get no subgenre).
  Highest-value add given ~7.8k TV titles.
- **Documentary subtypes** — Documentary is a primary genre with no substructure. Add **True Crime** (marquee
  streaming row, only partially reachable via the "Serial Killer" theme), Nature/Wildlife, Music Doc, Sports
  Doc, Docuseries.
- **Stand-up Comedy** — large streaming vertical, currently unclassifiable.
- **Reality / Competition (TV)** — unscripted has no home.
- **Animation substructure** — distinguish Kids/Preschool from general Family (no anime, per rule).
- **Anthology** and **Limited Series/Miniseries** — common TV-structure discovery filters, absent.
- **Holiday/Christmas** — standard seasonal row, absent.

## Taxonomy — redundancy / orthogonality (judgment calls)
- Positive-affect moods over-split: merge **Cozy + Comfort-watch → Feel-good/Wholesome** (drops the two
  lowest-support moods; Toy Story currently gets all three).
- **Bingeable** (a serialization property) and **Visually-stunning** (a production property) fail the mood
  axis's own "assignable from a plot synopsis" test — drop or move off the mood axis.
- **Musical** ships as a *theme* while **Music** is a primary genre — a form, not a cross-cutting theme.
- **Telenovela** is regional (Latin-American/Spanish); **Giallo / Samurai / Hong Kong Action** are region+theme
  hybrids — reclassify as regional or accept the overlap explicitly.
- Regional internal overlaps: Nordic Noir vs Scandinavian, K-Drama vs Korean Thriller — consider collapsing.
- "Dark & Gritty" (2nd-most-assigned mood) overlaps Neo-Noir / Dark Comedy — tighten the classifier definition
  so it isn't a default for anything violent.

## Doc drift (cheap, fix on next taxonomy touch)
- `den/tickets/DT-taxonomy.md` says "19 TMDB genres" (ships **18**), still lists Western-as-theme + Courtroom
  (both dropped in code), and omits the 5 emergent themes + "Musical". Regenerate the doc from `Taxonomy.swift`
  (code is the source of truth).

## Verdict
t01 is solid enough to *run* on; it is **not yet enough to fully grade** a 57k run (~20% of labels ungradeable,
Sitcom missing). Before trusting the gate: add golden coverage for Animation + the emergent themes, decide
regional's scoring status, and add the minimum-support guard. Sitcom (+ True Crime) are the taxonomy adds most
worth making *before* the full classification, since retrofitting means reclassifying.
