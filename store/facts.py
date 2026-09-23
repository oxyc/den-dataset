"""Facts — everything Wikidata says about a title: who made it, where it is from, when it aired.

den-spec `wire/store-v2.md` § "Facts". Nothing here is a model's answer; the one exception is
`facts_has_vec`, which is this pipeline's own scrape-time bookkeeping and is named so it cannot be
mistaken for `vec_plot_has`.
"""
import sys
from datetime import date

from .format import I32_NONE, U32_NONE

# Every Wikidata list field, kept whole. The store deliberately does NOT choose which of these a reader
# will want: facts-slim froze the field set its reader parsed at the time, and `screenwriters`,
# `composers` and `cinematographers` then sat in the file unread for months — the last two being exactly
# the "who made it feel like this" credits a people-led row needs. We paid to scrape them; they ship.
# corpus field -> section base name. Section names cap at 16 bytes, so the long ones are abbreviated
# HERE, once, rather than silently truncated at write time.
ENTITY_LISTS = {
    "cast": "cast",
    "broadcaster": "broadcasters",
    "composers": "composers",
    "cinematographers": "dops",
    "distributors": "distributors",
    "productionCompanies": "companies",
    "narrativeLocations": "locations",
    "mainSubjects": "subjects",
    "instanceOf": "instance_of",
    "basedOn": "based_on",
}

#: The credit fields `makers` is the union of. Screenwriters were shipped and dropped by the reader for
#: months: 67.2% coverage feeding the rail's heaviest weight.
MAKER_FIELDS = ("directors", "creators", "screenwriters")

#: The same three credits kept apart, so a reader can tell a director from a writer — `makers` cannot
#: (oxyc/den#135). den-spec's optional role sections, written after `makers`: corpus field -> section.
ROLE_LISTS = {"directors": "directors", "creators": "creators", "screenwriters": "writers"}

EPOCH = date(1970, 1, 1)
PRECISION = {"day": 0, "month": 1, "year": 2, "decade": 3, "century": 4}
PRECISION_NONE = 0xFF


def title_imdb_id(raw):
    """The `tt…` id for a title, or `None` for anything else IMDb numbers.

    IMDb's id space is namespaced by prefix and Wikidata's P345 is not checked against it, so a title can
    arrive carrying a person (`nm19818812`) or an event (`ev0000003`). Two rows do. Stored, they read as a
    title id to anything that trusts the column.

    That column is now a JOIN KEY: den-atlas ranks browse rows by joining IMDb's public `title.ratings`
    on it, so a wrong-namespace id is a row that can never match and is indistinguishable from a title
    IMDb has no rating for. A prefix check is the whole fix, and it belongs here rather than in the
    reader, because every reader would otherwise have to repeat it.
    """
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    return raw if raw.startswith("tt") and raw[2:].isdigit() else None


def days_since_epoch(value, key):
    """`{"date": "2007-01-20", "precision": "day"}` → (days, precision code).

    The corpus stores a dated fact as an object, not an int. Reading it with `isinstance(v, int)` left
    `released` at i32::MIN on all 47,618 rows — 46,765 of which have a date — and nothing noticed, because
    a column of sentinels is structurally perfect.
    """
    if not isinstance(value, dict):
        return I32_NONE, PRECISION_NONE
    text = value.get("date")
    if not isinstance(text, str) or not text:
        return I32_NONE, PRECISION_NONE
    code = PRECISION.get(value.get("precision"), PRECISION_NONE)
    try:
        parts = text.lstrip("+-").split("-")
        year = int(parts[0]) * (-1 if text.startswith("-") else 1)
        month = int(parts[1]) if len(parts) > 1 and parts[1] != "00" else 1
        day = int(parts[2]) if len(parts) > 2 and parts[2] != "00" else 1
        return (date(year, month, day) - EPOCH).days, code
    except (ValueError, IndexError, OverflowError):
        sys.exit(f"{key}: released date {text!r} is not a date this writer understands")


def read_genre_map(blob, path):
    """`genreMap` out of the facts artifact, and its record count.

    `genreMap` turns Wikidata genre Q-ids into TMDB genre ids, per media type. Without it the `genres`
    section shipped as ZERO bytes: `isinstance(g, int)` is false for every "Q842256", so all 99,216
    values were dropped and the empty section passed every check.
    """
    genre_map = blob.get("genreMap") or {}
    if not genre_map:
        sys.exit(f"{path} has no genreMap — the genres section would ship empty")
    return genre_map, len(blob.get("records") or [])


