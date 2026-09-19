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
    r"|las|una|del|por|para|como"
    r"|gli|dei|nel|della|degli"
    r"|het|een|voor|naar)(?:-|$)")


# Genre and mood words the spec bans outright. `data/README.md` credits that ban, together with the ban on
# proper nouns, with why this index beats the plot index by +11.3 pp: the tags describe STRUCTURE, and a
# genre word is the one thing the plot index already encodes better. A tag like `religious-horror` spends a
# slot re-stating what the taxonomy labels already say.
GENRE_WORDS = {
    "comedy", "comedies", "comic", "horror", "thriller", "thrillers", "romance", "romantic", "drama",
    "dramatic", "biography", "biopic", "documentary", "musical", "western", "noir", "satire", "satirical",
    "fantasy", "scifi", "sci-fi", "mystery", "action", "adventure", "slapstick", "feel-good", "scary",
    "heartwarming", "dark", "gritty", "funny", "sad", "uplifting", "tense",
}


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


def proper_nouns(tag, plot):
    """Tokens that appear in the plot ONLY capitalised mid-sentence — i.e. names, places, brands.

    Read from the source text rather than a gazetteer, so it works in every language the corpus grounds in
    and needs no list to maintain. A token that also appears lowercase somewhere is an ordinary word that
    merely started a sentence, and is not flagged.
    """
    if capitalises_all_nouns(plot):
        return []
    found = []
    for token in tag.split("-"):
        if len(token) < 3:
            continue
        mid = re.findall(rf"(?<![.!?]\s)(?<!^)\b({re.escape(token)})\b", plot, re.I | re.M)
        if not mid:
            continue
        if all(m[:1].isupper() for m in mid):
            found.append(token)
    return found


def has_non_ascii(tag):
    return any(ord(c) > 127 for c in tag)


def has_diacritic(tag):
    return any(unicodedata.combining(c) for c in unicodedata.normalize("NFD", tag))


def check(batch_in, batch_out, strict_language):
    """Returns a list of problems. Empty means the batch is usable."""
    bad = []
    if not isinstance(batch_out, list):
        return ["output is not a JSON array"]

    want = [r["key"] for r in batch_in]
    got = [r.get("key") for r in batch_out if isinstance(r, dict)]
    if len(got) != len(batch_out):
        bad.append("some rows are not objects")
    if len(set(got)) != len(got):
        dupes = sorted({k for k in got if got.count(k) > 1})
        bad.append(f"duplicated keys: {dupes}")
    invented = sorted(set(got) - set(want))
    missing = sorted(set(want) - set(got))
    if invented:
        bad.append(f"invented keys not in the input: {invented}")
    if missing:
        bad.append(f"keys dropped from the input: {missing}")

    plots = {r["key"]: r.get("plot", "") for r in batch_in}
    for row in batch_out:
        if not isinstance(row, dict):
            continue
        key, tags = row.get("key"), row.get("tags")
        if not isinstance(tags, list) or not tags:
            bad.append(f"{key}: no tags")
            continue
        if not MIN_TAGS <= len(tags) <= MAX_TAGS:
            bad.append(f"{key}: {len(tags)} tags, spec says {MIN_TAGS}-{MAX_TAGS}")
        for tag in tags:
            if not isinstance(tag, str) or not tag.strip():
                bad.append(f"{key}: empty tag")
            elif not TAG.match(tag):
                bad.append(f"{key}: {tag!r} is not lowercase-kebab-case")
            elif has_non_ascii(tag) or has_diacritic(tag):
                bad.append(f"{key}: {tag!r} is not English (non-ASCII)")
            elif FOREIGN_HINT.search(tag):
                problem = f"{key}: {tag!r} looks like it was left in the source language"
                bad.append(problem) if strict_language else print(f"  warn {problem}", file=sys.stderr)
            else:
                if words := genre_words(tag):
                    bad.append(f"{key}: {tag!r} carries banned genre/mood word(s) {words}")
                if names := proper_nouns(tag, plots.get(key, "")):
                    bad.append(f"{key}: {tag!r} carries proper noun(s) {names}")
    return bad


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

    rejected, ok, rows = [], 0, 0
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
        problems = check(batch_in, batch_out, args.strict_language)
        if problems:
            rejected.append((name, problems))
        else:
            ok += 1
            rows += len(batch_out)

    for name, problems in rejected:
        print(f"REJECT {name}")
        for p in problems[:6]:
            print(f"    {p}")
        if len(problems) > 6:
            print(f"    … and {len(problems) - 6} more")

    print(json.dumps({"checked": len(names), "accepted": ok, "rejected": len(rejected), "rowsAccepted": rows}))
    sys.exit(1 if rejected else 0)


if __name__ == "__main__":
    main()
