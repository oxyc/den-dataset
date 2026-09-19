#!/usr/bin/env python3
"""Check a premise-generation batch against its input, and reject the whole batch on corruption.

  scripts/v2/validate_premise_batch.py --phase out-premise-v2/gen [--batch 0000] [--strict-language]

A generating model can return a correct row COUNT while inventing ids, duplicating one key and dropping
another — `README.md` records exactly that: ids fabricated in 3 of 12 batches and a key duplicated in a 4th,
"reporting success every time". So a count is not evidence, and the only check worth running compares the
answer's key set against the input's.

Rejection is per batch, not per row. A batch that invented one id is a batch whose other rows were produced
by the same confused pass, and keeping the survivors silently mixes verified and unverified work into an
append-only artifact.

## The language check

`data/premise-tags-v1.SPEC.md` requires English tags whatever the plot's language, because this index is one
shared vocabulary — a tag nobody else can emit matches nothing. That rule is new, and 79% of the current
worklist is non-English (de 20%, it 18%, fr 16%), so it is the rule most likely to be ignored and the one
whose failure is invisible in a row count.

Detection is deliberately a heuristic and deliberately loud: non-ASCII letters are a certainty, and beyond
that we flag tags carrying diacritics or characteristic non-English function words. It cannot catch a
plausible-looking German compound rendered in ASCII, so `--strict-language` exists to fail on any hit rather
than warn, and a human should read a sample of non-English titles' output regardless.
"""
import argparse
import json
import os
import re
import sys
import unicodedata

TAG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MIN_TAGS, MAX_TAGS = 8, 12
# Function words that would only appear inside a tag left in its source language.
#
# Deliberately EXCLUDES every foreign function word that is also an ordinary English word, because those
# produce false rejections on correct tags: `con` (con-artists-lovers), `die` (die-hard), `van`, `per`,
# `la`, `il`, `de`, `lo`, `los`, `el`, `du`, `le`, `col`, `im`. The first smoke batch rejected
# `con-artists-lovers` on exactly this, which is a worse failure than a missed tag — a validator that cries
# wolf gets switched off, and then it catches nothing.
#
# What remains cannot be an English word, so a hit is real. It will still miss a German compound rendered in
# plausible ASCII with no function word in it, which is why `--strict-language` is a gate on the run rather
# than the whole check: read a sample of non-English output by hand as well.
FOREIGN_HINT = re.compile(
    r"(?:^|-)(?:der|das|und|mit|eine[nrsm]?|von|zum|zur|auf|für|nach|über|durch"
    r"|les|une|dans|pour|avec|sur|une"
    # `como` is gone for the same reason `con` is: Lake Como is a place, and `lake-como-seduction` is a
    # correct English tag. A hint that fires on a real word is worse than a missing hint. `las` went the
    # same way: across 346,130 tags its only hits were `las-vegas-underworld` and `las-vegas-showdown`
    # — a place name carried into English, 3 false rejections and no true ones.
    r"|una|del|por|para"
    r"|gli|dei|nel|della|degli"
    r"|het|een|voor|naar)(?:-|$)")


# Genre and mood words the spec bans outright. `data/README.md` credits that ban, together with the ban on
# proper nouns, with why this index beats the plot index by +11.3 pp: the tags describe STRUCTURE, and a
# genre word is the one thing the plot index already encodes better. A tag like `religious-horror` spends a
# slot re-stating what the taxonomy labels already say.
GENRE_WORDS = {
    "comedy", "comedies", "comic", "horror", "thriller", "thrillers", "romance", "romantic", "drama",
    "dramatic", "biography", "biopic", "documentary", "musical", "western", "noir", "satire", "satirical",
    "fantasy", "scifi", "mystery", "action", "adventure", "slapstick", "scary",
    "heartwarming", "dark", "gritty", "funny", "sad", "uplifting", "tense",
    # Tokens, not tags: membership is tested after splitting on "-", so a hyphenated genre name has to be
    # listed by its parts. `comedy-adventure-sci-fi` slipped through as "not entirely genre words" because
    # `sci` and `fi` were absent while the joined `sci-fi` was present.
    "sci", "fi", "feel", "good",
}