class Facts:
    """The fact columns. `makers` and `cast` are read back out afterwards — they are what the entity
    credit counts and the inverted maker index are built from."""

    def __init__(self, genre_map):
        self.genre_map = genre_map
        self.makers = []
        self.roles = {field: [] for field in ROLE_LISTS}
        self.entity_lists = {field: [] for field in ENTITY_LISTS}
        self.based_kind = []
        self.genres = []
        self.countries = []
        self.languages = []
        self.aliases = []
        self.imdb = []
        self.released = []
        self.released_prec = []
        self.ended = []
        self.ended_prec = []
        self.episodes = []
        self.seasons = []
        self.has_vector = []
        self.runtime = []
        self.franchise = []
        self.orig_lang = []

    def intern(self, strings, facts, titles):
        for kind in facts.get("basedOnKind") or []:
            strings.add(kind)
        for name in (titles.get("en"), titles.get("orig")):
            strings.add(name)
        for alias in titles.get("aliases") or []:
            strings.add(alias)
        strings.add(title_imdb_id(facts.get("imdbId")))
        for c in facts.get("countries") or []:
            strings.add(c)
        for lang in facts.get("languages") or []:
            strings.add(lang)

    def add(self, strings, key, facts, titles, ent):
        # makers = directors ∪ creators ∪ screenwriters.
        seen, row_makers = set(), []
        for field in MAKER_FIELDS:
            for q in facts.get(field) or []:
                i = ent.id(q, "maker")
                if i is not None and i not in seen:
                    seen.add(i)
                    row_makers.append(i)
        self.makers.append(row_makers)
        for field in ROLE_LISTS:
            row_role = []
            for i in (ent.id(q) for q in facts.get(field) or []):
                if i is not None and i not in row_role:
                    row_role.append(i)
            self.roles[field].append(row_role)
        row_genres = []
        for q in facts.get("genres") or []:
            if not isinstance(q, str):
                if isinstance(q, int) and q not in row_genres:
                    row_genres.append(q)
                continue
            entry = self.genre_map.get(q)
            if entry is None:
                ent.unresolved["genre"] += 1
                continue
            # The FILM mapping, falling back to the series one — `genre.movie.or(genre.tv)`, which is
            # what den-atlas has always done, and the reader then folds a composite into its film parts.
            #
            # Taking the media-specific mapping instead looked more faithful and lost information: TMDB's
            # series genres are COMPOSITES (10765 "Sci-Fi & Fantasy", 10759 "Action & Adventure"), so
            # several distinct Wikidata genres collapse into one. Measured over the corpus it changed
            # 1,943 series and was a strict LOSS for 607 of them — Chilling Adventures of Sabrina went
            # from Drama/Horror/Fantasy to Drama/Sci-Fi&Fantasy, dropping Horror outright, and Scooby-Doo
            # lost Horror the same way. It also emitted composite ids that the clients' hide rules and
            # /recommend do not speak, since both work in film genres.
            #
            # One Wikidata genre can still be several TMDB genres — "romantic comedy" is Comedy AND
            # Romance — so a list is accepted and a bare int treated as a list of one.
            mapped = (entry.get("movie") or entry.get("tv")) if isinstance(entry, dict) else entry
            for value in (mapped if isinstance(mapped, list) else [mapped]):
                if isinstance(value, int) and value not in row_genres:
                    row_genres.append(value)
        # NOT sorted. The order is the genreMap's, and `/recommend` treats the first as the most
        # significant when it names a title's genre — so sorting quietly renamed things: Jupiter
        # Ascending went from Action to Adventure. Dedup preserving first-seen instead.
        seen, ordered = set(), []
        for g in row_genres:
            if g not in seen:
                seen.add(g)
                ordered.append(g)
        self.genres.append(ordered)
        self.countries.append([strings.id(c) for c in facts.get("countries") or []])
        self.languages.append([strings.id(x) for x in facts.get("languages") or []])
        # `en` and `orig` FIRST, then the aliases — the same three sources, in the same order, that the JSON
        # reader chained (`den-atlas/src/facts.rs`: `t.en.chain(t.orig).chain(t.aliases)`).
        #
        # This wrote `aliases` alone until 2026-09-21, and it is a DIFFERENT field:
        # `check-alias-collisions.py` treats `own_names = [orig, en]` as the set aliases are vetted against,
        # so the two never overlap by construction. The names dropped were therefore exactly a title's own.
        # atlas builds its display-title search index from these, so every title whose original name differs
        # from its TMDB one stopped being findable by that name — "Gisaengchung" for Parasite. The
        # record-by-record comparison could not see it: the names live in their own map, not on a record.
        row_titles, taken = [], set()
        for name in [titles.get("en"), titles.get("orig"), *(titles.get("aliases") or [])]:
            if not isinstance(name, str) or not name.strip():
                continue
            ident = strings.id(name)
            if ident not in taken:
                taken.add(ident)
                row_titles.append(ident)
        self.aliases.append(row_titles)

        self.imdb.append(strings.id(title_imdb_id(facts.get("imdbId"))))
        days, prec = days_since_epoch(facts.get("released") or facts.get("started"), key)
        self.released.append(days)
        self.released_prec.append(prec)
        mins = facts.get("runtimeMinutes")
        self.runtime.append(min(65535, int(mins)) if isinstance(mins, int) and mins > 0 else 0)
        # Raw Q-IDs, not entity indices. The entity table holds almost no franchise entities —
        # 2,680 of 3,019 references are unresolvable — so interning these dropped the franchise for
        # seven titles in eight, silently. A Q-id needs no table to be useful: two titles sharing one
        # are in the same series whether or not anything can name it.
        #
        # The facts stage writes every P179 target that is a series, most specific first
        # (`lib/wikidata_facts.franchises`), and nothing else: a critics' list filed under P179 is not a
        # franchise. ALL of them, in that order. store-v1 kept only the first, and 219 titles are in more
        # than one real series — The Batman and The Hobbit only meet their siblings through the second.
        # A bare string is the older single-valued shape, read as a list of one.
        raw = facts.get("franchise")
        row_franchises = []
        for fr in (raw if isinstance(raw, list) else [raw]):
            fr_num = int(fr[1:]) if isinstance(fr, str) and fr.startswith("Q") and fr[1:].isdigit() else None
            if fr_num is not None and fr_num not in row_franchises:
                row_franchises.append(fr_num)
        self.franchise.append(row_franchises)
        for field in ENTITY_LISTS:
            self.entity_lists[field].append(
                [i for i in (ent.id(q, field) for q in facts.get(field) or []) if i is not None])
        self.based_kind.append([strings.id(k) for k in facts.get("basedOnKind") or [] if k])
        end_days, end_prec = days_since_epoch(facts.get("ended"), key)
        self.ended.append(end_days)
        self.ended_prec.append(end_prec)
        eps, sns = facts.get("episodes"), facts.get("seasons")
        self.episodes.append(min(65535, int(eps)) if isinstance(eps, int) and eps > 0 else 0)
        self.seasons.append(min(65535, int(sns)) if isinstance(sns, int) and sns > 0 else 0)
        self.has_vector.append(1 if facts.get("hasVector") else 0)
        langs = facts.get("languages") or []
        self.orig_lang.append(strings.id(langs[0]) if langs else U32_NONE)

    def put(self, sec, rows):
        """Everything up to `facts_has_vec`. The applicability columns follow, then `put_trailing`."""
        sec.put_list("makers", "I", 4, self.makers)
        # May be empty: a corpus with no series has no creators. A role field missing from the source cannot
        # hide here, because `makers` is built from the same three fields and is refused empty.
        for field, section in ROLE_LISTS.items():
            sec.put_list(section, "I", 4, self.roles[field], allow_empty=True)
        for field, section in ENTITY_LISTS.items():
            sec.put_list(section, "I", 4, self.entity_lists[field])
        sec.put_list("based_kind", "I", 4, self.based_kind)
        sec.put_list("genres", "I", 4, self.genres)
        sec.put_list("countries", "I", 4, self.countries)
        sec.put_list("languages", "I", 4, self.languages)
        sec.put_list("alias_titles", "I", 4, self.aliases)
        sec.put("imdb", "I", self.imdb, 4, expect=rows)
        sec.put("released", "i", self.released, 4, expect=rows)
        sec.put("released_prec", "B", self.released_prec, 1, expect=rows)
        sec.put("ended", "i", self.ended, 4, expect=rows)
        sec.put("ended_prec", "B", self.ended_prec, 1, expect=rows)
        sec.put("episodes", "H", self.episodes, 2, expect=rows)
        sec.put("seasons", "H", self.seasons, 2, expect=rows)
        # `facts_has_vector` is the FACTS field of that name — a scrape-time claim about whether a
        # vector was expected. It is NOT "this row has a plot vector": 9,010 rows carry a real
        # vector while this reads 0, and 3 read 1 with none. It sat two entries from
        # `vec_premise_has`, which does mean what it says, under a name that invited the confusion.
        sec.put("facts_has_vec", "B", self.has_vector, 1, expect=rows)

    def put_trailing(self, sec, rows):
        """The three fact columns the writer emits after the applicability ones."""
        sec.put("runtime", "H", self.runtime, 2, expect=rows)
        sec.put_list("franchise", "I", 4, self.franchise)
        sec.put("orig_lang", "I", self.orig_lang, 4, expect=rows)
