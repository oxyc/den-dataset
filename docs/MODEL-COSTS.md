# Model costs

What each paid model step costs, what was measured against what was estimated, and why each model was chosen. Use it to set spend caps (#179) and to judge a model switch (#182, #183).

Prices are list prices when measured, per 1M tokens (input / output). Thinking and reasoning tokens bill as output. Batch is 50% off standard on Anthropic, OpenAI and Gemini. Gemini 3.6–3.8 Flash doubles on 2027-01-01.

## The daily job

About **5 new titles a day** reach the catalogue. Release years whose admissions have caught up (2015–2019) added ~1,460 films a year; series add ~250 a year. Recent release years keep filling in, because a title is admitted only once it has a Wikipedia article or enough votes. The first live run (2026-09-30) added 2.

| Step | Model | $/title | $/year at 5/day | Source |
|---|---|---:|---:|---|
| Fan picks | Gemini 3.7 Flash, low thinking, online | ~$0.003 | ~$5.50 | measured, daily run 2026-09-30 |
| Premise tags | Claude Haiku 4.5 | $0.0017 at 5/call, $0.0012 at 40/call | ~$2–3 | measured (#182 bake-off; #146's real batches) |
| Genre & mood labels, facts delta | TypeSafe (Jev) | ~$0.0009 | ~$1.60 | measured, daily run 2026-09-30 |
| **Total** | | **~$0.0056** | **~$10** | |

Premise tags aren't in the daily job yet (#141, #179). An early estimate of $0.009 a title was about 5× too high.

**Caching doesn't help here.** Prompt caching only pays when the same prefix repeats within minutes, and the daily job makes about one call per step a day. Haiku 4.5's premise prompt (~2k tokens) is also below its minimum cacheable prefix.

**Batch halves the bill but costs a day of latency.** Batch jobs waited 5 minutes to 13 hours in #121, so the job would submit one day and collect the next. At today's volume that saves ~$4 a year.

## Whole-corpus runs

| Run | Titles | Model | Cost | $/title | Notes |
|---|---:|---|---:|---:|---|
| Genre & mood classification, first generation | 47,529 | TypeSafe (Jev) | ~$20 | ~$0.0004 | bought by hand, never by a timer (`OPERATE.md`) |
| Premise tags gap fill (#146) | 5,806 | Claude Haiku 4.5, 40 per call | $7.04 accepted, $8.86 with retries (API list; run on the Claude plan through the CLI) | $0.0012 | 147 accepted calls of 193 attempts; plots averaged 1,509 characters |
| Fan picks, every title (oxyc/den-atlas#121) | 52,985 | Gemini 3.7 Flash, low thinking | $82.45 ($1.53 sample, $80.92 full run) | $0.0016 | mostly Batch; ~3,400 titles the Batch service refused (unbilled) were re-asked at standard price |

## Fan picks: why Gemini 3.7 Flash (oxyc/den-atlas#121, 2026-09)

The step asks a model to name up to 20 films or series a fan of a title would love ("taste, not similarity"). Store matching then keeps only titles we hold. The inputs are title, year, type and the Wikipedia lead, from our own data only.

**Lists, 12 seeds.** "Wire list hit" is overlap with a hand-written list for *The Wire*; that list was Claude-written, so it favours Opus.

| Model | Wire list hit (of 20) | Picks matched in store | Verdict |
|---|---:|---:|---|
| Claude Opus | 13 | 96–98% | most reliable; leans on the obvious; returns sequels |
| gpt-5.6-sol (Codex) | 9 | 99% | best at "taste, not similarity" |
| gpt-5.6-terra (Codex) | 8 | 98% | good but uneven |
| Claude Sonnet | 10 | 90–93% | closest to plain similarity; some garbled titles |
| **Gemini 3.7 Flash** | 9 | 99% | strong across the board |
| Gemini 3.7 Flash, low thinking | 9 | 99% | differs from default by ~1–2 picks a title beyond run-to-run noise |
| Gemini Pro | 8 | 98% | widest reach, pads crime seeds with crime films |
| Gemma 4 31B | – | – | returned notes instead of JSON, ~60 s a call |

**Knowledge, 150 titles across popularity.** Each model was asked each title's director or creator, and the answer checked against Wikidata.

| Model | Top 1k | 1–3k | 3–6k | 6–10k | 10–20k | 20–53k | Wrong claims (of 150) |
|---|---:|---:|---:|---:|---:|---:|---:|
| Gemini 3.7 Flash | 25 | 25 | 25 | 24 | 22 | 20 | 3 |
| Gemini Pro | 25 | 24 | 25 | 24 | 21 | 21 | 4 |
| Claude Opus | 25 | 24 | 25 | 24 | 21 | 21 | 5 |
| gpt-5.6-sol | 25 | 24 | 25 | 24 | 21 | 22 | 5 |

All four know the whole catalogue, deep tail included. A split by popularity is a budget choice, not a knowledge one.

**Cost.**
- Measured on Gemini: ~351 input / 665 output tokens a title at low thinking, 0 thinking tokens.
- Other models are projected at ~500 input / 700 output tokens a title.
- Claude figures are measured at one title per call.

| Model | Price (in / out) | ~53k titles, standard / batch |
|---|---|---:|
| **Gemini 3.7/3.8 Flash, low thinking** | $0.75 / $3.75 (to 2026-12-31, then doubles) | ~$145 / **~$73** |
| Gemini 3.7/3.8 Flash, default thinking | same | ~$360 / ~$180 |
| Gemini Pro | $2 / $12 | ~$1,750 / ~$875 |
| Claude Opus | measured $0.020/title | ~$1,055 / ~$530 |
| Claude Sonnet | measured $0.0125/title | ~$665 / ~$330 |
| gpt-5.6-sol | $4 / $20 | ~$850 / ~$425 (no extra $ on the ChatGPT plan, but plan-capped) |
| gpt-5.6-terra | $2 / $12 | ~$500 / ~$250 |
| gpt-6-sol | $2 / $10 | ~$425 / ~$212 |
| gpt-5.6-luna | $0.20 / $1.20 | ~$50 / ~$25 (knowledge untested) |
| gpt-6-luna | $0.10 / $0.50 | ~$21 / ~$11 (knowledge untested) |

**Decision:** Gemini 3.7 Flash, low thinking, through the Batch API, for every title. It knows the catalogue as well as Opus and sol, its lists were at least as good, and it was the cheapest tested model with that quality. The key was already in place, and Batch has no plan limits. Default thinking costs 2.5× for changes within noise. Running before the 2027 price doubling saved about half.

**Outcome:**
- 52,927 of 52,985 titles answered; 58 are refused by Gemini every time.
- 92.0% of picks matched a stored title: 97.4% in the top 5k, 86.7% at the tail.
- 99.2% of titles keep ≥10 picks.
- The model said it didn't know 0.4% of titles.

## Premise tags: bake-off (#182, 2026-09-30)

Setup:
- **Sample:** 457 titles from the 154 DEV premise-similarity triplets.
- **Prompt:** #146's (`premise-tags-v1.SPEC.md`), 5 titles a call (luna 10).
- **Scoring:** every arm embedded by one den-embed and scored with `score_triplets` and McNemar against Haiku.

| Model | Triplet accuracy | vs Haiku | $/title (5/call) | Problems |
|---|---:|---|---:|---|
| Claude Haiku 4.5 | 0.786 (154) | – | $0.0017 | none; tagged every title |
| gpt-5.6-luna (Codex) | 0.800 (40) | 4/5, p=1.0 | ~$0.0003 | a tag with a space; needs an OpenAI API key |
| Gemini 3.5 Flash-Lite | 0.813 (150) | 14/19, p=0.49 | $0.0005 | malformed JSON without a schema; rows under 8 tags; tags with spaces |
| Gemini 3.1 Flash-Lite | 0.800 (150) | 13/16, p=0.71 | $0.0008 | ~379 thinking tokens a title |
| Gemini 3.7 Flash | 0.800 (150) | 18/21, p=0.75 | $0.0010 (2027: $0.0020) | ~1 in 4 five-title calls malformed without a schema |
| Gemini 3.8 Flash | 0.833 (150) | 9/17, p=0.17 | $0.0010 (2027: $0.0020) | same |
| Gemini 3.1 Pro, low thinking | 0.935 (31) | 0/7, p=0.016 | $0.0051 | only significant win; small sample |
| Shipped tags (v2) | 0.760 (154) | 23/19, p=0.64 | – | – |

- **Quality:** no cheaper model differs significantly from Haiku. At 150 triplets the 95% interval is about ±6.5 points.
- **Refusals:** every Gemini model refuses ~0.9% of titles (sexual-violence plots), and one refusal sinks a multi-title call. Haiku tagged them all.
- **Style:** Haiku writes longer, event-level tags (3.7 words a tag), luna 3.3, Flash-Lite 2.5. Mixing styles in one index is untested (#182).

**Re-tagging all 50,503 titles** at 40 a call. Each range spans a plot extract of 1,509 to 2,356 characters. The cost fit predicts #146 at $0.00113 a title against $0.0012 measured.

| Model | Standard | Batch |
|---|---:|---:|
| Claude Haiku 4.5 | $57–68 | $29–34 |
| gpt-5.6-luna | $10–12 | $5–6 |
| Gemini 3.5 Flash-Lite | $19–21 | $9–11 |
| Gemini 3.1 Flash-Lite | $33–36 | $17–18 |
| Gemini 3.7 Flash (2026) | $34–41 | $17–21 |

Untested: 40 titles a call on Gemini or luna. Test it with a response schema before a full run.

## Notes

- **Output dominates price.** Asking for 15 picks instead of 20 cuts fan-pick cost by about a quarter.
- **Thinking is the hidden cost.** Default thinking on Gemini Flash was 2.5× the price of low with no clear gain; on Pro it roughly tripled the price.
- **Monthly plans.** Claude and ChatGPT plans run models through their CLIs at no extra $, limited by plan caps. They suit local backfills and bake-offs, never the daily job.
- **Refusals** must not sink a batch: retry one title per call, then record the title as refused (#170).
- **Price changes.** Update this file when a price changes or a run is measured, and cite the issue.