# Vocabulary a generator reaches for when it gives up on a work and describes the JOB instead of the story:
# `placeholder-content`, `missing-plot`, `insufficient-data`, `unknown-series`, `unprocessed-work`. Batch
# 0258 returned 13 of these while every one of those works had a plot — 399 to 25,237 characters of it — and
# reported success. Nothing else caught it: the row count was right, the keys were right, the tags were valid
# kebab-case. Only the tags' MEANING was fabricated, which is why this is fatal rather than a tag to drop: a
# pass that invented one of these invented whatever else it could not be bothered to read.
META_WORDS = {
    "placeholder", "insufficient", "unprocessed", "unavailable", "untagged", "unknown", "missing",
    "todo", "tbd", "none", "null", "na", "error", "failed",
    # Only ever meta in combination with the above — `data`, `plot` and `content` never stand alone as a
    # premise, and a tag built entirely from this set describes the pipeline, not a film.
    "data", "plot", "content", "work", "series", "film", "movie", "entry", "record",
    # `premise` earns its place the same way: `premise-unknown` is filler, while the 59 shipped tags that
    # contain one of these words (`reality-show-premise`, `best-friend-tags-along`) survive because the
    # test is whether the WHOLE tag is meta, not whether it contains a meta word.
    "premise", "description", "summary", "generic", "undetermined", "unspecified",
}


def meta_tag(tag):
    """True when a tag is built ENTIRELY from process/data vocabulary, so it describes no story.

    Entirely, for the same reason `genre_words` tests entirely: `missing-child` and `unknown-father` are real
    premises. Measured against the shipped 316,355-tag corpus, this flags 0.
    """
    tokens = set(tag.split("-"))
    return bool(tokens) and tokens <= META_WORDS


def genre_words(tag):
    """Flag a tag that is ENTIRELY genre/mood words — never one that merely contains one.

    Measured against the shipped corpus, which is the only evidence that matters here: 5.4% of its 316,355
    tags contain a genre word (`doomed-romance`, `found-footage-horror`, `class-divide-romance`), and that
    corpus is the one that beat the plot index 12/12. Those are structural tags where the genre word
    qualifies a shape. Rejecting them would reject the thing that works.

    Tags that are nothing BUT genre words are 47 of 316,355 — 0.01% — and they are the real violation:
    `documentary`, `slapstick-comedy`, `noir-comedy-thriller`. That is what the spec means and all this
    should catch.
    """
    tokens = set(tag.split("-"))
    return sorted(tokens) if tokens <= GENRE_WORDS else []


def capitalises_all_nouns(plot):
    """German (and Luxembourgish) capitalise every noun, which defeats the check below entirely.

    Detected from the text rather than declared, so it needs no language field and covers any language with
    the same property. The first smoke batch flagged `idol` and `talent` as proper nouns in a German plot
    for exactly this reason — a check that is wrong for a fifth of the corpus is worse than no check.
    """
    mid = re.findall(r"(?<![.!?]\s)(?<!^)\b([A-Za-zÀ-ÿ]{3,})\b", plot, re.M)
    if len(mid) < 40:
        return True          # too little text to judge; decline to guess rather than accuse
    return sum(w[:1].isupper() for w in mid) / len(mid) > 0.15


ORDINARY_MIN = 100
_ordinary = None


def ordinary_vocabulary(path=None):
    """Tokens common enough in the SHIPPED tags to be ordinary premise vocabulary, never a name.

    Derived from the corpus rather than written by hand, so it needs no maintenance and reflects what this
    index actually says. The capitalisation heuristic below cannot tell `Giant` in "Giant God Warrior" or
    `Time` in "Time Shift" from a real name — both appear only capitalised — and it flagged
    `giant-creature-invasion` and `time-spanning-love` on exactly that. But `giant` is in 341 shipped tags
    and `time` in 1,618, while `versailles` is in one and `bogota` in none.

    The threshold separates them: at 100, `time` and `giant` pass while `vegas` (25) and `paris` (31) are
    still checked — and those two ARE proper nouns that leaked into the shipped set.
    """
    global _ordinary
    if _ordinary is None:
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        tags = json.load(open(path or os.path.join(root, "data/premise-tags-v1.json"),
                              encoding="utf-8"))["tags"]
        counts = {}
        for row in tags.values():
            for tag in row:
                for token in set(tag.split("-")):
                    counts[token] = counts.get(token, 0) + 1
        _ordinary = {t for t, n in counts.items() if n >= ORDINARY_MIN}
    return _ordinary


