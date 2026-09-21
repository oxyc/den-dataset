"""Cards — `card_title` and `card_year`.

den-spec `wire/store-v1.md` § "Cards". Both come from Wikidata: `card_poster` and `votes` were a
vendor's content and left with oxyc/den#118, so what a card draws is now a free name and a free date.
"""
import re

from .format import I16_NONE

#: A trailing parenthetical that disambiguates rather than names, by the vocabulary the corpus actually
#: uses. 18,476 titles across Wikidata labels and article names carry one; `(film)` alone accounts for
#: 6,589, then `(TV series)`, `(<year> film)`, and the non-English equivalents Wikidata labels arrive in.
#:
#: A vocabulary rather than "strip any trailing (...)", because that would damage real names: TMDB agrees
#: with Wikidata that *South Park (Not Suitable for Children)*, *To Have (Or Not)*, *Frontier(s)* and
#: *Everything You Always Wanted to Know About Sex* (*But Were Afraid to Ask)* end the way they do.
#:
#: Its known limit: `Pilot (Our Girl)` keeps its parenthetical, because an episode disambiguated by the
#: name of its series is not distinguishable by vocabulary from a title that ends in a parenthesis. 36 of
#: 47,618 rows keep one this way.
_DISAMBIGUATOR = re.compile(
    r"^(?:\d{4}|\d{4}\s.*|.*\b(?:film|movie|tv|television|series|serial|mini-?series|special|programme|"
    r"program|novel|album|song|video\s*game|play|anime|manga|franchise|soundtrack|short|documentary|"
    r"episode|season|book|fernsehserie|pel[ií]cula|s[ée]rie|serie|filme|telenovela|drama)\b.*)$",
    re.IGNORECASE,
)
_TRAILING_PAREN = re.compile(r"\s*\(([^()]*)\)\s*$")


def display_title(titles):
    """The name to draw on a card, from Wikidata — never from a catalogue vendor.

    `titles.en` is the Wikidata English label and it answers 47,609 of 47,618 rows on its own; `orig` and
    then the aliases catch the rest. Measured against the TMDB title this replaces, it is **exact for
    88.86%** of the corpus.

    The 11% that differ are not errors — they are the other English name a work goes by, and the free
    source is frequently the better one: *9½ Weeks* for TMDB's *Nine 1/2 Weeks*, *Cry Wolf* for
    *Cry_Wolf*, *Friday the 13th Part VI: Jason Lives* for *Jason Lives - Friday the 13th Part VI*. Only
    **one** row in the residual is non-Latin, which was the risk worth measuring: a work people know by an
    English name must not come back as its original-language one.

    **Five rows have no free name at all** — `facts.titles` is empty for them (`movie:1110820`,
    `movie:1300331`, `movie:1489931`, `tv:256150`, `tv:297492`; 0–45 votes, one of them unnamed in TMDB
    too). They lose their card and drop out of browse and search, which is the honest outcome: we have no
    name we are allowed to publish.

    The Wikipedia article name is deliberately NOT a source here, though it names the work in English.
    It is not in the corpus, so it would need a new writer input to reach 8 rows; and a title whose own
    article was too thin is grounded on another work's article (oxyc/den-dataset#16), which would name
    the novel rather than the film.
    """
    titles = titles or {}
    candidates = [titles.get("en"), titles.get("orig")] + list(titles.get("aliases") or [])
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        name = candidate.strip()
        found = _TRAILING_PAREN.search(name)
        if found and _DISAMBIGUATOR.match(found.group(1).strip()):
            name = name[: found.start()].strip()
        if name:
            return name
    return None


def release_year(value):
    """The year out of the same dated object `facts.days_since_epoch` reads, or `None`.

    Read off the date text rather than derived from the day count, so a pre-1970 title is not a negative
    number to convert back, and a year-precision fact — which is most of what disagrees below — keeps the
    only component it actually asserts.

    This replaces a TMDB release year. Measured against it: the two agree for **92.56%** of the 46,702
    rows that have both, and the dominant disagreement is by a single year (2,639 rows), which is the
    festival premiere Wikidata dates against the general release TMDB dates. **835 rows have a TMDB year
    and no Wikidata date**, and they lose the year off their card — the alternative was to keep
    redistributing it.
    """
    if not isinstance(value, dict):
        return None
    text = value.get("date")
    if not isinstance(text, str) or not text:
        return None
    try:
        year = int(text.lstrip("+-").split("-")[0])
    except (ValueError, IndexError):
        return None
    return -year if text.startswith("-") else year


class Cards:
    """The card columns, and how many titles got a name at all."""

    def __init__(self):
        self.title = []
        self.year = []
        self.named = 0

    def intern(self, strings, titles):
        """The display name is a STRIPPED form of one of the names `facts.py` interns, so it is interned
        in its own right: `Batman (serial)` is in the dictionary for search, and `Batman` is what a card
        draws."""
        strings.add(display_title(titles))

    def add(self, strings, facts, titles):
        name = display_title(titles)
        self.title.append(strings.id(name))
        year = release_year(facts.get("released") or facts.get("started"))
        self.year.append(int(year) if isinstance(year, int) and -32767 <= year <= 32767 else I16_NONE)
        if name:
            self.named += 1

    def put(self, sec, rows):
        sec.put("card_title", "I", self.title, 4, expect=rows)
        sec.put("card_year", "h", self.year, 2, expect=rows)
        # No `votes`. It was a TMDB vote count, and the store is a public release asset that may not carry
        # a vendor's content (oxyc/den#118); the enriched batches it was read from are TMDB Content
        # outright, so dropping the column drops the last TMDB artifact this writer touched.
        #
        # It is not a signal lost, it is a signal moved: den-atlas joins IMDb's own public
        # `title.ratings.tsv.gz` on the `imdb` column at load and refreshes it daily, which covers 99.9% of
        # the corpus against this column's 99.85%, correlates with it at Spearman 0.85, and is FRESHER than
        # a number frozen at build time. It also brings an average rating, which no store ever carried.
        #
        # What the reader must have first, and does: a missing column here used to mean every browse row
        # sorted by tmdbId with nothing logged — *La Job* (tv:5) beside *Game of Thrones*, which is
        # oxyc/den-dataset#22. den-atlas reports `votes_unusable` on `/health` when neither source has a
        # count, and `/ready` fails on it, so the silence is gone from the one place it mattered.
