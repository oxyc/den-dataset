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

**Decided changes** (#183, #187), not built yet:

| Step | Change | New $/title |
|---|---|---:|
| Premise tags | gpt-5.6-luna, Haiku as fallback for refusals | ~$0.0003 |
| Fan picks | compact answer format (format B) | ~$0.0017 |
| Jev | classify and critique in one call | −13% on those two |

Together that's about **$0.0029 a title, ~$5 a year** at 5 titles a day. Weekly Batch runs for premise tags and fan picks would halve their part again.

**Caching doesn't help here.** Prompt caching only pays when the same prefix repeats within minutes, and the daily job makes about one call per step a day. Haiku 4.5's premise prompt (~2k tokens) is also below its minimum cacheable prefix.

**Batch halves the bill but costs a day of latency.** Batch jobs waited 5 minutes to 13 hours in #121, so the job would submit one day and collect the next. At today's volume that saves ~$4 a year.

## Whole-corpus runs

| Run | Titles | Model | Cost | $/title | Notes |
|---|---:|---|---:|---:|---|
| Genre & mood classification, first generation | 47,529 | TypeSafe (Jev) | ~$20 | ~$0.0004 | bought by hand, never by a timer (`OPERATE.md`) |
| Premise tags gap fill (#146) | 5,806 | Claude Haiku 4.5, 40 per call | $7.04 accepted, $8.86 with retries (API list; run on the Claude plan through the CLI) | $0.0012 | 147 accepted calls of 193 attempts; plots averaged 1,509 characters |
| Premise tags re-tag of misattributed rows (#184) | 392 | Claude Haiku 4.5, 20 per call | $1.18 with one retry (API list; run on the Claude plan through the CLI) | $0.0030 | 21 calls for 20 batches; plots averaged ~3,900 characters, over twice #146's |
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

## Genre & mood labels

A primary genre, up to three subgenres and up to three moods per title, stored in `data/genres-moods-curated.json`.
- **Existing titles:** labelled in July 2026 by Claude Code subagents on the owner's plan, so no API spend. No code in the repo regenerates them.
- **New titles:** labelled automatically by the daily job's TypeSafe (Jev) genres & moods call, ~$0.0003 a title (daily run 2026-09-30).
- **Hand enrichment** (`./den genres-moods prepare | merge`, `pipeline/genres_moods_enrich.py`): an optional tool that overrides the automatic labels with agent-written ones for chosen titles.
  - Agents run on the monthly plans at no API cost, optionally with web search (`--web`).
  - Last used for the 2,285-title batch in #56.
  - Not planned for the foreseeable future: the automatic labels cover new titles.

## Premise tags: bake-off (#182, 2026-09-30)

Setup:
- **Sample:** 457 titles from the 154 DEV premise-similarity triplets.
- **Prompt:** #146's (`premise-tags-v1.SPEC.md`), 5 titles a call (luna 10).
- **Scoring:** every arm embedded by one den-embed and scored with `score_triplets` and McNemar against Haiku.

| Model | Triplet accuracy | vs Haiku | $/title (5/call) | Problems |
|---|---:|---|---:|---|
| Claude Haiku 4.5 | 0.786 (154) | – | $0.0017 | none; tagged every title |
| gpt-5.6-luna (Codex, 10/call) | 0.838 (154) | 10/18, p=0.18 | $0.0003 ($0.00015 Batch) | 10 of 46 calls had one tag with a space |
| Gemini 3.5 Flash-Lite | 0.813 (150) | 14/19, p=0.49 | $0.0005 | malformed JSON without a schema; rows under 8 tags; tags with spaces |
| Gemini 3.1 Flash-Lite | 0.800 (150) | 13/16, p=0.71 | $0.0008 | ~379 thinking tokens a title |
| Gemini 3.7 Flash | 0.800 (150) | 18/21, p=0.75 | $0.0010 (2027: $0.0020) | ~1 in 4 five-title calls malformed without a schema |
| Gemini 3.8 Flash | 0.833 (150) | 9/17, p=0.17 | $0.0010 (2027: $0.0020) | same |
| Gemini 3.1 Pro, low thinking | 0.935 (31) | 0/7, p=0.016 | $0.0051 | only significant win; small sample |
| Shipped tags (v2) | 0.760 (154) | 23/19, p=0.64 | – | – |

- **Quality:** no cheaper model differs significantly from Haiku. At 150 triplets the 95% interval is about ±6.5 points.
- **Refusals:** every Gemini model refuses ~0.9% of titles (sexual-violence plots), and one refusal sinks a multi-title call. Haiku tagged them all.
- **Style:** Haiku writes longer, event-level tags (3.7 words a tag), luna 3.3, Flash-Lite 2.5.
- **Mixing is safe.** Replacing 10% or 30% of shipped tags with luna's, Flash-Lite's or fresh Haiku's doesn't lower accuracy, including on triplets that mix sources (#182). So new titles can switch model without re-tagging the rest.

**Re-tagging all 50,503 titles** at 40 a call. Each range spans a plot extract of 1,509 to 2,356 characters. The cost fit predicts #146 at $0.00113 a title against $0.0012 measured.

| Model | Standard | Batch |
|---|---:|---:|
| Claude Haiku 4.5 | $57–68 | $29–34 |
| gpt-5.6-luna | $10–12 | $5–6 |
| Gemini 3.5 Flash-Lite | $19–21 | $9–11 |
| Gemini 3.1 Flash-Lite | $33–36 | $17–18 |
| Gemini 3.7 Flash (2026) | $34–41 | $17–21 |

Untested: 40 titles a call on Gemini or luna. Test it with a response schema before a full run.

**Decision:** gpt-5.6-luna for new titles, with Haiku 4.5 as fallback for titles luna refuses. Existing tags stay (#183).

## Cost optimizations (#187, 2026-09-30)

Spend on these measurements: $2.80.

**Paying twice.**
- Paid answers lived only in one run's out-dir, and the workflow keeps nothing between runs. A run that failed or didn't publish had its titles bought again the next day.
- It happened twice on 2026-09-30, ~$0.009. The failed runs of 09-27/28 ran without spend, so they cost nothing.
- **Fix:** a paid-answers ledger outside the run, checked before every paid call. This is now a hard rule.

**Combining Jev calls.**
- Classify and critique in one call: $0.00099 → $0.00087 a title (−13%). The answers differ no more than two runs of the same call.
- Folding in genres & moods too: rejected. It would read the whole article instead of its selected sections, and its answers drift ~4× past noise.
- Sharing one call between fan picks and premise tags: rejected. They read different text, so it saves only ~$0.12 a year.

**Jev against cheaper models** (genres & moods, 60 golden titles, one title per call):
- Gemini 3.5 Flash-Lite, 3.7 Flash and luna scored about the same as Jev.
- They cost 2–8× more a title ($0.00072–0.0026 against Jev's $0.00034), and label in a different style.
- Only luna batched 20 a call through Batch would undercut Jev, by ~$0.37 a year. **Jev stays.**
- Why Jev is cheap: most of its input is question text (the first classify pass sent 487M input tokens, 377M of them questions), billed at roughly $0.04 per 1M.

**Compact fan-pick answers** (Gemini 3.7 Flash, low thinking, still 20 picks):

| 300 titles | Current (keyed objects) | Format B (`{"k":bool,"p":[["Title",1999,"f"]]}`) |
|---|---:|---:|
| $/title | $0.00293 | $0.00166 (−43%) |
| Output tokens/title | 680 | 308 |
| Picks matched in store | 93.8% | 93.2% (~1.5 points lower past rank 20k) |
| Picks kept/title | 18.06 | 18.22 |
| Titles with ≥10 picks | 99.3% | 99.3% |
| Blind judgment of kept picks, 50 tail titles (0–2) | 2.00 | 1.98 (7 vs 6 preferred, 37 same, p=1.0) |

- Format B's lower match rate is extra names that never match and are dropped, so the row doesn't lose picks. **Ship format B.**
- A plain-text answer made Gemini think ~15× longer and cost more.

**Cadence.** Each step gets a daily, weekly or "N waiting" setting. Weekly steps run through Batch, submitted in one run and collected in the next. atlas already copes with a title that has no fan picks or premise vector yet.

## Fan picks for new releases (#189)

A model can't know fans of a title released after its training data. In the fan-picks run, Gemini 3.7 Flash didn't know 5.9% of 2025 titles and 35.2% of 2026 titles. For those it guessed picks from the Wikipedia lead.

Web-search test, 50 unknown 2025–26 titles, blind judgment 0–2:

| Option | Score | Matched picks/title | $/title | $/month at ~30 titles |
|---|---:|---:|---:|---:|
| Stored answer (no search) | 1.76 | 15.0 | $0.0028 | $0.08 |
| Gemini 3.8 Flash, no search | 1.64 | 13.9 | $0.0019 | $0.06 |
| Gemini 3.7 Flash + separate Google-search research call | 1.90 | 17.9 | $0.043 | $1.30 (~$0.14 within the free search allowance) |
| Claude Sonnet + web search (15 titles) | 1.27 | 17.5 | ~$0.048 | ~$1.43 |
| Gemini 3.5 Flash-Lite + search (20 titles) | 1.40 | 16.6 | $0.024 | $0.72 |
| gpt-5.6-luna + web search | 1.26 | 19.0 | $0.012 | $0.37 |

- **A thin prompt is the problem, not a new title.** With a full English lead, the stored answer already scored 2.00. With a one-line foreign stub, the model guesses the premise from the name.
- **Search pricing:** Gemini Google-search grounding includes 5,000 searches a month free, then $14 per 1,000. It only works as its own call: with JSON output requested, a search-enabled call comes back empty.
- These scores are one model's judgment, and the judge didn't know the titles either, so they measure plot fit more than taste.

**Checked against viewers instead.** 32 unknown 2025–26 titles with Reddit "like X" threads: 326 threads, 6,184 viewer recommendations. Critic text was kept out of this ruler.

| Context given to Gemini 3.7 Flash | Picks of 20 viewers also named | $/title |
|---|---:|---:|
| Title + lead (today) | 0.42 | $0.003 |
| + Wikipedia Reception section | 0.43 | $0.004 |
| Critics' comparison sentences | 0.43 | ~$0.03 |
| Comparisons + lead + Reception | 0.46 | ~$0.03 |
| Gemini search, critic reviews only | 0.44 | $0.044 ($0.004 billed) |
| Makers' interviews and festival notes | 0.44 | ~$0.03 |
| Full stored article | 0.42 | $0.004 |

- No method differs from today's prompt (every p ≥ 0.68). A list made for a different title scores 0.07.
- Critic comparisons fix some titles (*Sorry, Baby* 1 → 7 hits) and hurt others (*Stick*: golf films).
- **Decision:** new titles keep today's prompt (#189). Test cost $0.76 API; the searching and reading ran on the Claude plan.

## Notes

- **Output dominates price.** Fan picks stay at 20. The compact answer format cut their output tokens by more than half (#187).
- **Thinking is the hidden cost.** Default thinking on Gemini Flash was 2.5× the price of low with no clear gain; on Pro it roughly tripled the price.
- **Monthly plans.** Claude and ChatGPT plans run models through their CLIs at no extra $, limited by plan caps. They suit local backfills and bake-offs, never the daily job.
- **Refusals** must not sink a batch: retry one title per call, then ask the step's fallback model (#183).
- **Never pay twice.** A paid answer is kept outside the run and reused by every later run; only a deliberate re-ask (new model or spec) pays again (#187).
- **Price changes.** Update this file when a price changes or a run is measured, and cite the issue.