def proper_nouns(tag, plot):
    """Removed. Kept as a stub so the reasoning is not lost and nobody rebuilds it.

    The idea was to flag tokens appearing in the plot only capitalised mid-sentence. Measured against real
    batches it produced EIGHT false positives and ZERO true positives: `giant` and `time` (capitalised
    inside "Giant God Warrior" and "Time Shift"), `sun`, `moon`, `archive`, `oni`, and — before the
    German guard — `idol` and `talent`.

    Three rounds of patching did not fix it, because the premise is wrong. A premise tag is built from
    ordinary words, and ordinary words appear capitalised inside longer proper names constantly. The signal
    does not separate the two, and a check that is wrong every time it fires costs good tags and teaches
    everyone to ignore the report.

    The spec still bans proper nouns and the generator largely complies. The residual rate is tolerable on
    the evidence that matters: the shipped corpus that beat the plot index 12/12 contains `las-vegas-showdown`
    and 31 tags with `paris`, and it works. If this is ever worth catching, it needs a gazetteer or a named
    entity model, not a capitalisation rule.
    """
    return []


def normalise(tag):
    """The tag this one obviously meant: lowercased, accents transliterated, apostrophes dropped.

    Most format failures are mechanical. Across 61 batches, 21 of 38 findings were `göring-collection`,
    `ménage-à-trois-tension`, `societal-collapse-London` — correct premise tags carrying an accent or a
    capital, which is unsurprising when 79% of the source plots are not in English. Rejecting a batch of 22
    titles to fix one character spends a re-run to buy nothing, so a tag that normalises to a valid one is
    repaired and reported rather than condemned.

    A tag that does NOT survive this — a space, a slash, an empty string — is still fatal, because then the
    generator produced something that was never a tag.
    """
    flat = unicodedata.normalize("NFD", tag)
    flat = "".join(c for c in flat if not unicodedata.combining(c))
    return flat.lower().replace("'", "").replace("’", "")


def has_non_ascii(tag):
    return any(ord(c) > 127 for c in tag)


def has_diacritic(tag):
    return any(unicodedata.combining(c) for c in unicodedata.normalize("NFD", tag))


