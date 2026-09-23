#!/usr/bin/env python3
"""Recover `plotArticleRole` / `plotArticleRedirected` for rows enriched before the pass recorded them.

The enrich pass records which of its candidates grounded each plot (oxyc/den-dataset#16). Every row in the
shipped generation predates that, so the per-title census reports 47,529 titles as UNKNOWN and the only
invariant left is the collision census — which sees 730 of the ~2,080 mis-grounded titles, because a title
grounded on a novel that grounds nothing else collides with nobody.

Re-enriching to learn what is already on disk would cost a full scrape. The decision is REPLAYABLE instead:
the pass's own response cache still holds the SPARQL bodies naming each title's candidates, and the
`action=parse` bodies naming where each fetch landed. Matching the recorded `plotArticle` against those says
which candidate won and whether a redirect moved it.

    pipeline/backfill_plot_provenance.py --enriched-dir out-repass/enriched --out-dir out-repass/enriched

`--out-dir` may be `--enriched-dir`, which rewrites in place; batches are read whole and written whole, so a
row is never half-updated. Point it elsewhere to keep the original.

WHAT THIS WILL NOT DO. It never writes a role it did not derive from a cached body, and it never overwrites
one the pass itself recorded. A row the cache cannot explain keeps both fields ABSENT, which every reader
treats as unknown — guessing `own` would report a title as correctly grounded on the strength of a replay
that failed, which is worse than the gap it fills.

WHICH MEDIA A BODY ANSWERED. Movie 95 and series 95 are different titles, and a SPARQL body does not say
which one it was asked about — only the query text does, and that is hashed into the file name. So the name
is re-derived from the batches on disk (`mapping_media`). A body whose query cannot be re-derived is used
only where its match cannot be the other title's (`other_may_have_answered`), and what it decides is
counted apart ("media unconfirmed"). Otherwise the row is left unrecorded and counted as "ambiguous media".
"""
import argparse
import collections
import hashlib
import json
import os
import sys
import urllib.parse

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from lib import cache as caching, plot, wikidata  # noqa: E402

#: The three candidates the enrich pass chooses between, in the order it tries them.
OWN, OWN_OTHER_LANGUAGE, SOURCE_WORK = "own", "own-other-language", "source-work"

MEDIA = ("movie", "tv")

#: `ResponseCache.key`: SHA256 of "<namespace>\x01<host><path>?<query, sorted by key>", filed under the
#: first two characters of the digest. The HOST is part of it deliberately — every Wikipedia serves
#: `/w/api.php`, and leaving it out once handed Italian requests the English body.
NAMESPACE = "wiki"

#: `WikipediaSource.articleProse`'s request, which is what grounded every plot in the corpus. Reproduced
#: exactly: one extra or missing key hashes to a different file and the lookup silently finds nothing.
PARSE_QUERY = {"action": "parse", "prop": "wikitext|revid",
               "format": "json", "formatversion": "2", "redirects": "1"}


def article_title(url):
    """The article name in a Wikidata sitelink URL, in the form the enrich pass stored it."""
    if "/wiki/" not in url:
        return None
    return urllib.parse.unquote(url.split("/wiki/", 1)[1]).replace("_", " ") or None


