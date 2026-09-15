#!/usr/bin/env python3
"""Refuse to publish an alias that is really another title's name.

`titles.aliases` comes from Wikidata's `skos:altLabel` (`WikipediaSource.swift:821`), and it is what lets
"parasite" find 기생충 and "spirited away" find 千と千尋の神隠し. It is also unvetted: anyone may add an altLabel,
and a wrong one is not a small error here. atlas takes `t` as a MAX over every name a title carries, and an
exact hit scores `EXACT_TITLE + 0.4·pop` — the strongest signal the ranking has, built so a typed title always
beats a theme. So one bad alias puts any film at the top of that query.

Taxi Driver carries the alias "Alien". It ranked SECOND for `q=alien`, above Aliens, Alien³ and Alienoid.

## Why this needs a person

There is no rule that separates a wrong alias from a right one, and it is worth being clear about that
before anyone tries again:

    'Alien'             on Taxi Driver      — junk
    '96 Hours'          on Taken            — the German release title
    'Avengers Assemble' on The Avengers     — the UK title
    'All That Jazz'     on I Am Jazz        — junk

Structurally identical: an alias on a popular title that is also some other title's canonical name, ranking
above the owner. The difference is whether the film was ever released under that name — a fact about the
world, not about the data. Demoting aliases as a class breaks the first list to fix the second, so the
judgement is recorded in `data/alias-decisions.json` and this script only enforces that one exists.

## What it does

Three buckets, and only the third reaches a human:

  1. **Same after hard folding** — punctuation, accents, leading articles in several languages, and format
     suffixes ("Beauty & the Beast" / "Beauty and the Beast", "Bait" / "Bait 3D"). Not errors.
  2. **Substring of its own title** — "Dead Reckoning" on "Mission: Impossible – Dead Reckoning Part One".
     Not errors either.
  3. **Names a different title.** Every one of these needs a `keep` or `drop` decision. A residue entry with
     no decision FAILS, the way `check-producers.py` fails an artifact with no producer: new junk surfaces on
     the next rebuild instead of shipping quietly.

`--tmdb` sorts the review file by whether TMDB has ever recorded the alias as an alternative title, which is
the difference between skimming a few hundred rows and reading them.

## On TMDB

The repo ships no TMDB Content, and this keeps that guarantee structurally rather than by care: the only
thing taken from a TMDB response is a BOOLEAN — did it list this alias — and the only thing written to disk
is that boolean. No TMDB string reaches the review file, the cache, or the dataset. The alias text in every
output comes from Wikidata.

    scripts/check-alias-collisions.py out-facts-full/facts-<hash>.json
    scripts/check-alias-collisions.py <facts.json> --tmdb --review out/alias-review.json
    scripts/check-alias-collisions.py <facts.json> --apply out/facts-clean.json
"""
import argparse
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DECISIONS = os.path.join(HERE, os.pardir, "data", "alias-decisions.json")

# Format and part markers that make two names of the same thing look different.
SUFFIX = re.compile(
    r"\s*(3d|2d|imax|part\s*(one|two|three|four|i{1,3}|\d+)|chapter\s*\d+|vol(ume)?\.?\s*\d+)$"
)
# Leading articles across the languages the corpus actually carries.
ARTICLES = (
    "the ", "an ", "a ", "le ", "la ", "les ", "el ", "los ", "las ", "il ", "lo ", "gli ",
    "der ", "die ", "das ", "den ", "det ", "en ", "ett ", "o ", "os ", "as ", "de ", "het ",
)


def hard_fold(s):
    """Folded harder than search folds — for telling two spellings of ONE name apart from two names."""
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    for _ in range(3):  # "Bait 3D Part One" needs more than one pass
        before = s
        s = SUFFIX.sub("", s).strip()
        for article in ARTICLES:
            if s.startswith(article):
                s = s[len(article):]
                break
        if s == before:
            break
    return s


def own_names(titles):
    return [titles[k] for k in ("orig", "en") if isinstance(titles.get(k), str) and titles[k].strip()]


def decision_key(media, tmdb_id, alias):
    return f"{media}:{tmdb_id}:{hard_fold(alias)}"


def load_decisions(path):
    if not os.path.exists(path):
        return {}, {"keep": [], "drop": []}
    raw = json.load(open(path, encoding="utf-8"))
    index = {}
    for verdict in ("keep", "drop"):
        for row in raw.get(verdict, []):
            index[decision_key(row["mediaType"], row["tmdbId"], row["alias"])] = verdict
    return index, raw


