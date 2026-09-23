#!/usr/bin/env python3
"""The admission floors: what a title must clear before it is worth grounding and classifying.

**Admission is a union** (oxyc/den-dataset#27): a title is admitted when its TMDB vote count clears its
TMDB floor OR the number of Wikipedias with an article on it clears its Wikipedia floor. TMDB's count
under-represents older and non-Anglo-American work, and the Wikipedia count is Wikidata's (CC0), so it
can decide what a public dataset holds. Nothing the TMDB floor admits is ever lost.

This half used to be IMDb's vote count. IMDb's datasets are licensed for personal, non-commercial use and
not for building a database or a public site, so it is gone; the Wikipedia count replaced it, measured
below.

**The floors are per tier, not one number.** The shipped corpus was built from two discover passes: every
origin at 50 TMDB votes, and a foreign-depth pass at 15 for European, South American and Australian/New
Zealand origins, which is where regional titles live. 46.6% of the corpus has one of those origins, and
10,142 titles (21.3%) sit between 15 and 49 TMDB votes — admitted only by that tier. The Python port of the
worklist kept the 50 and lost the 15, so this restores it: a title belongs to every tier its origin matches
and is judged by the lowest floor among them.

**The Wikipedia floors are matched to the TMDB ones**, the way the IMDb floors were: the median
Wikipedia count of the titles sitting at the TMDB floor, over the 59,209 enriched titles of 2026-09-22.

    tier        TMDB floor   at it   median   within ±10%   median   quartiles   chosen floor
    worldwide   50           105     5        1,329         5        2 / 5 / 9   10 (see below)
    regional    15           917     3        2,034         3        1 / 3 / 5   3

**What the worldwide floor admits was measured below the TMDB floor**, where it matters: the enriched
titles hold 35 there (the corpus was discovered at 50 for these origins), so 1,906 titles were sampled from
`/discover` at 15–49 TMDB votes, by decade (43,130 films and 8,188 series sit in that band). 1,305 are in
the worldwide tier, which the TMDB floor refuses — about 33,600 titles weighted up. Of those:

    Wikipedia floor   admitted   weighted   of IMDb's 267 at 2,000   IMDb did not
    3                 972        ~20,000    237                      735
    5                 725        ~12,900    208                      517
    8                 426        ~6,300     146                      280
    10                276        ~3,600     96                       180

At 5 it keeps 78% of what the IMDb half admitted and adds more than it drops: silent and studio-era
films (`Chang` 1927, `Camille` 1921, `Born Reckless` 1930, `Frisco Jenny` 1933, `Soviet Toys` 1924),
television TMDB barely rates (`Superboy`, `The Courtship of Eddie's Father`, `A.D. Police`, `Money
Flower`) — 30 sampled at random were all real, documented works. What it drops of IMDb's are mostly
single-country television with an article on one to four wikis (`F Troop`, `Amen`, `Afsos`). The cost is
a plot fetch and, later, a classification for each of the ~12,900.

**The worldwide floor is set to 10, not the median 5**, because every admitted title also needs premise
tags, which are labelled by hand-run model batches rather than by the paid pass — the part that does
not scale. At 10 the tail admitted is ~3,600 clearly notable titles; a sample of American 15–49-vote
titles held nothing at 5 or above anyway. The regional floor stays at the median 3, where the regional
tier's own TMDB floor of 15 already admits most of what matters (Rederiet, Beck, Kvarteret Skatan).

**The tier is TMDB's origin where the worklist states it, and Wikidata's P495 where it does not.** Both
come off the title's worklist row or Wikidata; nothing asks TMDB about one title (oxyc/den-dataset#53).
A `discover` or `delta` row says whether `/discover` names it under a regional origin
(`pipeline/worklist`), which is the question the TMDB floors were set against, so for those rows the gate
is what it was. P495 alone does not reproduce it: judged by today's floors over the 47,548 corpus titles
with a facts row, swapping every title's tier to P495 moves 2,706 — 1,728 JOIN the regional tier, which
only admits more, and 978 LEAVE it, of which **381 then clear no floor at all** (below 50 TMDB votes and
below 10 Wikipedias). Most are co-productions TMDB files under several origins and Wikidata under one
(`Family Tree`: TMDB GB+US, P495 US), and 227 of the 978 have no P495. P495 is therefore only the
fallback, for a row whose worklist has no TMDB answer to give: an `export` row, or a hand-made list.

**Where each count comes from.** The TMDB count is the one on the title's WORKLIST row — `/discover`
stated it when the universe was built, and it is the number that query selected on. An export row carries
none, because the daily dump states popularity, and nothing else is asked: a title nothing states a TMDB
count for is judged on its Wikipedia count alone. A worklist row that stated no count is written without
one rather than with a zero, which would be below every floor. The Wikipedia count is asked of Wikidata
only for the titles TMDB's count leaves short.

That made a list of ids a narrower gate than a `/discover` universe: replayed over 200 corpus titles, an
id-only worklist refused 61 that TMDB's count had admitted (`Loose Change`, `The Answer Man`, `Casi
divas`). So a title the shipped catalogue (`data/genres-moods-curated.json`) names, on a row that states no
count, keeps the admission an earlier build gave it (`pipeline/enrich.admit`): a re-fetch plan or
`scripts/build-worklist.py`'s list needs no flag. The same replay then admitted 172 of the 200. The other
28 were enriched once and never shipped, every one of them plotless, and nothing a fresh out-dir can read
says they were admitted: a list of those is drained with both Wikipedia floors at 0, which admits every
title. A new title, and any row that states a count, is judged as above.
"""
from dataclasses import dataclass

