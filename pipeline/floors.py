#!/usr/bin/env python3
"""The admission floors: which vote counts a title must clear before it is worth grounding and classifying.

**Admission is a union** (oxyc/den-dataset#27): a title is admitted when its TMDB vote count clears its
TMDB floor OR its IMDb vote count clears its IMDb floor. Each source undercounts a different set of titles
— TMDB under-represents older and non-Anglo-American work (`Elkürtük`: 40,939 IMDb votes against 44 on
TMDB), IMDb under-represents non-English television (`El Señor de los Cielos`: 3,650 TMDB, 1,648 IMDb) — so
either one alone drops titles the other rightly admits, and nothing the TMDB floor admitted is ever lost.

**The floors are per tier, not one number.** The shipped corpus was built from two discover passes: every
origin at 50 TMDB votes, and a foreign-depth pass at 15 for European, South American and Australian/New
Zealand origins, which is where regional titles live. 46.6% of the corpus has one of those origins, and
10,142 titles (21.3%) sit between 15 and 49 TMDB votes — admitted only by that tier. The Python port of the
worklist kept the 50 and lost the 15, so this restores it: a title belongs to every tier its origin matches
and is judged by the lowest floor among them.

**The IMDb floors are matched to the TMDB ones, measured over the corpus** (47,548 titles with an enriched
record, IMDb's `title.ratings` of 2026-09-22): the median IMDb count of the titles sitting AT the TMDB
floor.

    tier        TMDB floor   titles at it        median IMDb   within ±10%   chosen IMDb floor
    worldwide   50           72 (non-regional)   2,110         2,302         2,000
    regional    15           469                 457           490           500

**The tier is read off TMDB's `origin_country`, and that is a measured decision, not an oversight.**
oxyc/den-dataset#53 is taking TMDB out of the title path, and Wikidata's P495 (country of origin) is the
obvious replacement — the facts sidecar already carries it for 99.3% of the corpus. Measured over the
47,548 enriched titles that corpus holds: swapping the tier to P495 moves 2,570 of them. 1,739 JOIN the
regional tier, which only ever admits more; 831 LEAVE it and are judged at 50 TMDB votes instead of 15,
and **272 of those then clear no floor at all** — below 50 on TMDB, below 2,000 on IMDb, out of the
corpus. 239 of the 272 are co-productions TMDB files under several origins and Wikidata under one
(`Doll & Em`: TMDB GB, P495 US), and 33 have no P495 at all. Those are precisely the regional titles the
15 exists for, and a below-floor verdict is never checkpointed, so they would not fail — they would stay
pending, re-fetched and re-refused every pass, silently. The tier therefore stays on TMDB's origins until
something states a co-production's countries as fully as TMDB does. It is the one field keeping the
per-title detail call alive.

**Where each count comes from.** The TMDB count is the one on the title's WORKLIST row — `/discover`
stated it when the universe was built, and it is the number that query selected on, so the gate does not
ask TMDB for it again per title (oxyc/den-dataset#53). An export row carries none, because the daily dump
states popularity; those fall back to the detail call while it is still made. A title nothing states a
TMDB count for is judged on IMDb's count alone rather than as a title with zero votes.

2,000 is also the whole corpus's ±10% median at 50 (1,992), the figure the decision was made on. Rounded
DOWN for the worldwide tier, since the union only ever adds: a floor a little low admits a few more titles
IMDb rates, and costs a TMDB detail call and a plot fetch each, while a floor a little high silently keeps
out exactly the titles the IMDb half exists for. Judged by these floors, every corpus title the TMDB floor
admits stays admitted; 42,561 (89.5%) clear both, 4,964 (10.4%) TMDB only — 18.2% of non-English titles
against 4.7% of English — which is why IMDb cannot be the gate alone.
"""
from dataclasses import dataclass

#: The foreign-depth tier's origins: Europe, South America, Australia and New Zealand. Japan is left out
#: on purpose — its low-vote tail is mostly anime, and its popular titles clear the worldwide floor.
REGIONAL_ORIGINS = frozenset(
    "FR DE IT ES SE NO DK FI IS NL BE PL PT CZ AT CH IE GR HU RO GB BR AR CL CO PE UY VE BO EC AU NZ".split())


@dataclass(frozen=True)
class Floors:
    """The four floors: TMDB and IMDb, for the worldwide tier and the regional one."""
    tmdb: int = 50
    regional_tmdb: int = 15
    imdb: int = 2000
    regional_imdb: int = 500

    def of(self, record):
        """`(tmdb_floor, imdb_floor)` for one title — the lowest of each among the tiers it belongs to.

        Every title is in the worldwide tier; a title whose TMDB origin is regional is in both. A title
        with no origin at all is judged worldwide, since nothing says it is regional.

        `originCountry` is TMDB's, deliberately — the module docstring has the measurement that kept it
        there, and it is the last per-title TMDB field the admission path reads.
        """
        if REGIONAL_ORIGINS & set(record.get("originCountry") or ()):
            return min(self.tmdb, self.regional_tmdb), min(self.imdb, self.regional_imdb)
        return self.tmdb, self.imdb

    @property
    def lowest_tmdb(self):
        """What discovery enumerates at: a title below every TMDB floor can still be admitted on IMDb's
        count only if something enumerated it, so this is the lowest floor any tier uses."""
        return min(self.tmdb, self.regional_tmdb)

    @property
    def lowest_imdb(self):
        """The lowest IMDb floor — the dump is read only above it."""
        return min(self.imdb, self.regional_imdb)


DEFAULT = Floors()


def given(tmdb=None, regional_tmdb=None, imdb=None, regional_imdb=None):
    """The defaults, with whichever floors a run named replaced."""
    named = {"tmdb": tmdb, "regional_tmdb": regional_tmdb, "imdb": imdb, "regional_imdb": regional_imdb}
    return Floors(**{name: value for name, value in named.items() if value is not None})
