#!/usr/bin/env python3
"""A title's PLOT out of its Wikipedia article — the EMBEDDER's view, and deliberately the opposite of the
classifier's in `lib/wikipedia.py`.

The embedder gets the story and nothing else, because embedding a Reception section clusters titles by
critical consensus rather than by what happens in them. So an article is split into its sections and each
heading is classified — story, theme, or excluded — and only the first two are kept, story first so a
caller trimming to a budget drops the weaker evidence rather than the end of the plot.

Two sources. With a Wikimedia Enterprise bearer the pre-sectioned structured-contents endpoint is asked
first; otherwise, and whenever it has nothing, the free action API's wikitext is split locally. They are
not interchangeable in what they RECORD: Enterprise returns sections and no page title, so it reports no
revision id and no resolved article, and cannot see a redirect. Enterprise is never cached — it is the
fresh path by construction — and the action API always is, through `lib/wikipedia.py`'s key.

Every plot says which of the two served it (`source`), because which was ASKED is not which answered: a
throttled or expired bearer falls back title by title, and `lib/enterprise` stops asking at all once the
account's month is spent or its breaker trips.
"""
import json
import urllib.parse

from . import enterprise
from . import http
from . import wikipedia

#: Plot headings per language, for the Wikipedias a title with no English article falls back to.
#:
#: Each list is that Wikipedia's own house style rather than a translation of "Plot": de uses `Handlung`,
#: it `Trama`, es `Argumento`/`Sinopsis`, ru `Сюжет`. Six languages covered essentially all of a 70-title
#: sample. Welsh (`cy`) is deliberately ABSENT despite appearing more often than Italian or French there:
#: that frequency is the signature of bot-generated stubs, and including it adds articles with no prose and
#: a lot of false confidence. The keys are also the wikis the Wikidata mapping asks for sitelinks on.
HEADINGS_BY_LANGUAGE = {
    "de": ("handlung", "inhalt"),
    "it": ("trama",),
    "es": ("argumento", "sinopsis", "trama"),
    "fr": ("synopsis", "résumé", "intrigue"),
    "nl": ("verhaal", "plot"),
    "ru": ("сюжет",),
    "pt": ("sinopse", "enredo"),
    "ja": ("あらすじ", "ストーリー"),
    "ko": ("줄거리", "시놉시스"),
    "sv": ("handling",),
    "pl": ("fabuła",),
    "da": ("handling",),
}

#: Section titles that carry the plot, in preference order. "Premise"/"Storyline" are what most series
#: articles use (films favour "Plot"); "Brief summary" is a film spot check (Very Happy Alexander).
PLOT_NAMES = ("plot", "plot summary", "synopsis", "storyline", "premise", "story", "summary",
              "brief summary")

#: What an anthology or documentary says where a narrative film says "Plot" — Fantasia 2000's "Segments",
#: Jodorowsky's Dune's "Content". Ranked below every name above.
PLOT_FALLBACK_NAMES = ("segments", "content")

#: An INSTALMENT of a serial. A long-running series has no "Plot" section: The Wire's 22,892 characters of
#: plot sit under `Season 1 (2002)` … `Season 5 (2008)`, and taking the first plot-named section returned
#: nothing for it and for the rest of serial television.
SERIAL_PREFIXES = ("season", "series", "part", "volume", "arc", "chapter", "episode")

#: What the work is ABOUT rather than what happens in it. Deliberately narrow: "Style" is not here, because
#: The Wire's `Style > Realism` is 2,618 characters about the writers' research. "Overview" is on
#: measurement — five of ten series that still yielded nothing led with it (Stranger Things among them).
#: "Characters" is the sketch show's premise, "Format" the unscripted show's. "History" and "Episodes" were
#: considered and rejected: the first is business history (The Smurfs' merchandising rights), the second a
#: wikitable that cleans to nothing.
THEME_NAMES = ("themes", "setting", "concept", "premise and production", "characters and setting",
               "social commentary", "overview", "characters", "series overview", "format", "show format")

#: Headings whose subtree is NEVER the work, whatever a child is called: reception and analysis are about
#: it from outside. Harry & Meghan filed four `Volume I`/`Volume II` headings under `Critical response` and
#: `Veracity of claims`, and letting a story leaf win there put 18,141 characters of reviews in as plot.
HARD_EXCLUDED = ("reception", "critical", "review", "awards", "accolades", "ratings", "viewership",
                 "audience", "response", "responses", "veracity", "controversy", "criticism", "analysis of",
                 "legal", "box office", "broadcast", "release", "home media", "marketing", "merchandis",
                 "legacy", "in popular culture", "references", "external links", "see also",
                 "further reading", "notes", "bibliography", "sources", "cite", "impact", "public opinion")