def check(batch_in, batch_out, strict_language):
    """Returns (fatal, quality). Fatal rejects the batch; quality names tags to drop and keep going.

    The split matters and I got it wrong first. CORRUPTION — an invented id, a dropped key, a duplicate, a
    malformed answer — condemns the whole batch, because the rows around it came from the same confused pass
    and keeping the survivors mixes verified with unverified work.

    A bad TAG is not that. The first real batch carried two tags that were nothing but genre words out of
    176; re-running 22 titles to fix 2 tags spends tokens to buy nothing, and at any realistic rate a
    whole-batch rule would reject almost every batch, which is how a gate gets switched off. Those tags are
    reported and dropped; the title keeps its remaining tags, and falls into the quality report if that
    leaves it under the floor.
    """
    fatal, quality = [], []
    if not isinstance(batch_out, list):
        return ["output is not a JSON array"], []

    want = [r["key"] for r in batch_in]
    got = [r.get("key") for r in batch_out if isinstance(r, dict)]
    if len(got) != len(batch_out):
        fatal.append("some rows are not objects")
    if len(set(got)) != len(got):
        dupes = sorted({k for k in got if got.count(k) > 1})
        fatal.append(f"duplicated keys: {dupes}")
    invented = sorted(set(got) - set(want))
    missing = sorted(set(want) - set(got))
    if invented:
        fatal.append(f"invented keys not in the input: {invented}")
    if missing:
        fatal.append(f"keys dropped from the input: {missing}")

    plots = {r["key"]: r.get("plot", "") for r in batch_in}
    for row in batch_out:
        if not isinstance(row, dict):
            continue
        key, tags = row.get("key"), row.get("tags")
        if not isinstance(tags, list) or not tags:
            fatal.append(f"{key}: no tags")
            continue
        if not MIN_TAGS <= len(tags) <= MAX_TAGS:
            quality.append(f"{key}: {len(tags)} tags, spec says {MIN_TAGS}-{MAX_TAGS}")
        # Padding to the floor by repeating one tag is how a generator fakes the count when it has stopped
        # reading: `['superhero-origin', 'power-awakening', 'mentor-conflict', 'premise-unknown' x5]`.
        # It catches that whatever filler word is chosen, so it does not depend on META_WORDS being complete.
        # 1 of 37,533 shipped works repeats a tag; 15 of 5,897 in this run did, and all 15 were padding.
        if len(set(tags)) != len(tags):
            repeats = sorted({t for t in tags if isinstance(t, str) and tags.count(t) > 1})
            fatal.append(f"{key}: repeated tag(s) {repeats} — the list was padded, not generated")
        for tag in tags:
            if not isinstance(tag, str) or not tag.strip():
                fatal.append(f"{key}: empty tag")
                continue
            if not TAG.match(tag) or has_non_ascii(tag) or has_diacritic(tag):
                # Repairable, or genuinely not a tag? A capital or an accent is a typo with an obvious
                # correction; a space or a slash means the generator produced something else entirely.
                fixed = normalise(tag)
                if TAG.match(fixed) and not has_non_ascii(fixed):
                    quality.append(f"{key}: {tag!r} -> {fixed!r} (normalise)")
                else:
                    fatal.append(f"{key}: {tag!r} is not a tag and does not normalise to one")
                continue
            if meta_tag(tag):
                fatal.append(f"{key}: {tag!r} describes the pipeline, not the story "
                             f"(plot was {len(plots.get(key, ''))} chars)")
            elif FOREIGN_HINT.search(tag):
                problem = f"{key}: {tag!r} looks like it was left in the source language"
                fatal.append(problem) if strict_language else quality.append(problem)
            else:
                if words := genre_words(tag):
                    quality.append(f"{key}: {tag!r} is only genre/mood words {words} — drop the tag")
                if names := proper_nouns(tag, plots.get(key, "")):
                    quality.append(f"{key}: {tag!r} carries proper noun(s) {names} — drop the tag")
    return fatal, quality


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, help="the gen/ directory holding in/ and out/")
    ap.add_argument("--batch", help="one batch number, e.g. 0000 (default: every batch with an output)")
    ap.add_argument("--strict-language", action="store_true",
                    help="fail on a suspected source-language tag rather than warning")
    args = ap.parse_args()

    in_dir, out_dir = os.path.join(args.phase, "in"), os.path.join(args.phase, "out")
    names = ([f"batch-{args.batch}.json"] if args.batch
             else sorted(n for n in os.listdir(out_dir) if n.startswith("batch-")))

    rejected, quality, ok, rows = [], [], 0, 0
    for name in names:
        out_path = os.path.join(out_dir, name)
        if not os.path.exists(out_path):
            rejected.append((name, ["no output written"]))
            continue
        try:
            batch_out = json.load(open(out_path, encoding="utf-8"))
        except Exception as exc:
            rejected.append((name, [f"unparseable: {exc}"]))
            continue
        batch_in = json.load(open(os.path.join(in_dir, name), encoding="utf-8"))
        problems, notes = check(batch_in, batch_out, args.strict_language)
        if notes:
            quality.append((name, notes))
        if problems:
            rejected.append((name, problems))
        else:
            ok += 1
            rows += len(batch_out)

    for name, notes in quality:
        print(f"notes {name}  ({len(notes)} tag(s) to drop; the batch is still usable)")
        for n in notes[:4]:
            print(f"    {n}")
        if len(notes) > 4:
            print(f"    … and {len(notes) - 4} more")
    for name, problems in rejected:
        print(f"REJECT {name}")
        for p in problems[:6]:
            print(f"    {p}")
        if len(problems) > 6:
            print(f"    … and {len(problems) - 6} more")

    print(json.dumps({"checked": len(names), "accepted": ok, "rejected": len(rejected), "rowsAccepted": rows,
                      "batchesWithTagNotes": len(quality),
                      "tagsToDrop": sum(len(n) for _, n in quality)}))
    sys.exit(1 if rejected else 0)


if __name__ == "__main__":
    main()