#: The foreign-depth tier's origins: Europe, South America, Australia and New Zealand. Japan is left out
#: on purpose — its low-vote tail is mostly anime, and its popular titles clear the worldwide floor.
REGIONAL_ORIGINS = frozenset(
    "FR DE IT ES SE NO DK FI IS NL BE PL PT CZ AT CH IE GR HU RO GB BR AR CL CO PE UY VE BO EC AU NZ".split())


@dataclass(frozen=True)
class Floors:
    """The four floors: TMDB votes and Wikipedia articles, for the worldwide tier and the regional one."""
    tmdb: int = 50
    regional_tmdb: int = 15
    wikipedias: int = 10
    regional_wikipedias: int = 3

    def of(self, record):
        """`(tmdb_floor, wikipedia_floor)` for one title — the lowest of each among the tiers it belongs to.

        Every title is in the worldwide tier; a regional title is in both. `regional` is the worklist's
        answer, from `/discover`, and decides when present; otherwise `originCountry` — Wikidata's P495 —
        does. A title with neither is judged worldwide, since nothing says it is regional.
        """
        regional = record.get("regional")
        if regional is None:
            regional = bool(REGIONAL_ORIGINS & set(record.get("originCountry") or ()))
        if regional:
            return (min(self.tmdb, self.regional_tmdb),
                    min(self.wikipedias, self.regional_wikipedias))
        return self.tmdb, self.wikipedias

    @property
    def lowest_tmdb(self):
        """What discovery enumerates at: a title below every TMDB floor can still be admitted on its
        Wikipedia count only if something enumerated it, so this is the lowest floor any tier uses."""
        return min(self.tmdb, self.regional_tmdb)


DEFAULT = Floors()


def given(tmdb=None, regional_tmdb=None, wikipedias=None, regional_wikipedias=None):
    """The defaults, with whichever floors a run named replaced."""
    named = {"tmdb": tmdb, "regional_tmdb": regional_tmdb, "wikipedias": wikipedias,
             "regional_wikipedias": regional_wikipedias}
    return Floors(**{name: value for name, value in named.items() if value is not None})
