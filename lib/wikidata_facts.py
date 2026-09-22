#!/usr/bin/env python3
"""The facts sidecar's Wikidata queries — the CC0 ranking signals /recommend reads, one property at a time.

**One property per request.** Several multi-valued OPTIONALs in one query return their cross product,
which times out at WDQS on a 100-id batch.

**Only the per-property queries are cached**, under the key the Swift scrape used (`sparql-facts` plus
the query text, which includes the id batch): a scrape is re-run far more often than it succeeds outright,
and a batch that died on its last property should not pay for all of them again. The titles hop and the
entity and source-work lookups were never cached; the scrape checkpoints what they return instead.

Everything an entity is carried as is a Q-id; names live in a shared entity map, resolved once per build.
The coverage figures beside each spec were measured on this corpus, and they are why several
obvious-looking properties are absent: P1080 narrative universe scored 0%, P155/P156 sequel order 8%,
P166 awards 11%.
"""
import collections
import json
import math
import re
import unicodedata
import urllib.parse

from . import http
from .wikidata import HOST, ID_PROPERTY, PATH, WikidataError

#: The per-property requests, keyed on their text like the doc-facts ones — a different namespace path, so
#: the two scrapes' entries cannot collide.
CACHE_PATH = "sparql-facts"

Spec = collections.namedtuple("Spec", "key prop kind iso_via tv_only single numeric")


def spec(key, prop, kind, iso_via=None, tv_only=False, single=False, numeric=False):
    return Spec(key, prop, kind, iso_via, tv_only, single, numeric)


#: `entity` is a Q-id with a name in `entities`; `iso` resolves the value to a code through a second
#: property on it; `literal` is the bare value; `date` carries its precision. `single` collapses to one
#: value — a title has one IMDb id, and a single-element list made every consumer unwrap it — and
#: `numeric` emits a number, because Wikidata returns every literal as a string.
SPECS = (
    spec("instanceOf", "P31", "entity"),                     # film / series / miniseries / documentary
    spec("imdbId", "P345", "literal", single=True),          # 100%
    spec("released", "P577", "date"),                        # films: 71% day-precision
    spec("started", "P580", "date", tv_only=True),           # series: 98% day-precision
    spec("ended", "P582", "date", tv_only=True),
    spec("directors", "P57", "entity"),                      # films 97%, series 37% (TMDB: 25%)
    spec("creators", "P170", "entity"),                      # series 32-40%
    spec("screenwriters", "P58", "entity"),                  # films 76%
    spec("cast", "P161", "entity"),                          # films 86% (median 10 vs TMDB's cap of 4)
    spec("genres", "P136", "entity"),                        # finer than TMDB's 19; see genre_map
    spec("countries", "P495", "iso", iso_via="P297"),
    spec("languages", "P364", "iso", iso_via="P218"),
    spec("broadcaster", "P449", "entity", tv_only=True),     # 91% on series; TMDB carries nothing like it
    spec("productionCompanies", "P272", "entity"),           # 41-51%
    spec("distributors", "P750", "entity"),                  # 81%
    spec("composers", "P86", "entity"),                      # 71% on films
    spec("cinematographers", "P344", "entity"),              # 61% on films
    spec("runtimeMinutes", "P2047", "literal", single=True, numeric=True),
    spec("franchise", "P179", "entity", single=True),        # sparse (41% top films, 5% tail) — tiebreak only
    spec("mainSubjects", "P921", "entity"),                  # 29%
    spec("basedOn", "P144", "entity"),                       # 18% — links adaptations of one source
    spec("narrativeLocations", "P840", "entity"),            # 47%
    spec("seasons", "P2437", "literal", tv_only=True, single=True, numeric=True),
    spec("episodes", "P1113", "literal", tv_only=True, single=True, numeric=True),
)

#: `wikibase:timePrecision` → the name the sidecar carries. Coarser than a year is not useful for ordering.
PRECISION = {11: "day", 10: "month", 9: "year"}
_TRIM = {"day": 10, "month": 7, "year": 4}

