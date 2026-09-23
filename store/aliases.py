"""Alias decisions: the keep/drop judgements in `data/alias-decisions.json`, applied where aliases become
`alias_titles`.

`titles.aliases` is Wikidata's `skos:altLabel`, and anyone may add one. atlas scores an exact hit on any
name a title carries as the strongest signal it has, so an alias that is really another title's name puts
that title at the top of a search for it: Taxi Driver carries "Alien" and ranked second for `q=alien`.
Whether such an alias is a real release title or junk is a fact about the world rather than the data, so a
person decides each one — `pipeline/check_alias_collisions.py` explains why no rule can, and writes the
undecided ones out for review.

The store build is where the decisions take effect. Every `drop` alias is removed from the corpus rows
before anything is interned, so it reaches neither `alias_titles` nor the card's name fallback. The build
also counts the colliding aliases nobody has decided, and `--stamp-meta` records that count with the
decisions file's hash as `aliasDecisions` — which `check_alias_collisions.py --gate` holds a publish to.

Applying them to a facts file does not hold. The drops were once applied that way by hand and the facts
since have not carried them, but Wikidata still carries "Alien" on Taxi Driver, so a scrape into a fresh
out-dir would have shipped it again with nothing to say so.
"""
import hashlib
import json
import os
import re
import unicodedata
from collections import defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DECISIONS = os.path.join(REPO, "data", "alias-decisions.json")

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
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    index = {}
    for verdict in ("keep", "drop"):
        for row in raw.get(verdict, []):
            index[decision_key(row["mediaType"], row["tmdbId"], row["alias"])] = verdict
    return index, raw


def collisions(titled):
    """Every alias sorted into its bucket. `titled` is `(mediaType, tmdbId, titles)` per title.

    Returns `(counts, colliding)`. Only the last bucket is a question for a person, and `colliding` holds
    it: `(mediaType, tmdbId, titles, alias, owners)` for each alias that is some OTHER title's own name.

      1. same as one of the title's own names after hard folding — punctuation, accents, leading articles,
         format suffixes ("Beauty & the Beast" / "Beauty and the Beast", "Bait" / "Bait 3D")
      2. a substring of its own title, or the other way round ("Dead Reckoning" on "Mission: Impossible –
         Dead Reckoning Part One")
      3. a name nothing else claims
      4. names a different title
    """
    titled = list(titled)
    canonical = defaultdict(set)
    for media, tmdb_id, titles in titled:
        for name in own_names(titles):
            canonical[hard_fold(name)].add((media, tmdb_id))

    counts = defaultdict(int)
    colliding = []
    for media, tmdb_id, titles in titled:
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
            colliding.append((media, tmdb_id, titles, alias, owners))
    return counts, colliding


def apply(rows, path=DECISIONS):
    """Remove every `drop` alias from the corpus rows, in place, and return the `aliasDecisions` record.

    The record is the decisions file's sha256, how many aliases were dropped, and how many colliding
    aliases have no decision. The undecided count is taken BEFORE the drops come out, over every row the
    store is built from, so it is the number the publisher has to see as zero.
    """
    decided, _ = load_decisions(path)
    titled = []
    for row in rows:
        media, _, tmdb_id = row["key"].partition(":")
        titled.append((media, int(tmdb_id), (row.get("facts") or {}).get("titles") or {}))

    _, colliding = collisions(titled)
    undecided = sum(1 for media, tmdb_id, _, alias, _ in colliding
                    if decision_key(media, tmdb_id, alias) not in decided)

    drops = {key for key, verdict in decided.items() if verdict == "drop"}
    dropped = 0
    for media, tmdb_id, titles in titled:
        aliases = titles.get("aliases")
        if not aliases:
            continue
        kept = [a for a in aliases if decision_key(media, tmdb_id, a) not in drops]
        if len(kept) != len(aliases):
            dropped += len(aliases) - len(kept)
            titles["aliases"] = kept

    with open(path, "rb") as fh:
        sha = hashlib.sha256(fh.read()).hexdigest()
    return {"sha256": sha, "dropped": dropped, "undecided": undecided}