#: Usually not the work, but ORGANISATIONAL — a plot can sit inside one. The Wire files its five season
#: summaries under `Cast and characters`, 22,892 characters of plot behind a heading that reads as a cast
#: list, so a story leaf outranks these.
SOFT_EXCLUDED = ("production", "development", "filming", "casting", "cast", "crew", "music", "soundtrack",
                 "distribution", "adaptations", "sequel", "prequel", "spin-off", "history")

_ORDINALS = ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "i", "ii",
             "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "first", "second", "third", "fourth", "fifth")

STORY, THEME, EXCLUDED = "story", "theme", "excluded"

ENTERPRISE_HOST = "api.enterprise.wikimedia.com"
ENTERPRISE_PATH = "/v2/structured-contents/"
#: English only: the other-language fallback reads the action API of that language's Wikipedia.
ENTERPRISE_BODY = b'{"filters":[{"field":"is_part_of.identifier","value":"enwiki"}],"limit":1}'
#: Foundation's `urlPathAllowed`, which is how the Swift pass spelled a title into the path.
_PATH_SAFE = "!$&'()*+,-./:;=@_~"

#: Which source served a plot.
ENTERPRISE, ACTION_API = "enterprise", "action-api"


def compared(line):
    """A heading reduced to comparable text: markup gone, entities resolved, lowercased."""
    return wikipedia.display_heading(line).lower()


def plot_rank(line):
    """Rank of a plot heading in preference order, or None.

    Spaced by ten so a qualified "Plot and background" slots in just under the two exact Plot spellings
    and above "Synopsis" and "Summary" — appending it after every exact name put it below "Summary", the
    weaker-heading-wins defect this rank exists to prevent.
    """
    heading = compared(line)
    if heading in PLOT_NAMES:
        return PLOT_NAMES.index(heading) * 10
    if heading.startswith("plot "):
        return 15
    if heading in PLOT_FALLBACK_NAMES:
        return len(PLOT_NAMES) * 10 + PLOT_FALLBACK_NAMES.index(heading)
    return None


def is_serial(heading):
    """"Season 1 (2002)", "Series 2", "Part One", bare "Episodes" — but not "Episode structure".

    The serial word alone is not enough: "Episode structure" is 1,622 characters of production prose under
    The Wire's `Production`. The word must stand alone or be followed by an instalment number.
    """
    words = [word for word in heading.split(" ") if word]
    # A leading article is noise: The Storyteller heads its anthology "The episodes".
    if len(words) > 1 and words[0] in ("the", "a", "an"):
        words = words[1:]
    if not words:
        return False
    raw = words[0]
    # "Seasons" de-pluralises to "season", but "Series" must not become "serie" — stripping the s
    # unconditionally dropped every British series article, 16,679 characters of Misfits among them.
    singular = raw[:-1] if raw.endswith("s") else raw
    if raw not in SERIAL_PREFIXES and singular not in SERIAL_PREFIXES:
        return False
    if len(words) == 1:
        return True
    following = words[1]
    return following[0].isnumeric() or following.strip(":(),.") in _ORDINALS


def _theme(heading):
    return any(heading == name or heading.startswith(name + " ") for name in THEME_NAMES)


def _excluded(heading):
    return any(heading.startswith(prefix) for prefix in HARD_EXCLUDED + SOFT_EXCLUDED)


def section_kind(line, parent=None):
    """What a heading's prose is about, given the heading enclosing it.

    Neither the leaf nor the parent is enough alone, and The Wire shows each direction: its seasons sit
    under `Cast and characters` (a story leaf must outrank an organisational parent), `Institutional
    dysfunction` sits under `Themes` (a theme parent is inherited), and `Realism` under `Style` is
    production (an unrecognised leaf under an unrecognised parent stays out).
    """
    heading = compared(line)
    up = compared(parent) if parent is not None else None
    # A hard-excluded ancestor beats everything, even a story leaf.
    if up is not None and any(up.startswith(prefix) for prefix in HARD_EXCLUDED):
        return EXCLUDED
    if plot_rank(line) is not None or is_serial(heading):
        return STORY
    if _excluded(heading):
        return EXCLUDED
    # Inheritance, both ways: a child of Plot is plot even when named "Act II" — dropping those loses the
    # back half of any film whose plot is broken into acts — and a child of Themes is thematic.
    if up is not None:
        if plot_rank(parent) is not None or is_serial(up):
            return STORY
        if _theme(up):
            return THEME
        if _excluded(up):
            return EXCLUDED
    return THEME if _theme(heading) else EXCLUDED


