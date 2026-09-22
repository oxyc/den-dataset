#!/usr/bin/env python3
"""Three invariants between the enriched corpus and the shipped labels, reported rather than enforced.

A title with a Wikipedia plot should end up with labels and therefore a vector. When it does not, it has no
More Like This and cannot be reached by any thematic query — and nothing notices. That is how Spirited Away,
One Piece, Bleach, Pokémon, Re:ZERO and Off Campus came to be in the shipped dataset with a plot and no
labels; it surfaced only because someone asked why The Wire had no similar titles.

This is the same shape as the failures `check-producers.py` exists for: an artifact drifting from the thing
it derives from, with nothing positioned to notice.

## The third invariant: a title's plot text must be about that title (#16)

**No two titles may be grounded on the same Wikipedia article.** Measured on the shipped generation: 1,066
titles (2.24% of the 47,529 grounded) are grounded on one of 465 articles that ground more than one title.
Five of them carry the *Wuthering Heights novel*, whose 46,936 characters of `Feminist` / `Class and money` /
`Morality` are literary criticism of an 1847 book rather than the plot of any adaptation.

What is counted is the **borrowers**: 336 of those 1,066 are the article's own subject, grounded correctly,
and are in the census only because someone else took their page. Ratcheting all 1,066 asks them to fix
something they did not do. Both sides are printed; only the borrowing side is counted, and a title whose
provenance is unrecorded counts, because this guard ratchets and must not clear a title it cannot tell
about.

**One Wikidata item behind several TMDB ids is exempt** — 31 titles are in that position (`Don't Hug Me I'm
Scared` 1-5 are a single item; `Carlos` is in TMDB as a movie and a series), so the invariant could never
reach zero with them in it. The identity is `imdbId` (P345), which lives in the facts file, so the
exemption needs `--facts`.

The cause is the source-work fallback in `taxonomy-backfill`. `regroundOnWikipedia` tries the title's own
article, then the Wikidata P144 source work, and keeps the LONGEST:

    let candidates = [facts?.article, facts?.sourceArticle].compactMap { $0 }
    …
    if plot.text.count > (found?.plot.text.count ?? 0) { found = (candidate, plot) }
    if plot.text.count >= ownArticleSufficient { break }          // ownArticleSufficient = 1000

That is right when ONE adaptation maps to one source work — it is what stops Silo falling back to 189
characters of its own article instead of the novel's 12,415. It fails when MANY adaptations map to the same
source: a 46,936-character novel beats every adaptation's own article, so all of them inherit it, and
longest-wins guarantees it. The consequence is measurable downstream — 63% of shared-article groups agree on
`primaryGenre` against 12% for a size-matched random control, and all five novel-grounded Wuthering Heights
titles are labelled `ending=happy` (the novel's) while `movie:3084`, the only one grounded on its own film
article, is correctly `ending=tragic`.

### The per-title check the census is a proxy for

Provenance recovered for all 47,529 grounded titles says the collision census sees **35% of the defect**:
2,075 titles are grounded on something that is not about them — 1,975 through the P144 source work and 100
through a sitelink that redirected into another page — and only 729 of those collide with a second title.
A title grounded on a novel that grounds nothing else is invisible to a collision census *by construction*:
Silo is on the novel, alone, and reads as clean.

So the enrich pass now RECORDS what it decided, in `plotArticleRole` (`own` / `own-other-language` /
`source-work`) and `plotArticleRedirected`, and `plot_provenance` reads it back per title. No fetch, no LLM
and no collision needed — the pass already knew both facts and used to throw them away.

Absent means the row was enriched before the pass recorded this, which is UNKNOWN. It is reported as such
and counted as neither clean nor mis-grounded; every row in the shipped generation is in that state, so
this census reads 0 with 47,529 unknown until a re-enrich. Reading a missing role as `own` would report the
whole corpus as correctly grounded on the strength of a field nothing wrote.

### Why the ARTICLE and not the text

#16 measured this as byte-identical plot text and found 940 titles, noting that "940 is a floor" because two
titles grounded on the same article at different revisions differ by an edit and are not counted. Checking
`(plotLanguage, plotArticle)` instead raises that floor to 1,066 at no cost: it is the recorded decision
rather than a symptom of it, it survives a revision bump, a heading-rule change or a translation, and it
needs no text comparison at all.

Deliberately NOT checked: near-duplicate text (shingling/minhash). It would cost a quadratic-ish pass over
47k plots to rediscover what the `plotArticle` field already states exactly, and two titles grounded on
genuinely different articles with near-identical prose are a Wikipedia mirror, not this bug. Also not
checked: whether a given sharing is LEGITIMATE — two cuts of one film, or a series and its franchise page,
may share an article defensibly. Adjudicating that needs the Jev `validity` choice, which does not exist at
enrich time. This counts and ratchets; it does not judge.

## Why it WARNS on the standing count and REFUSES on a regression

The first two invariants are violated today — 6 and 0 under the newest-wins reading, 3 and 1 under the
lexical one the readers used before — and so is the third, 1,066 times. A gate that refuses the next publish
on a pre-existing violation gets switched off, so the standing count reports and returns 0. Turn it into a
gate once the counts are zero.

`--shared-plot-baseline` is the part that bites now. It is the previous publish's count, and any INCREASE
exits 2 — the record-count guard's shape, which refuses a regression against the published manifest rather
than an absolute level. That is what makes this an invariant rather than a census: the standing 1,066 is
printed on every publish so it cannot stay invisible, and the number can only go down. It matters because
the one thing that makes this worse is a re-run — oxyc/den-dataset#27 proposes a daily incremental job that
re-grounds what moved — and a scheduled pass that quietly doubles the count is exactly what nothing is
positioned to notice today.

## Why `--enriched-dir` is explicit, and `--max-batch-id` exists

There is no record of which enriched batches a published dataset was built from: `out-t02-cc0b/enriched` is a
SYMLINK to `../out-t02/enriched`, so a publish reads a live directory that keeps growing. Enriching after
embedding is the NORMAL order — the embed pass ran at 18:28 and batches 174-176 were written at 22:26-01:22 —
so comparing today's enriched tree against an older labels blob reports every one of those as a violation.
`--max-batch-id` bounds the comparison to the batches a publish actually saw; `manifest-counts.py --stamp`
records it as `maxBatchId`.

    scripts/check-plot-invariants.py --enriched-dir out-t02/enriched --labels out-publish/labels-t02.json
    scripts/check-plot-invariants.py ... --max-batch-id 177 --facts out-publish/facts-<ver>.json
    scripts/check-plot-invariants.py ... --shared-plot-baseline 1066 --stamp-meta out/dataset.meta.json

`--facts` does two things at once, which is worth knowing before adding it to a publish. It SCOPES the
census to the shipped keys — 8,949 of the 47,529 grounded titles never shipped — and it supplies the
`imdbId` the one-item exemption needs. Measured on the current generation: 1,066 with neither, 1,043 with
the exemption alone at full scope, 407 with both. The publish gate does not pass it today, so its baseline
is the unscoped, unexempted count.

Exit codes: 0 fine (or a standing violation, warned) · 1 `--fail` and a standing violation · 2 the
shared-article count REGRESSED against `--shared-plot-baseline`.
"""
import argparse
import json
import os
import sys


