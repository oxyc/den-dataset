# Labelling spec: genres & moods

You label films and series with a **primary genre**, up to three **subgenres** and up to three **moods**,
using only the fixed vocabulary at the end of this file. You work on one batch file at a time. Your task
names the batch, and says whether you may search the web.

## What to do

1. Read the input batch named in your task, e.g. `in/batch-003.json` (paths are relative to the directory
   this file is in). It is a JSON array; each item is
   `{key, title, year, media, articleLanguage, articleUrl, premise}`. `premise` is the lead and the
   story/theme sections of the title's Wikipedia article, in the article's language.
2. Label every item (rules below).
3. Write the answers to `out/` under the batch's own name, e.g. `out/batch-003.json`: a JSON array with
   exactly one object per input item, in any order:

   ```json
   {"key": "movie:12345",
    "primary_genre": "Drama",
    "subgenres": [{"label": "Coming-of-Age", "confidence": 0.85}],
    "moods": [{"label": "Tearjerker", "confidence": 0.6}]}
   ```

4. **Only when your task allows web search**, also write `out/batch-003.sources.json`: one entry per input
   key, listing the pages you read for that title, e.g.
   `{"movie:12345": ["https://en.wikipedia.org/wiki/...", "https://www.rogerebert.com/reviews/..."]}`.
   Every key needs at least one `http(s)` URL; a title you labelled without searching lists its
   `articleUrl`.
5. Check it: `python3 validate.py out/batch-003.json` (add `--web` when your task allows web search), run
   from this directory. Fix anything it reports and **run it again until it prints `ok`**.
6. Write nothing else: no other files, no edits to the input, no notes inside the JSON.

## Rules

- You are a film/TV cataloguer. Assign labels **only** from the controlled vocabulary below. Never invent
  labels, never rename them.
- **Use what you know about the title and, when your task allows it, the web; otherwise the text.** The
  premise is always there. What you reliably know about the work (its reception, its makers, how it is
  usually described) counts. When you do not recognise the title, label it from the premise alone and do
  not guess from the title or year.
- Pick the single **dominant** primary genre: what the work fundamentally is, not its setting or one
  ingredient.
- Be specific; **omit weak guesses** (confidence below 0.5). An empty `subgenres` or `moods` list is a valid
  answer when nothing clearly fits.
- At most 3 subgenres (blended subgenres and themes together) and at most 3 moods, strongest first.
- **Animation is a medium, not a genre.** It is not in the vocabulary. For an animated work choose its story
  genre (Family, Adventure, Comedy, Action, Drama, Fantasy, Science Fiction); children's animation is Family.
- **Cult, Anime, Art House, Epic** depend on knowing the work, not its plot. Assign them only when you
  recognise the work as one, or the text or a page you read establishes it (a cult following; Japanese
  animation; an art-house or festival release; an epic of vast scale). From the plot alone, omit them.
- Moods that describe reception rather than plot (Comfort-watch, Bingeable, Cozy, Visually-stunning) need
  evidence: your knowledge of how the work is received, reviews found on the web when allowed, or the text.
  Never assign them by default.
- Each definition below says what a label means and, for the commonly over-used ones, what it does **not**
  mean. Follow the definitions over your own sense of the word.

## Confidence

A number from 0 to 1 for how clearly the label applies, on this scale:

- 0.85–0.95: a headline label; the work is plainly this.
- 0.6–0.8: clearly applies, but secondary.
- 0.5: minor but present.
- below 0.5: leave the label out.

Labels are kept downstream by fixed cut-offs on this scale (0.55 blended subgenres, 0.50 themes, 0.55
moods), so use it honestly rather than rating everything high.

## Vocabulary

{vocabulary}
