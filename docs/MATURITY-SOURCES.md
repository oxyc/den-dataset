# Numeric maturity sources

Den publishes one conservative numeric minimum age. Missing, unrated, ambiguous, and unrecognised values
remain unknown. A source may contribute an age only when its reuse terms permit Den's data lane and its
record is joined to exactly one corpus title through an official identifier or exact title, year, and
director. Across verified observations, the highest age wins.

## Public baseline

| Authority | Reuse basis and attribution | Update path and stable id | Coverage and version semantics | Status |
|---|---|---|---|---|
| KAVI (Finland) | The [IKLU API guide](https://kavi.fi/wp-content/uploads/2020/03/aPI.pdf) permits free use directly or in an external service. Attribute KAVI and link the guide. | Movie-only `/agelimit/` API; IKLU `id`; resumable date-window snapshot because answers silently cap at 100. | Film records can describe a format/cut. Keep observations distinct and take the highest verified age. | Adapter and adaptive collector merged; optional snapshot. |
| CNC (France) | [Open Licence 2.0](https://www.etalab.gouv.fr/licence-ouverte-open-licence/). Attribute CNC and the dataset. | The data.gouv catalogue advertises the current CSV; `N° de visa` is stable. | Theatrical visas, 1945–2025 in the current release. Warning/censure without an explicit restriction is unknown. | Bulk adapter merged and measured. |
| ClassInd (Brazil) | The [official catalogue](https://www.gov.br/mj/pt-br/acesso-a-informacao/dados-abertos/classificacao-indicativa-sistema-classind) labels the dataset CC Attribution and the ministry open-data policy permits reuse and redistribution. Attribute MJSP/ClassInd, link [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), and pin the catalogue metadata. | CKAN catalogue advertises the current audiovisual CSV; `Código` is stable. | Cinema, television, home video/VOD, and other works. Only the exact official `Livre, 10, 12, 14, 16, 18` ladder is mapped. | Adapter merged; current catalogue DNS failure is recorded and skipped, never fabricated. |
| Medietilsynet (Norway) | Government information reuse under [`offentleglova` §7](https://lovdata.no/lov/2006-05-19-16/%C2%A77); attribute Medietilsynet and retain its error/staleness warning. | Complete [published film database](https://www.medietilsynet.no/filmdatabasen/) XLSX; the file lacks a stable row id, so the snapshot uses a deterministic factual-row digest. | Historical film database, 1913–2022. Synopsis and reason prose are excluded. | Bulk adapter merged. |
| Wikidata | [CC0](https://creativecommons.org/publicdomain/zero/1.0/); attribute/link Wikidata for provenance. | Chosen Wikidata item; rating-valued properties include KAVI P5152 and CNC P2758. | Discovery/audit candidates only. These properties contain rating items, not official certificate identifiers, so they cannot bypass the exact official-record linker. | Not maturity evidence. |

Every build manifest records the exact source URL, retrieval time, SHA-256, byte length, reuse basis, and
the factual-only scope. Raw authority snapshots stay in the build bundle; only matched factual observations
enter the corpus.

## Private runtime enhancement

TMDB `release_dates` and `content_ratings` may fill a public-baseline unknown inside Atlas/edge only under
the [TMDB API terms](https://www.themoviedb.org/api-terms-of-use). The
entry must retain the raw value, normalized age, fetch time, and expiry; refresh or delete it within six
months; and support purge. Neither the title-level value nor its derived age may enter a downloadable
dataset, fixture, replay, exported cache, or off-deployment backup. Product attribution is required.

## Excluded unless terms or credentials change

| Source | Reason |
|---|---|
| Common Sense Media | Partnership agreement required. |
| MPA FilmRatings | No documented open bulk/read redistribution licence. |
| BBFC | Electronic data is licensed; public search must not be harvested. |
| FSK | No published open bulk redistribution licence. |
| Australia NCD | Documented API is for approved classification-submission tools, not open bulk reads. |

Wikipedia prose, model judgments, genres, and absence of warnings are never maturity evidence.