def describing_prose(wikitext):
    """Every section describing the work, story before theme, as `(text, headings)`."""
    by_level, story, theme = {}, [], []
    for heading, level, body in wikipedia.split_sections(wikitext):
        if not heading:
            continue
        # Deliberately never cleared when a shallower heading appears, as the Swift pass did it: the
        # parent is the nearest heading ABOVE this level that has been seen.
        by_level[level] = heading
        parent = next((by_level[up] for up in range(level - 1, 1, -1) if up in by_level), None)
        prose = wikipedia.clean_wikitext(body)
        if not prose:
            continue
        kind = section_kind(heading, parent)
        if kind != EXCLUDED:
            (story if kind == STORY else theme).append((wikipedia.display_heading(heading), prose))
    kept = story + theme
    return "\n\n".join(prose for _name, prose in kept), [name for name, _prose in kept]


class NoPage(Exception):
    """The wiki has no page by that name: `missingtitle`, or `invalidtitle` for a name that cannot be one.

    The action API says so with a 200 carrying an error envelope, never a 404, so this is how a stale
    sitelink arrives. It is an ANSWER — asking again gets it again — and it is about the article's existence,
    not its sections, so it must not read as "no plot section" the way an envelope parsed to nothing did.
    """


#: The error codes that mean the page does not exist, rather than that the request went wrong.
NO_PAGE = ("missingtitle", "invalidtitle")


def _parsed(article, language, cache):
    """The action-API `parse` object, or None where the page has no wikitext to read. Raises `NoPage`.

    A body that is not JSON is no plot rather than an error: the Swift pass decoded it to nothing, and a
    non-JSON 200 is not something a retry fixes.
    """
    try:
        body = wikipedia.fetch_parse(article, language, cache)
    except ValueError:
        return None
    return _parse_object(body, article, language)


def _parse_object(body, article, language):
    """The `parse` object out of one `action=parse` body, or None. Raises `NoPage`."""
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict) and error.get("code") in NO_PAGE:
        raise NoPage(f"{language}.wikipedia.org has no page {article!r} ({error['code']})")
    parsed = body.get("parse") if isinstance(body, dict) else None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("wikitext"), str):
        return None
    return parsed


def _found(text, sections, parsed, article, language):
    revid = parsed.get("revid")
    title = parsed.get("title")
    # The RESOLVED article: `redirects=1` is sent, so a redirect's content and revid are the target's, and
    # storing the redirect's name beside them would make a revision refresh compare two different pages.
    return {"text": text, "revId": revid if isinstance(revid, int) and not isinstance(revid, bool) else None,
            "resolvedArticle": title if isinstance(title, str) else article,
            "sections": sections, "language": language, "source": ACTION_API}


def action_api_plot(article, cache=None):
    """The English article's describing sections, read from ONE `action=parse` request.

    One request for the whole article, split locally: fetching per section is a round trip each against a
    rate-limited API, for an article that arrives in one.
    """
    parsed = _parsed(article, "en", cache)
    return None if parsed is None else _english(parsed, article)


def _english(parsed, article):
    text, sections = describing_prose(parsed["wikitext"])
    return _found(text, sections, parsed, article, "en") if text else None


def other_language_plot(article, language, cache=None):
    """The same extraction on another Wikipedia, where only the heading names differ.

    A language with no heading list is None rather than a guess: a wrong list yields production prose that
    reads like a plot to anyone who cannot check it.
    """
    if language not in HEADINGS_BY_LANGUAGE:
        return None
    parsed = _parsed(article, language, cache)
    return None if parsed is None else _other_language(parsed, article, language)


def _other_language(parsed, article, language):
    headings = HEADINGS_BY_LANGUAGE[language]
    kept = []
    for heading, _level, body in wikipedia.split_sections(parsed["wikitext"]):
        name = compared(heading)
        if not any(name == wanted or name.startswith(wanted + " ") for wanted in headings):
            continue
        prose = wikipedia.clean_wikitext(body)
        if prose:
            kept.append((wikipedia.display_heading(heading), prose))
    if not kept:
        return None
    return _found("\n\n".join(prose for _name, prose in kept), [name for name, _prose in kept], parsed,
                  article, language)