def parse_landing(cache_dir, article, language):
    """Where this article's cached `action=parse` actually landed, or None if it was never fetched.

    The landing page is how a redirect is detected at all: Wikidata's sitelink for a sequel can point at a
    redirect into the parent work's page, and the name alone carries no trace of that.
    """
    query = dict(PARSE_QUERY, page=article)
    safe = "&".join(f"{key}={value}" for key, value in sorted(query.items()))
    digest = hashlib.sha256(
        f"{NAMESPACE}\x01{language}.wikipedia.org/w/api.php?{safe}".encode()).hexdigest()
    path = os.path.join(cache_dir, digest[:2], f"{digest}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return (json.load(handle).get("parse") or {}).get("title")
    except (OSError, ValueError):
        # A truncated body is a cache that was interrupted, not a title with no article. Unknown, and the
        # caller leaves the row unrecorded rather than reading the failure as an answer.
        return None


def batch_names(enriched_dir):
    return sorted(name for name in os.listdir(enriched_dir) if name.startswith("batch-") and name.endswith(".json"))


def mapping_digest(ids, media):
    """The cache file name of the enrich pass's mapping query for `ids` asked as `media`."""
    query = wikidata.mapping_query(ids, media, plot.HEADINGS_BY_LANGUAGE)
    return caching.ResponseCache(NAMESPACE, "", 0).key("sparql", {"q": query})


def mapping_media(enriched_dir):
    """cache file name -> media, for every mapping query a batch on disk re-derives.

    Per batch: each media's ids asked as that media, which is what the pass sends; and the whole batch asked
    as each media, which is what it sent before a mixed batch was refused (series 91545 was looked up as a
    movie that way). The digest is exact — a name that matches is that query. A body no batch re-derives
    came from a query this corpus cannot reconstruct (another out-dir, a re-run over a different id set, an
    older query text) and is left unattributed rather than assigned a media.
    """
    media_of = {}
    for name in batch_names(enriched_dir):
        with open(os.path.join(enriched_dir, name), encoding="utf-8") as handle:
            rows = json.load(handle)
        groups = [[r["tmdbId"] for r in rows if r.get("mediaType") == media] for media in MEDIA]
        for ids in [group for group in groups if group] + [[r["tmdbId"] for r in rows]]:
            for media in MEDIA:
                media_of[mapping_digest(ids, media)] = media
    return media_of


def candidates_by_key(cache_dir, media_of):
    """(media, tmdb id) -> every candidate set a cached SPARQL body returned for it.

    `media` is None for a body `mapping_media` could not attribute. A LIST per key, not one entry: the same
    title can be in several bodies, and the recorded article is what decides between them.
    """
    mapping = collections.defaultdict(list)
    for sub in sorted(os.listdir(cache_dir)):
        directory = os.path.join(cache_dir, sub)
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            body = _sparql_body(os.path.join(directory, name))
            if body is None:
                continue
            media = media_of.get(name[:-len(".json")])
            for tmdb, found in _candidates(body).items():
                mapping[(media, tmdb)].append(found)
    return mapping


def _sparql_body(path):
    """The parsed body if this cache entry is a SPARQL result, else None.

    The first 200 bytes are read before the whole file, because the cache holds ~80k article bodies for
    every couple of thousand SPARQL results and parsing them all costs minutes for nothing.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            if '"bindings"' not in handle.read(200):
                return None
            handle.seek(0)
            body = json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    bindings = (body.get("results") or {}).get("bindings")
    return bindings if isinstance(bindings, list) else None


def _candidates(bindings):
    """tmdb id -> {own article, source work, article by language} from one SPARQL result."""
    found = {}
    for row in bindings:
        tmdb = (row.get("tmdb") or {}).get("value")
        if not tmdb:
            continue
        entry = found.setdefault(tmdb, {"article": None, "source": None, "by_language": {}})
        if row.get("article") and not entry["article"]:
            entry["article"] = article_title(row["article"]["value"])
        if row.get("sourceArticle") and not entry["source"]:
            entry["source"] = article_title(row["sourceArticle"]["value"])
        if row.get("anyArticle") and row.get("anySite"):
            host = urllib.parse.urlparse(row["anySite"]["value"]).hostname or ""
            language = host.replace(".wikipedia.org", "")
            title = article_title(row["anyArticle"]["value"])
            if language and title:
                entry["by_language"][language] = title
    return found


def provenance(record, mapping, cache_dir):
    """(role, redirected, confirmed) for one grounded row, or (None, None, None) when the cache cannot say.

    The bodies attributed to the row's own media are asked first; `confirmed` is False when the answer came
    from a body whose media could not be re-derived. A body attributed to the OTHER media is never asked:
    it describes a different title that happens to share the id.
    """
    tmdb = str(record["tmdbId"])
    for media, confirmed in ((record["mediaType"], True), (None, False)):
        role, redirected = _match(record, mapping.get((media, tmdb), ()), cache_dir)
        if role is not None:
            return role, redirected, confirmed
    return None, None, None


def _match(record, entries, cache_dir):
    """(role, redirected) from one set of candidate entries, or (None, None).

    An EXACT match wins over a redirect match wherever both are available: the recorded `plotArticle` is the
    article the fetch resolved to, so a candidate that equals it was reached without moving, and a candidate
    that merely lands on it is the weaker claim.
    """
    article = record.get("plotArticle")
    language = record.get("plotLanguage") or "en"
    if not article:
        return None, None
    landed = None
    for entry in entries:
        # `article` and `source` are enwiki names; `by_language` holds every other Wikipedia. A non-English
        # plot can only have come from that language's sitelink — the source work is fetched in English.
        if language == "en":
            wanted = ((entry["article"], OWN), (entry["source"], SOURCE_WORK))
        else:
            wanted = ((entry["by_language"].get(language), OWN_OTHER_LANGUAGE),)
        for asked, role in wanted:
            if not asked:
                continue
            if asked == article:
                return role, False
            if landed is None and parse_landing(cache_dir, asked, language) == article:
                landed = (role, True)
    return landed or (None, None)


def other_may_have_answered(record, mapping, held, cache_dir):
    """Whether an unattributed body's match could be the OTHER title with this id answering.

    Where bodies ARE attributed to the other media, they say what that title's candidates are: if none of
    them is or lands on the recorded article, the match was not that title's. Where there are none, the
    corpus holding both titles is enough to leave it open.
    """
    tmdb = str(record["tmdbId"])
    other = "tv" if record["mediaType"] == "movie" else "movie"
    entries = mapping.get((other, tmdb))
    if entries:
        return _match(record, entries, cache_dir)[0] is not None
    return len(held[tmdb]) > 1


def recorded(record):
    """Whether the enrich pass already recorded a role, which this never overwrites."""
    return record.get("plotArticleRole") in (OWN, OWN_OTHER_LANGUAGE, SOURCE_WORK)


def backfill(enriched_dir, out_dir, cache_dir):
    """Fill the two fields in every batch, and return what was written."""
    mapping = candidates_by_key(cache_dir, mapping_media(enriched_dir))
    batches = {}
    held = collections.defaultdict(set)
    for name in batch_names(enriched_dir):
        with open(os.path.join(enriched_dir, name), encoding="utf-8") as handle:
            batches[name] = json.load(handle)
        for record in batches[name]:
            held[str(record["tmdbId"])].add(record["mediaType"])
    os.makedirs(out_dir, exist_ok=True)
    counts = collections.Counter()
    for name, rows in batches.items():
        for record in rows:
            if not record.get("hasWikiPlot"):
                continue
            if recorded(record):
                counts["already recorded"] += 1
                continue
            role, redirected, confirmed = provenance(record, mapping, cache_dir)
            if role is None:
                counts["unrecoverable"] += 1
                continue
            if not confirmed and other_may_have_answered(record, mapping, held, cache_dir):
                counts["ambiguous media"] += 1
                continue
            record["plotArticleRole"] = role
            record["plotArticleRedirected"] = redirected
            label = role + (" +redirect" if redirected else "")
            counts[label if confirmed else f"{label} (media unconfirmed)"] += 1
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as handle:
            json.dump(rows, handle)
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--enriched-dir", required=True)
    parser.add_argument("--out-dir", required=True,
                        help="where the rewritten batches go; pass --enriched-dir to rewrite in place")
    parser.add_argument("--cache-dir", default=os.path.join(".cache", NAMESPACE),
                        help="the enrich pass's response cache (default: .cache/wiki)")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.cache_dir):
        # Without the cache there is nothing to replay, and an empty run would rewrite every batch
        # unchanged and report a clean zero — a no-op that reads as a finished job.
        print(f"error: no response cache at {args.cache_dir}. The decision is replayed from the bodies the "
              f"enrich pass cached; without them it cannot be recovered and the rows must be re-enriched.",
              file=sys.stderr)
        return 1

    counts = backfill(args.enriched_dir, args.out_dir, args.cache_dir)
    for reason, count in sorted(counts.items()):
        print(f"  {reason:28s} {count}")
    if counts["unrecoverable"]:
        print(f"\n{counts['unrecoverable']} rows keep both fields absent: the cache names no candidate that "
              f"is, or lands on, the article they recorded.\nThat is UNKNOWN — the census counts them as "
              f"neither grounded nor mis-grounded.")
    if counts["ambiguous media"]:
        print(f"\n{counts['ambiguous media']} rows keep both fields absent because the only body that explains "
              f"them has a query this corpus cannot re-derive, and the title of the other media with the "
              f"same id may be the one it answered.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
