#!/usr/bin/env python3
"""Refuse to publish an alias that is really another title's name.

`titles.aliases` comes from Wikidata's `skos:altLabel` (`lib/wikidata_facts.py`), and it is what lets
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
  3. **Names a different title.** Every one of these needs a `keep` or `drop` decision. One with no
     decision REFUSES the publish, the way `check-producers.py` refuses an artifact with no producer: new
     junk surfaces on the next rebuild instead of shipping quietly.

The buckets and the folding are `store/aliases.py`, shared with the store build, which is where the
decisions are applied: it removes every `drop` alias and records, as `aliasDecisions` in the manifest, the
decisions file's hash and how many colliding aliases it shipped undecided. `--gate` is the publisher's half
(`publish-dataset.sh` runs it): it refuses a manifest with no such record, one built from other decisions
than the committed file, and one with an undecided collision.

Given a facts file instead, it lists the collisions and writes the undecided ones to `--review` for a
person. `--tmdb` sorts that file by whether TMDB has ever recorded the alias as an alternative title, which
is the difference between skimming a few hundred rows and reading them.

## On TMDB

The repo ships no TMDB Content, and this keeps that guarantee structurally rather than by care: the only
thing taken from a TMDB response is a BOOLEAN — did it list this alias — and the only thing written to disk
is that boolean. No TMDB string reaches the review file, the cache, or the dataset. The alias text in every
output comes from Wikidata.

    scripts/check-alias-collisions.py out-facts-full/facts-<hash>.json
    scripts/check-alias-collisions.py <facts.json> --tmdb --review out/alias-review.json
    scripts/check-alias-collisions.py --gate out/dataset.meta.json
"""
import argparse
import hashlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from store.aliases import (DECISIONS, collisions, decision_key, hard_fold,  # noqa: E402
                           load_decisions, own_names)


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


def gate(meta_path, decisions_path):
    """The publisher's check, on the `aliasDecisions` record the store build stamped. 0 passes, 1 refuses.

    Read off the manifest rather than recomputed from a facts file, because a publish dir may hold only
    the store and the manifest — and because the question is what the STORE ships, which only the build
    that wrote it saw.
    """
    with open(meta_path, encoding="utf-8") as fh:
        meta = json.load(fh)
    decisions = os.path.relpath(decisions_path)
    rebuild = f"pipeline/build_store.py … --stamp-meta {meta_path}"
    record = meta.get("aliasDecisions")
    if not (isinstance(record, dict) and isinstance(record.get("undecided"), int)
            and isinstance(record.get("sha256"), str)):
        print(f"error: the manifest records no aliasDecisions, so nothing says the store applied "
              f"{decisions} or how many of its aliases name another title undecided. Rebuild the store "
              f"with {rebuild}, which applies the decisions and records them.", file=sys.stderr)
        return 1

    with open(decisions_path, "rb") as fh:
        committed = hashlib.sha256(fh.read()).hexdigest()
    if record["sha256"] != committed:
        print(f"error: the store applied a different {decisions} ({record['sha256'][:12]}…) from the one in "
              f"this tree ({committed[:12]}…), so a decision made since it was built — a drop above all — "
              f"is not in what would ship. Rebuild the store with {rebuild}.", file=sys.stderr)
        return 1

    if record["undecided"]:
        facts = next((e.get("path") for e in meta.get("storeInputs") or [] if e.get("arg") == "facts"),
                     "<the store's facts-<version>.json>")
        print(f"error: the store ships {record['undecided']} alias(es) that are another title's name, with no "
              f"keep/drop decision in {decisions}.\n"
              f"       atlas ranks an exact name hit above everything else, so an alias that is really another\n"
              f"       title's name puts this title at the top of that search: Taxi Driver carried \"Alien\"\n"
              f"       and ranked second for it. To decide them:\n"
              f"         1. list them:\n"
              f"              scripts/check-alias-collisions.py {facts} --review alias-review.json\n"
              f"            (--tmdb sorts the list by whether TMDB records the alias; needs TMDB_API_KEY)\n"
              f"         2. add each one to \"keep\" or \"drop\" in {decisions}: mediaType, tmdbId, alias,\n"
              f"            and the evidence as \"why\". Keep a real release or translated title; drop only an\n"
              f"            alias that names a different work.\n"
              f"         3. rebuild the store, which removes the drops and records the count again:\n"
              f"              {rebuild}", file=sys.stderr)
        return 1

    print(f"alias gate: the store applied {decisions} ({record['dropped']} dropped) and ships no undecided "
          f"collision")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("facts", nargs="?")
    ap.add_argument("--gate", metavar="META",
                    help="refuse this manifest's store unless it applied the committed decisions and ships "
                         "no undecided collision")
    ap.add_argument("--decisions", default=DECISIONS)
    ap.add_argument("--review", help="write the undecided residue here, for a human to judge")
    ap.add_argument("--tmdb", action="store_true", help="sort the review by TMDB corroboration")
    ap.add_argument("--cache", help="corroboration cache (booleans only); default beside --review")
    args = ap.parse_args()

    if args.gate:
        return gate(args.gate, args.decisions)
    if not args.facts:
        ap.error("give a facts file to review, or --gate <dataset.meta.json>")

    with open(args.facts, encoding="utf-8") as fh:
        records = json.load(fh)["records"]
    decided, _ = load_decisions(args.decisions)

    counts, colliding = collisions(
        (r.get("mediaType"), r.get("tmdbId"), r.get("titles") or {}) for r in records)
    residue = [{
        "mediaType": media, "tmdbId": tmdb_id,
        "title": (own_names(titles) or [None])[0],
        "alias": alias,
        "claimedBy": [{"mediaType": m, "tmdbId": i} for m, i in owners[:4]],
    } for media, tmdb_id, titles, alias, owners in colliding
        if decision_key(media, tmdb_id, alias) not in decided]

    for name in sorted(counts):
        print(f"{counts[name]:>7}  {name}")
    print(f"{len(residue):>7}  UNDECIDED — need a keep/drop in {os.path.relpath(args.decisions)}")

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