#: TMDB's genre vocabulary, which is what the app's hide-genre setting stores. Movie and TV ids differ for
#: the same word, and TV folds several together, so a Wikidata genre maps to a different id per media.
TMDB_GENRES = {
    "action": (28, 10759), "adventure": (12, 10759), "animation": (16, 16), "comedy": (35, 35),
    "crime": (80, 80), "documentary": (99, 99), "drama": (18, 18), "family": (10751, 10751),
    "fantasy": (14, 10765), "history": (36, None), "horror": (27, None), "music": (10402, None),
    "mystery": (9648, 9648), "romance": (10749, None), "science fiction": (878, 10765),
    "thriller": (53, None), "war": (10752, 10768), "western": (37, 37), "kids": (None, 10762),
    "reality": (None, 10764), "soap opera": (None, 10766), "talk show": (None, 10767), "news": (None, 10763),
}

#: What a title was adapted FROM, folded to a closed vocabulary a browse row can render — Wikidata's own
#: types (`novel`, `novella`, `epistolary novel`, `roman à clef`) are far finer. Strongest first: a work is
#: often several things at once, and a real kind beats `other`, a text beats a character.
SOURCE_KINDS = ("book", "comic", "play", "game", "screen", "music", "franchise", "character", "other")
SOURCE_KIND_BY_TYPE = {
    "literary work": "book", "written work": "book", "novel": "book", "novella": "book",
    "short story": "book", "book": "book", "non-fiction book": "book", "autobiography": "book",
    "memoir": "book", "biography": "book", "fairy tale": "book", "poem": "book", "anthology": "book",
    "short story collection": "book", "children's literature": "book", "fable": "book", "light novel": "book",
    "comic book series": "comic", "manga series": "comic", "graphic novel": "comic", "manga": "comic",
    "comic strip": "comic", "comic book": "comic", "webtoon": "comic",
    "dramatic work": "play", "play": "play", "musical": "play", "dramatico-musical work": "play",
    "opera": "play", "theatrical production": "play",
    "video game": "game", "video game series": "game",
    "film": "screen", "television series": "screen", "animated television series": "screen",
    "film series": "screen", "limited series": "screen", "anime television series": "screen",
    "television film": "screen", "short film": "screen", "animated film": "screen",
    "media franchise": "franchise", "brand": "franchise",
    "song": "music", "single": "music", "album": "music",
}
#: A source that is a PERSON or a CHARACTER is not an adaptation of a text: "based on Batman" would fill a
#: books row with superhero films.
CHARACTER_MARKERS = ("character", "fictional human", "superhero team", "human", "mutate", "fictional")
SOURCE_SUFFIXES = (("novel", "book"), ("literature", "book"), ("manga", "comic"), ("comics", "comic"),
                   ("video game", "game"), ("play", "play"))
#: What an enwiki disambiguator says when it names a medium rather than something that tells two titles
#: apart. "Solaris (1972 film)" displays as "Solaris"; "Paris (city)" keeps its bracket.
ARTICLE_MARKERS = ("film", "tv series", "series", "miniseries", "season", "franchise", "novel", "manga", "anime")

_INT = re.compile(r"[+-]?\d+")
_FLOAT = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")


def _values(ids):
    return " ".join(f'"{tmdb_id}"' for tmdb_id in sorted(set(int(v) for v in ids)))


def facts_query(ids, media, item):
    """The per-property query, byte for byte what the Swift sent — it is the cache key."""
    if item.kind == "iso":
        select, body = "?tmdb ?code", f"?film wdt:{item.prop} ?v . ?v wdt:{item.iso_via or 'P297'} ?code ."
    elif item.kind == "date":
        select = "?tmdb ?v ?prec"
        body = (f"?film p:{item.prop} ?st . ?st psv:{item.prop} ?node . "
                f"?node wikibase:timeValue ?v ; wikibase:timePrecision ?prec .")
    else:
        select, body = "?tmdb ?v", f"?film wdt:{item.prop} ?v ."
    return (f"SELECT {select} WHERE {{\n"
            f"  VALUES ?tmdb {{ {_values(ids)} }}\n"
            f"  ?film wdt:{ID_PROPERTY[media]} ?tmdb .\n"
            f"  {body}\n"
            f"}}")


def bindings(payload):
    try:
        return json.loads(payload.decode("utf-8"))["results"]["bindings"]
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        raise WikidataError(f"not a SPARQL result: {payload[:200]!r}") from None


def _value(binding, name):
    return (binding.get(name) or {}).get("value")


def _int(text):
    return int(text) if text is not None and _INT.fullmatch(text) else None


def _qid(uri):
    """The last path segment of an entity URI, empty segments skipped as Swift's `split` skips them."""
    parts = [part for part in uri.split("/") if part]
    return parts[-1] if parts else ""