def batch_files(enriched_dir, max_batch_id=None):
    """Batch files in BATCH-NUMBER order, oldest first — the order `EnrichedBatches.orderedNames` defines.

    Not `sorted()`: that is lexicographic, so `batch-99.json` comes after `batch-177.json` and the winner
    among duplicate keys depends on how many digits an id has.
    """
    out = []
    for name in os.listdir(enriched_dir):
        if not (name.startswith("batch-") and name.endswith(".json")):
            continue
        try:
            n = int(name[len("batch-"):-len(".json")])
        except ValueError:
            continue
        if max_batch_id is not None and n > max_batch_id:
            continue
        out.append((n, name))
    return [name for _, name in sorted(out)]


ROLES = ("own", "own-other-language", "source-work")


def plot_provenance(grounded):
    """Per title, what the enrich pass recorded about WHICH article it took the plot from.

    `plotArticleRole` says which candidate won — the title's own article, its article on another
    Wikipedia, or the Wikidata P144 source work — and `plotArticleRedirected` says whether the fetch
    landed somewhere other than the page it asked for. Together they answer "is this text about this
    title?" directly, where the collision census can only answer "did two titles land on one page".

    Returns `misgrounded` (the text describes a different work), `unrecorded` (the row predates the
    field, so nothing is known) and a count per role.

    ABSENT IS UNKNOWN, NOT CLEAN. Every row in the shipped generation lacks both fields, and reading a
    missing role as `own` would report 47,529 titles as correctly grounded on the strength of a field
    nothing ever wrote. They are listed separately and counted as neither.
    """
    misgrounded, unrecorded, roles = [], [], {}
    for key, rec in sorted(grounded.items()):
        role = rec.get("plotArticleRole")
        if role not in ROLES:
            unrecorded.append(key)
            continue
        roles[role] = roles.get(role, 0) + 1
        # A P144 source work is a novel or manga — a different work telling a related story. A redirect
        # moved the fetch off the page the sitelink named, which lands in a parent or sibling work
        # (`Jarhead 2: Field of Fire` → `Jarhead (film)`) and is untouched by any change to the
        # source-work fall-through.
        if role == "source-work" or rec.get("plotArticleRedirected") is True:
            misgrounded.append(key)
    return {"misgrounded": misgrounded, "unrecorded": unrecorded, "roles": roles}