def cached_plot(article, language, cache):
    """The plot the action-API body ALREADY ON DISK for this article yields, or None — never a request.

    What a revision backfill needs: a title grounded before its revision was recorded can only be tied to
    one by the body it was read from, and a live read would answer with today's revision instead. None
    when there is no cache, no body, or a body with nothing in it — the caller has learnt nothing.
    """
    if cache is None:
        return None
    hit = cache.read(cache.key(f"{language}.wikipedia.org{wikipedia.API_PATH}",
                               dict(wikipedia.PARSE_QUERY, page=article)))
    if hit is None:
        return None
    try:
        parsed = _parse_object(json.loads(hit.decode("utf-8")), article, language)
    except (ValueError, NoPage):
        return None
    if parsed is None:
        return None
    if language == "en":
        return _english(parsed, article)
    return _other_language(parsed, article, language) if language in HEADINGS_BY_LANGUAGE else None


def _section(value):
    """An Enterprise section, held to the shape the Swift decoder required — or ValueError."""
    if not isinstance(value, dict):
        raise ValueError("an Enterprise section is not an object")
    for field in ("name", "value"):
        if value.get(field) is not None and not isinstance(value[field], str):
            raise ValueError(f"an Enterprise section's `{field}` is not a string")
    parts = value.get("has_parts")
    if parts is not None and not isinstance(parts, list):
        raise ValueError("an Enterprise section's `has_parts` is not a list")
    return value


def _own_paragraphs(section):
    """A section's OWN prose: its value plus its direct unnamed parts, never a nested section's.

    Flattening the subtree here credited a whole `Themes` branch to "Themes" and hid whatever was excluded
    inside it; the recursion belongs to the caller, which classifies each heading separately.
    """
    parts = []
    value = (section.get("value") or "").strip()
    if value:
        parts.append(value)
    for child in section.get("has_parts") or []:
        _section(child)
        if not child.get("name"):
            text = (child.get("value") or "").strip()
            if text:
                parts.append(text)
    return "\n".join(parts)


def enterprise_prose(payload):
    """Every section describing the work, from a structured-contents payload. None when there is none.

    The same classifier and parent rules as the wikitext path, with nesting supplying the parent. Taking
    the first Plot section and stopping — as this once did — would have undone the broader extraction for
    every title whenever a bearer was held, since this path is tried first.
    """
    articles = json.loads(payload.decode("utf-8"))
    if not isinstance(articles, list):
        raise ValueError("an Enterprise payload is not a list of articles")
    story, theme = [], []

    def walk(section, parent):
        _section(section)
        name = section.get("name") or ""
        if name:
            own = _own_paragraphs(section)
            if own:
                kind = section_kind(name, parent)
                if kind != EXCLUDED:
                    (story if kind == STORY else theme).append((wikipedia.display_heading(name), own))
        for child in section.get("has_parts") or []:
            walk(child, name or parent)

    for article in articles:
        if not isinstance(article, dict):
            raise ValueError("an Enterprise article is not an object")
        sections = article.get("sections")
        if sections is not None and not isinstance(sections, list):
            raise ValueError("an Enterprise article's `sections` is not a list")
        for section in sections or []:
            walk(section, None)
    kept = story + theme
    if not kept:
        return None
    return "\n\n".join(prose for _name, prose in kept), [name for name, _prose in kept]


def enterprise_plot(article, token):
    """The Enterprise answer, or None — on ANY failure, so the caller falls back to the action API.

    Best-effort by design: the free API has the same coverage, and a failed or empty fast path must cost a
    slower fetch rather than a title. None without a request when `lib/enterprise` says not to ask — the
    month's quota is spent, or the breaker is off.

    Asked ONCE: with the action API behind it, waiting out a retry schedule — or a `Retry-After: 5`, which
    cost 15s per candidate — is never cheaper than asking the action API.
    """
    if not enterprise.gate.reserve(token):
        return None
    path = ENTERPRISE_PATH + urllib.parse.quote(article, safe=_PATH_SAFE)
    try:
        payload = enterprise.gate.sent(
            http.request, ENTERPRISE_HOST, path, method="POST", body=ENTERPRISE_BODY,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, attempts=1)
        found = enterprise_prose(payload)
    except (http.HTTPError, ValueError):
        return None
    if found is None:
        return None
    # No revision and no resolved article: the endpoint names no page. Echoing the requested title back
    # would read as "no redirect happened" on the one path that cannot tell.
    return {"text": found[0], "revId": None, "resolvedArticle": None, "sections": found[1],
            "language": "en", "source": ENTERPRISE}


def plot(article, language="en", cache=None, token=None):
    """A plot for one article on one Wikipedia, or None when it has no describing section. Raises `NoPage`
    when the wiki has no such page."""
    if language != "en":
        return other_language_plot(article, language, cache)
    if token:
        found = enterprise_plot(article, token)
        if found is not None:
            return found
    return action_api_plot(article, cache)