def trim_date(raw, precision):
    """`+1999-03-31T00:00:00Z` → `1999-03-31`, cut to what the precision asserts, so a year-precision
    value cannot be read as the 1st of January."""
    text = raw[1:] if raw.startswith("+") else raw
    if len(text) < 10:
        return None
    return text[:_TRIM[precision]]


def collapse(values, item):
    """A gathered list, as the spec promises it. Several values for a logically single field (two IMDb
    ids on a merged item) keep the first in sorted order rather than asserting there is only one."""
    if not values:
        return None
    if not item.single:
        return values
    first = values[0]
    if not item.numeric:
        return first
    number = _int(first)
    if number is None and _FLOAT.fullmatch(first):
        # "42.0" is 42 minutes. Half away from zero, as Swift's `rounded()` does — not Python's banker's.
        value = float(first)
        number = int(math.copysign(math.floor(abs(value) + 0.5), value))
    return number


def parse_facts(payload, item):
    """`tmdbId -> value` for one spec. A date keeps the EARLIEST value: a festival premiere and a wide
    release are both normal, and "newest first" needs something real to sort on."""
    lists, dates = {}, {}
    for binding in bindings(payload):
        tmdb_id = _int(_value(binding, "tmdb"))
        if tmdb_id is None:
            continue
        if item.kind == "date":
            precision = PRECISION.get(_int(_value(binding, "prec")))
            raw = _value(binding, "v")
            if raw is None or precision is None:
                continue
            trimmed = trim_date(raw, precision)
            # String order, so "1999" sorts before "1999-03-31" and a year-precision value beats a
            # day-precision one in the same year — what the Swift did, and what the shipped file holds.
            if trimmed is None or (tmdb_id in dates and dates[tmdb_id][0] <= trimmed):
                continue
            dates[tmdb_id] = (trimmed, precision)
            continue
        if item.kind == "iso":
            raw = _value(binding, "code")
            value = raw.upper() if raw is not None else None
        elif item.kind == "entity":
            raw = _value(binding, "v")
            value = _qid(raw) if raw is not None else None
            if value is not None and not value.startswith("Q"):
                value = None
        else:
            value = _value(binding, "v")
        if value is None:
            continue
        found = lists.setdefault(tmdb_id, [])
        if value not in found:
            found.append(value)
    out = {tmdb_id: collapse(sorted(values), item) for tmdb_id, values in lists.items() if values}
    out = {tmdb_id: value for tmdb_id, value in out.items() if value is not None}
    for tmdb_id, (date, precision) in dates.items():
        out[tmdb_id] = {"date": date, "precision": precision}
    return out


def _sparql(query):
    return http.request(HOST, PATH, {"format": "json"}, method="POST", body=query.encode("utf-8"),
                        headers={"Content-Type": "application/sparql-query",
                                 "Accept": "application/sparql-results+json"})


def fetch_facts(ids, media, item, cache=None):
    """`(tmdbId -> value, whether it was asked live)`. A body is kept only once it parses, so a WDQS
    maintenance page cannot outlive the outage; a kept body that no longer parses is asked again."""
    query = facts_query(ids, media, item)
    key = cache.key(CACHE_PATH, {"q": query}) if cache is not None else None
    if key is not None:
        hit = cache.read(key)
        if hit is not None:
            try:
                return parse_facts(hit, item), False
            except WikidataError:
                pass
    payload = _sparql(query)
    parsed = parse_facts(payload, item)
    if key is not None:
        cache.write(key, payload)
    return parsed, True


def article_title(url):
    """`https://en.wikipedia.org/wiki/Inception` → `Inception`: underscores to spaces, percent-decoded,
    and left as it is when the escapes do not decode."""
    marker = url.find("/wiki/")
    if marker < 0:
        return None
    raw = url[marker + len("/wiki/"):]
    decoded = raw
    if not re.search(r"%(?![0-9A-Fa-f]{2})", raw):
        try:
            decoded = urllib.parse.unquote(raw, errors="strict")
        except UnicodeDecodeError:
            decoded = raw
    title = decoded.replace("_", " ")
    return title or None


def _run(select, body, ids, media):
    query = (f"SELECT ?tmdb {select} WHERE {{\n"
             f"  VALUES ?tmdb {{ {_values(ids)} }}\n"
             f"  ?film wdt:{ID_PROPERTY[media]} ?tmdb .\n"
             f"  {body}\n"
             f"}}")
    return bindings(_sparql(query))