def report_provenance(census, grounded, limit=10):
    """Print the per-title census — the 2,075, not the 729 of them that happen to collide."""
    denom = len(grounded) or 1
    found = census["misgrounded"]
    print(f"  grounded on another work's article (recorded at enrich) : {len(found)} "
          f"({100 * len(found) / denom:.2f}% of {denom} grounded)")
    if census["roles"]:
        print("      by winning candidate: "
              + ", ".join(f"{role} {census['roles'][role]}" for role in ROLES if role in census["roles"]))
    for key in found[:limit]:
        rec = grounded[key]
        why = "redirected" if rec.get("plotArticleRedirected") is True else rec.get("plotArticleRole")
        print(f"      {key:16s} {why:12s} {rec.get('plotArticle')!r}")
    if len(found) > limit:
        print(f"      … and {len(found) - limit} more")
    if census["unrecorded"]:
        print(f"  no recorded provenance : {len(census['unrecorded'])} — enriched before the pass recorded "
              "which candidate\n      won, so whether their text is about them is UNKNOWN, not clean. "
              "Re-enrich to learn it.")
    return len(found)


def owns_its_article(rec):
    """Whether the RECORD says this title is grounded on its own page and the fetch did not move.

    It clears a title only on positive evidence. A row with no recorded role, and one whose redirect the
    fetch could not report (the Enterprise endpoint names no page), stay counted. This guard ratchets, so
    the safe direction is to over-count: clearing a title on a question nothing answered would lower the
    number the NEXT publish is measured against, and hide a real regression under it.
    """
    return rec.get("plotArticleRole") == "own" and rec.get("plotArticleRedirected") is False


def shared_plot_articles(grounded, same_item=None):
    """`(language, article) -> [key, …]` for every article that grounds MORE THAN ONE title.

    Keyed on the language too: `Hamlet` on enwiki and `Hamlet` on dewiki are different articles, and
    collapsing them would report a title grounded on its own German article as sharing with a different
    title's English one.

    `plotLanguage` defaults to "en" rather than to None. The field was added after the first passes, so
    older enriched rows carry an English plot and no language — treating those as their own bucket would
    hide exactly the oldest, most-inherited groundings.

    `same_item` maps key -> the title's Wikidata identity (its IMDb id, which is P345 and unique per item).
    A group whose members are ALL one identity is one work behind several TMDB ids, not a borrowed
    article: `Don't Hug Me I'm Scared` 1-5 are a single Wikidata item with five TMDB ids, and `Carlos` is
    in TMDB as both a movie and a series. 31 titles are in that position, so the invariant cannot reach
    zero while they count. A member with no known identity does NOT exempt a group — a missing fact is not
    evidence of sameness.
    """
    groups = {}
    for key, rec in grounded.items():
        article = rec.get("plotArticle")
        if not article:
            continue
        groups.setdefault((rec.get("plotLanguage") or "en", article), []).append(key)

    def one_item(keys):
        items = {(same_item or {}).get(key) for key in keys}
        return len(items) == 1 and None not in items

    return {where: sorted(keys) for where, keys in groups.items()
            if len(keys) > 1 and not one_item(keys)}