class TMDBCorroboration:
    """Whether TMDB lists an alias as an alternative title. Only the answer is kept — never TMDB's text."""

    def __init__(self, key, cache_path):
        self.key = key
        self.cache_path = cache_path
        self.cache = {}
        if cache_path and os.path.exists(cache_path):
            try:
                self.cache = json.load(open(cache_path, encoding="utf-8"))
            except ValueError:
                self.cache = {}  # a truncated cache is not worth a failed run
        self.titles = {}

    def _alternatives(self, media, tmdb_id):
        """The folded alternative titles for one id, held in memory for this run only."""
        if (media, tmdb_id) in self.titles:
            return self.titles[(media, tmdb_id)]
        kind = "tv" if media == "tv" else "movie"
        url = (f"https://api.themoviedb.org/3/{kind}/{tmdb_id}/alternative_titles"
               f"?api_key={urllib.parse.quote(self.key)}")
        found = set()
        try:
            with urllib.request.urlopen(url, timeout=20) as fh:
                body = json.load(fh)
            rows = body.get("titles") or body.get("results") or []
            found = {hard_fold(r["title"]) for r in rows if isinstance(r.get("title"), str)}
        except Exception:
            found = None  # unknown, not "absent": an unreachable TMDB must not read as junk
        self.titles[(media, tmdb_id)] = found
        return found

    def says(self, media, tmdb_id, alias):
        key = decision_key(media, tmdb_id, alias)
        if key in self.cache:
            return self.cache[key]
        alternatives = self._alternatives(media, tmdb_id)
        verdict = None if alternatives is None else (hard_fold(alias) in alternatives)
        self.cache[key] = verdict
        time.sleep(0.05)
        return verdict

    def save(self):
        if not self.cache_path:
            return
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
        with open(self.cache_path, "w", encoding="utf-8") as fh:
            json.dump(self.cache, fh, indent=0, sort_keys=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("facts")
    ap.add_argument("--decisions", default=DECISIONS)
    ap.add_argument("--review", help="write the undecided residue here, for a human to judge")
    ap.add_argument("--tmdb", action="store_true", help="sort the review by TMDB corroboration")
    ap.add_argument("--cache", help="corroboration cache (booleans only); default beside --review")
    ap.add_argument("--apply", help="write a facts file with every `drop` alias removed")
    args = ap.parse_args()

    facts = json.load(open(args.facts, encoding="utf-8"))
    records = facts["records"]
    decided, raw_decisions = load_decisions(args.decisions)

    canonical = defaultdict(set)
    for r in records:
        for name in own_names(r.get("titles") or {}):
            canonical[hard_fold(name)].add((r.get("mediaType"), r.get("tmdbId")))

    counts = defaultdict(int)
    residue = []
    for r in records:
        titles = r.get("titles") or {}
        media, tmdb_id = r.get("mediaType"), r.get("tmdbId")
        mine = {hard_fold(n) for n in own_names(titles)}
        for alias in titles.get("aliases") or []:
            if not isinstance(alias, str) or not alias.strip():
                continue
            folded = hard_fold(alias)
            if not folded:
                counts["empty after folding"] += 1
                continue
            if folded in mine:
                counts["artefact: same after hard folding"] += 1
                continue
            if any(folded in n or n in folded for n in mine if n):
                counts["artefact: substring of its own title"] += 1
                continue
            owners = sorted(canonical.get(folded, set()) - {(media, tmdb_id)})
            if not owners:
                counts["a name nothing else claims"] += 1
                continue
            counts["names a different title"] += 1
            verdict = decided.get(decision_key(media, tmdb_id, alias))
            if verdict is None:
                residue.append({
                    "mediaType": media, "tmdbId": tmdb_id,
                    "title": (own_names(titles) or [None])[0],
                    "alias": alias,
                    "claimedBy": [{"mediaType": m, "tmdbId": i} for m, i in owners[:4]],
                })

    for name in sorted(counts):
        print(f"{counts[name]:>7}  {name}")
    print(f"{len(residue):>7}  UNDECIDED — need a keep/drop in {os.path.relpath(args.decisions)}")

    if args.apply:
        drops = {decision_key(d["mediaType"], d["tmdbId"], d["alias"]) for d in raw_decisions.get("drop", [])}
        removed = 0
        for r in records:
            titles = r.get("titles") or {}
            aliases = titles.get("aliases")
            if not aliases:
                continue
            kept = [a for a in aliases
                    if decision_key(r.get("mediaType"), r.get("tmdbId"), a) not in drops]
            removed += len(aliases) - len(kept)
            if kept:
                titles["aliases"] = kept
            else:
                titles.pop("aliases", None)
        with open(args.apply, "w", encoding="utf-8") as fh:
            json.dump(facts, fh, ensure_ascii=False)
        print(f"applied: {removed} aliases dropped -> {args.apply}")

    if residue and args.review:
        if args.tmdb:
            key = os.environ.get("TMDB_API_KEY", "")
            if not key:
                sys.exit("--tmdb needs TMDB_API_KEY (scripts/lib/den-env.sh loads den.env)")
            cache_path = args.cache or os.path.join(os.path.dirname(args.review) or ".",
                                                    "alias-corroboration.cache.json")
            tmdb = TMDBCorroboration(key, cache_path)
            try:
                for row in residue:
                    row["tmdbKnowsThisAlias"] = tmdb.says(row["mediaType"], row["tmdbId"], row["alias"])
            finally:
                tmdb.save()
            # Uncorroborated first: those are where a wrong alias is likeliest to be hiding.
            residue.sort(key=lambda r: ({False: 0, None: 1, True: 2}[r["tmdbKnowsThisAlias"]],
                                        str(r["title"])))
            known = sum(1 for r in residue if r["tmdbKnowsThisAlias"] is True)
            unknown = sum(1 for r in residue if r["tmdbKnowsThisAlias"] is None)
            print(f"         TMDB corroborates {known}, cannot say {unknown}, "
                  f"does not list {len(residue) - known - unknown}")
        with open(args.review, "w", encoding="utf-8") as fh:
            json.dump(residue, fh, indent=2, ensure_ascii=False)
        print(f"         review file: {args.review}")

    return 1 if residue else 0


if __name__ == "__main__":
    sys.exit(main())