def _language(tag):
    """`zh-hant` → `zh`: the part of a language tag an ISO 639-1 code (P218) can match."""
    return (tag or "").split("-")[0].lower()


def titles(ids, media, languages=None):
    """`tmdbId -> {article, label, original, aliases}` — the enwiki article, `rdfs:label`, P1476 and every
    English alias. Search needs all of them: a title index holding only the original title misses
    "parasite" and "spirited away". Aliases are their own request because `skos:altLabel` is
    multi-valued and multiplies every other row.

    A title can have several of each — two P1476 values, an `en` label and a `mul` one, two items sharing
    a TMDB id — and WDQS returns the rows in no fixed order. The Swift kept whichever came first, so one
    title shipped a different original title from one run to the next (tv:105009: 東京リベンジャーズ, then
    東京卍リベンジャーズ). Every candidate is collected and one is chosen by rule:

      * the original title in one of the title's own languages (`languages`: tmdbId -> its P364 codes, as
        the `languages` fact holds them). P1476 is where editors also put translations, so the least
        value alone picks a Latin-script translation over the native title — over the facts oracle's
        2,000 titles, 44 have several P1476 values, and code-point order alone ships a Finnish title for a
        Ukrainian film (movie:502927) and a German one for a Swedish film (movie:1115105). Ties — a
        co-production's two languages, a romanisation tagged like its original, a title with no P364 —
        fall through to:
      * the least value by code point, for every string; and an `en` label before a `mul` one."""
    if not ids:
        return {}
    found = {}
    for binding in _run("?article ?label ?orig (LANG(?label) AS ?labelLang) (LANG(?orig) AS ?origLang)",
                        "  OPTIONAL { ?article schema:about ?film ; schema:isPartOf <https://en.wikipedia.org/> . }\n"
                        "  OPTIONAL { ?film wdt:P1476 ?orig . }\n"
                        "  OPTIONAL { ?film rdfs:label ?label . FILTER(LANG(?label) IN ('en','mul')) }",
                        ids, media):
        tmdb_id = _int(_value(binding, "tmdb"))
        if tmdb_id is None:
            continue
        seen = found.setdefault(tmdb_id, {"article": set(), "label": set(), "original": set()})
        if _value(binding, "article") is not None:
            title = article_title(_value(binding, "article"))
            if title is not None:
                seen["article"].add(title)
        if _value(binding, "label") is not None:
            seen["label"].add((_value(binding, "labelLang") != "en", _value(binding, "label")))
        if _value(binding, "orig") is not None:
            seen["original"].add((_language(_value(binding, "origLang")), _value(binding, "orig")))
    out = {}
    for tmdb_id, seen in found.items():
        own = {_language(code) for code in (languages or {}).get(tmdb_id) or ()}
        original = min(seen["original"], key=lambda cand: (cand[0] not in own, cand[1]), default=(None, None))
        out[tmdb_id] = {"article": min(seen["article"], default=None),
                        "label": min(seen["label"], default=(None, None))[1],
                        "original": original[1], "aliases": []}
    for binding in _run("?alias", "  ?film skos:altLabel ?alias . FILTER(LANG(?alias) IN ('en','mul'))",
                        ids, media):
        tmdb_id, alias = _int(_value(binding, "tmdb")), _value(binding, "alias")
        if tmdb_id is None or alias is None:
            continue
        row = out.setdefault(tmdb_id, {"article": None, "label": None, "original": None, "aliases": []})
        if alias not in row["aliases"]:
            row["aliases"].append(alias)
    return out