def wikidata_identity(facts_records):
    """`key -> imdbId` from a facts record list. `imdbId` is P345, one per Wikidata item.

    Ships as `one_or_many` (check-facts-schema.py): a bare string for nearly every title, a list where
    Wikidata carries more than one. Both shapes read the same here.
    """
    out = {}
    for rec in facts_records:
        imdb = rec.get("imdbId")
        if isinstance(imdb, list):
            imdb = imdb[0] if len(imdb) == 1 else None
        if imdb:
            out[f"{rec['mediaType']}:{rec['tmdbId']}"] = imdb
    return out


def report_shared_articles(groups, grounded, limit=10):
    """Print the census, worst first, naming both sides — a guard nobody can act on is a guard nobody reads.

    COUNTS THE BORROWERS. Of the 1,066 titles in shared groups, 336 are grounded on their own page,
    correctly, and appear only because someone else borrowed it — ratcheting all 1,066 asks them to fix
    something they did not do. The number that moves when the grounding is fixed is the 730 who took
    someone else's article, and `owns_its_article` is what tells the sides apart. Both sides are still
    PRINTED: naming only the borrower would leave a reader unable to see what was borrowed from where.
    """
    members = sum(len(keys) for keys in groups.values())
    owners = sum(1 for keys in groups.values() for key in keys if owns_its_article(grounded[key]))
    unrecorded = sum(1 for keys in groups.values() for key in keys
                     if grounded[key].get("plotArticleRole") not in ROLES)
    titles = members - owners
    denom = len(grounded) or 1
    print(f"  grounded on an article that also grounds another title : {titles} "
          f"({100 * titles / denom:.2f}% of {denom} grounded)")
    if not groups:
        return titles
    if owners:
        print(f"      {owners} more are the article's own subject, grounded correctly, and not counted")
    if unrecorded:
        print(f"      {unrecorded} have no recorded provenance, so which side they are on is unknown — "
              "counted")
    print(f"      across {len(groups)} shared articles; worst {min(limit, len(groups))}:")
    for where, keys in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:limit]:
        language, article = where
        print(f"      {len(keys):3d} titles  {article!r} ({language})")
        for key in keys[:6]:
            rec = grounded[key]
            # The PLOT's length, which is `overview` once `groundedOnWikiPlot` has replaced it. Not
            # `overviewChars`: that field keeps the TMDB overview's length across the swap, so it answers a
            # different question and reads as a plot length that is wrong by an order of magnitude.
            chars = len(rec.get("overview") or "")
            side = "owns it" if owns_its_article(rec) else ""
            print(f"            {key:16s} {chars:7,d} ch  {rec.get('title') or ''!r} {side}".rstrip())
        if len(keys) > 6:
            print(f"            … and {len(keys) - 6} more")
    return titles


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--enriched-dir", required=True)
    ap.add_argument("--labels", required=True, help="the shipped labels blob (labels-<tax>.json)")
    ap.add_argument("--facts", help="scope to keys in this facts file, as the shipped dataset does")
    ap.add_argument("--max-batch-id", type=int, help="ignore batches above this (manifest `maxBatchId`)")
    ap.add_argument("--fail", action="store_true", help="exit 1 on a violation, once the counts are zero")
    ap.add_argument("--shared-plot-baseline", type=int,
                    help="the previously published shared-article title count; ANY increase exits 2")
    ap.add_argument("--stamp-meta",
                    help="record this run's shared-article counts into that manifest, as the next "
                         "publish's baseline")
    args = ap.parse_args()

    # Last occurrence wins, matching `finalize`'s own de-dup.
    has_plot = {}
    grounded = {}
    for name in batch_files(args.enriched_dir, args.max_batch_id):
        with open(os.path.join(args.enriched_dir, name), encoding="utf-8") as fh:
            for r in json.load(fh):
                key = f"{r['mediaType']}:{r['tmdbId']}"
                has_plot[key] = bool(r.get("hasWikiPlot"))
                # A title RE-enriched into a later batch without a plot must leave the grounded set, or the
                # census keeps reporting the article its superseded row was grounded on.
                if has_plot[key]:
                    grounded[key] = r
                else:
                    grounded.pop(key, None)

    with open(args.labels, encoding="utf-8") as fh:
        labelled = {f"{r.get('mediaType', 'movie')}:{r['tmdbId']}" for r in json.load(fh)["records"]}

    scope = None
    same_item = None
    if args.facts:
        with open(args.facts, encoding="utf-8") as fh:
            records = json.load(fh)["records"]
        scope = {f"{r['mediaType']}:{r['tmdbId']}" for r in records}
        # The enriched row carries no Wikidata id, so this file is the only place the one-item exemption
        # can learn that several TMDB ids are one work.
        same_item = wikidata_identity(records)

    def inscope(key):
        return scope is None or key in scope

    plot_no_labels = sorted(k for k, v in has_plot.items() if v and k not in labelled and inscope(k))
    labels_no_plot = sorted(k for k in labelled if has_plot.get(k) is False and inscope(k))

    print(f"enriched keys {len(has_plot)} (batches <= {args.max_batch_id or 'all'})  labelled {len(labelled)}")
    print(f"  hasWikiPlot and NOT labelled : {len(plot_no_labels)}")
    for k in plot_no_labels[:20]:
        print(f"      {k}")
    print(f"  labelled and NOT hasWikiPlot : {len(labels_no_plot)}")
    for k in labels_no_plot[:20]:
        print(f"      {k}")

    in_scope = {k: r for k, r in grounded.items() if inscope(k)}
    groups = shared_plot_articles(in_scope, same_item=same_item)
    shared_titles = report_shared_articles(groups, in_scope)
    provenance = plot_provenance(in_scope)
    misgrounded = report_provenance(provenance, in_scope)

    if args.stamp_meta:
        with open(args.stamp_meta, encoding="utf-8") as fh:
            meta = json.load(fh)
        # Unowned keys, stamped here and carried forward by `ManifestMerge` — the same reason
        # `manifest-counts.py` stamps `maxBatchId` rather than adding it to `DatasetMeta`, whose
        # `namingSidecar` would drop a new field on the next `metadata` run.
        meta["sharedPlotArticleTitles"] = shared_titles
        meta["sharedPlotArticles"] = len(groups)
        # What the collision census is a 35% proxy for. Stamped from the first publish that records it, so
        # the generation that first carries provenance leaves the baseline a later ratchet can use.
        meta["misgroundedTitles"] = misgrounded
        meta["provenanceUnrecordedTitles"] = len(provenance["unrecorded"])
        with open(args.stamp_meta, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=1)
            fh.write("\n")

    if args.shared_plot_baseline is not None and shared_titles > args.shared_plot_baseline:
        print(f"\nerror: grounding REGRESSED — {shared_titles} titles are grounded on an article that "
              f"also grounds another title, against {args.shared_plot_baseline} in the published dataset.",
              file=sys.stderr)
        print("       A title grounded on another work's article is described by that work: its labels, "
              "its facets and its\n       premise all come from a story it does not tell (oxyc/den-dataset#16).",
              file=sys.stderr)
        print("       The cause is the source-work fallback in `taxonomy-backfill`'s regroundOnWikipedia — "
              "longest-wins over\n       [own article, P144 source work], so one novel beats every "
              "adaptation's own article and all of them\n       inherit it. The named clusters above say "
              "which articles to look at.", file=sys.stderr)
        print("       Fix the grounding, or — if this increase is understood and deliberate — say why:",
              file=sys.stderr)
        print("         DEN_ALLOW_SHARED_PLOTS='why the count went up' scripts/publish-dataset.sh <dir>",
              file=sys.stderr)
        return 2

    if plot_no_labels or labels_no_plot:
        print("\nwarning: a title with a plot and no labels has no vector, so no More Like This and no "
              "thematic reach.", file=sys.stderr)
        return 1 if args.fail else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
