# What this pipeline has taught, the hard way

Findings that cost real time to learn and are cheap to re-learn wrongly. Not operating guidance —
`docs/OPERATE.md` is the procedure; this is why it has its shape.

## Labelling with a model

**Close the vocabulary.** The highest-leverage finding in the project. Open-vocabulary tags agree ~11%
between two runs of the same model on the same plot; picking from a closed list agrees 97.5–100%. Voting
cannot rescue an open vocabulary: a tag has to be named identically twice to survive, so 2-of-3 voting cut
15.22 tags a title to 6.34. A closed list is also what makes errors catchable: six agents put a tone word
(`bleak`) into the `ending` axis, and a validator caught every one because `bleak` is not in that list.

**Say what to do, not what to avoid.** Listing forbidden words did not stop the `bleak` error. "Ask how it
RESOLVED, not how it FELT" did.

**Density is a gate, not a statistic.** A labelling run came back with valid JSON, exact counts, real ids and
in-vocabulary labels — and 46–70% of titles with no subgenre at all. Only a title seen in two batches
(*Blake's 7*: full labels in one, "Drama" in the other) exposed it. Smaller slices and a prompt stating the
expected density fixed it; a batch far below that density is refused.

**Ask content questions before genre.** Once a genre token is in the output, later answers have reason to
agree with it, which turns a content question into a genre transform.

**The questions are what a Jev pass costs, not the article.** Jev bills question text as input at the
article's rate: ~377M of the classify pass's 487M input tokens were question text (#56). Shorter
definitions are the saving; a cheaper state is not.

## Running a batch of agents

- **A correct row count proves nothing.** Observed here: fabricated ids with exact counts (in 3 of 12
  batches), a duplicated key silently replacing another title, a key-shift carrying a neighbour's tags, and a
  title dropped by two independent agents. Verify keys element by element against the input, and refuse a
  batch with one fabricated id whole.
- **Coverage is checked against the manifest's id set**, never against `ls out/`.
- **Batch size belongs to the model, not the task.** 40 titles fits Haiku's output cap; Sonnet overran it and
  wrote an empty file, which reads as unrun rather than over-asked.
- **A batch can die of deliberation, silently.** A judging worker spent its whole 64,000-token output budget
  thinking and wrote nothing, which looks exactly like a batch nobody ran. The only signal is wall-clock: a
  batch well past its siblings' duration needs checking, not waiting. Judgement-heavy prompts carry a
  "decide on the plain reading" instruction in the prompt file, where it survives the next run.
- **Don't read an agent's output before its completion notification.** A half-written file produced three
  wrong claims in one session.

## Everything else

**A batch size can be a correctness parameter.** A 100-id SPARQL batch that returns in ~1 s from one client
timed out at 60 s from another; 25 advanced steadily.

**Check what a resume actually skips.** A facts scrape froze at 16,500 titles through 22 restarts: an
entity-resolution step re-ran over the whole checkpoint on every restart, so no restart reached a new batch.

**An artifact whose source is not committed will be described wrongly.** See `data/README.md`.

**A fact belongs in one document.** The embedder rule was stated in two, one went stale, and a false
"resolved" survived beside a correct table.

**Measure before extrapolating from the first sample.** A rate read off a run's first minute projected 50
hours for work that took 3; both it and a token estimate 40% low were sampled during a cold start. den-embed
also caches responses (128 repeated texts: 0.02 s against 14.7 s cold), so a benchmark that re-sends the same
texts measures the cache.