def entity_details(qids, batch=200):
    """`Q-id -> {name, tmdbPersonId, aliases}`. The aliases are what let a search answer "tom hanks" from a
    record holding only a Q-id; P4985 opens a person page with no name lookup. A label the service could
    not resolve comes back as the Q-id itself and is not a name."""
    out = {}
    for start in range(0, len(qids), batch):
        values = " ".join(f"wd:{q}" for q in qids[start:start + batch])

        def run(select, body):
            return bindings(_sparql(f"SELECT ?item {select} WHERE {{ VALUES ?item {{ {values} }} {body} }}"))

        for binding in run("?itemLabel ?pid",
                           '  SERVICE wikibase:label { bd:serviceParam wikibase:language "en,mul". }\n'
                           "  OPTIONAL { ?item wdt:P4985 ?pid . }"):
            uri = _value(binding, "item")
            if uri is None:
                continue
            qid = _qid(uri)
            row = out.setdefault(qid, {"name": None, "tmdbPersonId": None, "aliases": []})
            name = _value(binding, "itemLabel")
            if name is not None and name != qid:
                row["name"] = name
            if row["tmdbPersonId"] is None:
                row["tmdbPersonId"] = _value(binding, "pid")
        for binding in run("?alias", "  ?item skos:altLabel ?alias . FILTER(LANG(?alias) IN ('en','mul'))"):
            uri, alias = _value(binding, "item"), _value(binding, "alias")
            if uri is None or alias is None:
                continue
            row = out.setdefault(_qid(uri), {"name": None, "tmdbPersonId": None, "aliases": []})
            if alias not in row["aliases"]:
                row["aliases"].append(alias)
    return out


def instance_of(qids, batch=200):
    """`Q-id -> its P31 labels`, for the targets of `basedOn` — what a source work IS, which the bare Q-id
    cannot say.

    A type with no English label comes back labelled with its own Q-id, which is not a type name and would
    fold to `other`. The Swift compared that label with the ITEM's Q-id, which it never is, so the type's
    Q-id is selected here to compare against."""
    out = {}
    for start in range(0, len(qids), batch):
        values = " ".join(f"wd:{q}" for q in qids[start:start + batch])
        query = ("SELECT ?item ?type ?typeLabel WHERE {\n"
                 f"  VALUES ?item {{ {values} }}\n"
                 "  ?item wdt:P31 ?type .\n"
                 '  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }\n'
                 "}")
        for binding in bindings(_sparql(query)):
            uri, label = _value(binding, "item"), _value(binding, "typeLabel")
            if uri is None or label is None or label == _qid(_value(binding, "type") or ""):
                continue
            out.setdefault(_qid(uri), []).append(label)
    return out


def _trim(text):
    """`trimmingCharacters(in: .whitespaces)`: spaces and tabs, never newlines."""
    keep = [i for i, c in enumerate(text) if not (c == "\t" or unicodedata.category(c) == "Zs")]
    return text[keep[0]:keep[-1] + 1] if keep else ""


def source_kind(label):
    low = _trim(label).lower()
    if low in SOURCE_KIND_BY_TYPE:
        return SOURCE_KIND_BY_TYPE[low]
    if any(marker in low for marker in CHARACTER_MARKERS):
        return "character"
    for suffix, kind in SOURCE_SUFFIXES:
        if low.endswith(suffix):
            return kind
    return "other"


def strongest_kind(labels):
    """The strongest kind among a source work's types, or None for a work Wikidata states no type for."""
    kinds = [source_kind(label) for label in labels]
    return min(kinds, key=SOURCE_KINDS.index) if kinds else None


def stripped_article_suffix(title):
    """`"Solaris (1972 film)"` → `"Solaris"`. The enwiki article title is the best English display string
    there is (89% exact against TMDB, against rdfs:label's 86%), but its disambiguator would render
    verbatim on a poster card."""
    opened = title.rfind("(")
    if opened < 0 or not title.endswith(")"):
        return title
    inside = title[opened + 1:-1].lower()
    if not any(marker in inside for marker in ARTICLE_MARKERS):
        return title
    return _trim(title[:opened])


def genre_map(entities):
    """`Q-id -> {"movie": id, "tv": id}` for the genres that match TMDB's vocabulary EXACTLY once the medium
    is stripped. Wikidata is far finer than TMDB's 19 and guessing a parent would file a cyberpunk film under
    Science Fiction on a substring.

    Animation is the one family matched by shape, because it is the only genre with a user-facing hide
    rule behind it: Wikidata spreads it across dozens of items, and an unmapped one means "hide animation"
    silently fails. A live-action/animated hybrid is not what someone hiding animation means.
    """
    out = {}
    for qid, names in entities.items():
        label = names.get("en")
        if label is None:
            continue
        if label != "live-action/animated" and (label == "animated" or label.startswith("animated ")
                                                  or "anime" in label):
            out[qid] = {"movie": 16, "tv": 16}
            continue
        hit = TMDB_GENRES.get(label)
        if hit is None:
            continue
        entry = {name: value for name, value in zip(("movie", "tv"), hit) if value is not None}
        if entry:
            out[qid] = entry
    return out
