#!/usr/bin/env python3
"""A whole Wikipedia article, as prose, with its headings kept.

This is the CLASSIFIER's view of an article, and it is deliberately the opposite of the embedder's. The
embedder gets the story and nothing else, because embedding a Reception section clusters titles by
critical consensus rather than by what happens in them. A classifier wants everything: the section rules
are a heuristic and they mis-fire, and pre-filtering with a weaker mechanism to protect a stronger one is
backwards. More importantly the LEAD is the only thing that says what the article IS — given an extracted
plot, nothing can tell that prose about Heathcliff came from the novel's page rather than a film's, which
is how six tmdbIds came to share 46,936 characters of Wuthering Heights. Given the article, the first
sentence settles it.

Headings stay inline as `== Heading ==` so a reader can ask which sections are the story without a second
request. The structure is the point, not noise to strip.

One request per article: wikitext, revid and the resolved title come down together, and the article is
split locally. Fetching per section would be six round trips against a rate-limited API for an article
that arrives in one.
"""
import json
import re

from . import cache as caching
from . import http

API_PATH = "/w/api.php"

#: `action=parse` with everything the caller needs on one response. Reproduced exactly from the pass that
#: grounded the shipped corpus: the cache key covers the whole query, so one extra or missing parameter
#: hashes to a different file and silently re-fetches ~80k articles.
PARSE_QUERY = {"action": "parse", "prop": "wikitext|revid", "format": "json",
               "formatversion": "2", "redirects": "1"}

#: `(heading, level, body)`. The backreference makes the closing run match the opening one, so a `===`
#: heading is not closed by a stray `==`.
SECTION = re.compile(r"(?m)^(={2,6})\s*(.+?)\s*\1\s*$")

#: The handful of HTML entities that actually turn up in article wikitext, decoded to their glyphs.
#: `&amp;` is LAST so an already-encoded `&amp;nbsp;` is not double-decoded into a stray space.
ENTITIES = (("&nbsp;", " "), ("&ndash;", "–"), ("&mdash;", "—"), ("&quot;", '"'),
            ("&apos;", "'"), ("&#39;", "'"), ("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"))

#: The same set `displayHeading` used — a heading arrives wrapped in markup whenever the page needs
#: anchors or directionality handling, and comparing the raw string silently dropped those articles.
HEADING_ENTITIES = (("&amp;", "&"), ("&quot;", '"'), ("&#039;", "'"), ("&apos;", "'"),
                    ("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " "))

_TAG = re.compile(r"<[^>]+>")
_STEPS = (
    (re.compile(r"<!--[\s\S]*?-->"), ""),                      # HTML comments
    (re.compile(r"<ref[^>]*?/>"), ""),                          # self-closing <ref …/>
    (re.compile(r"<ref[^>]*?>[\s\S]*?</ref>"), ""),             # <ref>…</ref>, possibly multi-line
    (re.compile(r"<[^>]+>"), ""),                               # any remaining HTML tags
    (re.compile(r"={2,}[^=\n]+={2,}"), ""),                     # section headings
)
_TABLE = re.compile(r"\{\|(?:(?!\{\|)[\s\S])*?\|\}")
_TEMPLATE = re.compile(r"\{\{[^{}]*\}\}")
_LINKS = (
    (re.compile(r"\[\[(?:File|Image):[^\[\]]*\]\]"), ""),       # media links
    (re.compile(r"\[\[[^\[\]|]*\|([^\[\]]*)\]\]"), r"\1"),      # [[a|b]] → b
    (re.compile(r"\[\[([^\[\]]*)\]\]"), r"\1"),                 # [[a]]   → a
    (re.compile(r"\[https?://[^\s\]]+\s+([^\]]*)\]"), r"\1"),   # [http://… text] → text
    (re.compile(r"\[https?://[^\]]*\]"), ""),                   # bare [http://…]
    (re.compile(r"\[\d+\]"), ""),                               # [1]-style ref markers
)
_WHITESPACE = (
    (re.compile(r"[ \t]+"), " "),
    (re.compile(r" *\n *"), "\n"),
    (re.compile(r"\n{2,}"), "\n"),
)


def display_heading(line):
    """A heading with its markup gone but its case intact — what gets recorded as provenance.

    The Rifleman's heading arrives as two `<span class="anchor">` elements followed by the word, so
    storing the raw line is not an option; and "Season 1 (2002)" is what a person reading the provenance
    needs, which is why this is separate from the lowercased form a comparison would use.
    """
    text = _TAG.sub("", line)
    for entity, glyph in HEADING_ENTITIES:
        text = text.replace(entity, glyph)
    return text.strip()


def split_sections(wikitext):
    """Raw article wikitext as `(heading, level, body)`, lead first.

    The lead carries no heading and is yielded with an empty one; every later entry's body runs to the
    start of the next heading.
    """
    matches = list(SECTION.finditer(wikitext))
    if not matches:
        return [("", 1, wikitext)]
    out = [("", 1, wikitext[:matches[0].start()])]
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(wikitext)
        out.append((match.group(2), len(match.group(1)), wikitext[match.end():end]))
    return out


def clean_wikitext(text):
    """Wiki markup stripped to plain prose, whitespace collapsed."""
    for pattern, replacement in _STEPS:
        text = pattern.sub(replacement, text)
    # Tables first, so a table's inner templates go with it; innermost-first, because a match contains no
    # nested `{|` before its `|}`, so a table nested in a cell unwinds outward.
    while True:
        text, count = _TABLE.subn("", text, count=1)
        if not count:
            break
    while True:
        text, count = _TEMPLATE.subn("", text, count=1)
        if not count:
            break
    for pattern, replacement in _LINKS:
        text = pattern.sub(replacement, text)
    for emphasis in ("'''''", "'''", "''"):
        text = text.replace(emphasis, "")
    for entity, glyph in ENTITIES:
        text = text.replace(entity, glyph)
    for pattern, replacement in _WHITESPACE:
        text = pattern.sub(replacement, text)
    return text.strip()


def is_parse_result(body):
    """True when an action-API body is a successful `parse` rather than an error envelope.

    The action API reports a missing page or a bad parameter as a 200 carrying `{"error":{…}}`, so caching
    on status alone would pin `missingtitle` for the whole TTL and make a transient outage look like a
    permanently plotless title.
    """
    return isinstance(body, dict) and "error" not in body and "parse" in body


def fetch_parse(article, language="en", cache=None):
    """The raw `action=parse` body for one article, from disk where it is already there."""
    host = f"{language}.wikipedia.org"
    query = dict(PARSE_QUERY, page=article)
    key = None
    if cache is not None:
        # The HOST is part of the key deliberately. Every Wikipedia serves `/w/api.php`, so leaving it out
        # was a silent correctness bug rather than a missed hit: a request for the Italian "Iago (film)"
        # had the same path and query as the English one and was handed the ENGLISH body. The
        # multilingual fallback then saw an article with no plot section and gave up.
        key = cache.key(f"{host}{API_PATH}", query)
        hit = cache.read(key)
        if hit is not None:
            try:
                return json.loads(hit.decode("utf-8"))
            except ValueError:
                pass
    payload = http.request(host, API_PATH, query)
    body = json.loads(payload.decode("utf-8"))
    if key is not None and is_parse_result(body):
        cache.write(key, payload)
    return body


def article_prose(article, language="en", cache=None):
    """The whole article as prose, or None when it has none.

    Returns the text, the revision it was read at, the page the fetch actually LANDED on (we send
    `redirects=1`, and storing the redirect's name beside the target's revid would make a later bulk-revid
    refresh compare against a page that effectively never changes), and the headings in order.
    """
    body = fetch_parse(article, language, cache)
    parsed = body.get("parse") if isinstance(body, dict) else None
    wikitext = (parsed or {}).get("wikitext")
    if not isinstance(wikitext, str):
        return None

    parts, headings = [], []
    for heading, _level, section in split_sections(wikitext):
        prose = clean_wikitext(section)
        if not prose:
            continue
        if not heading:
            parts.append(prose)                      # the lead, which carries what the article IS
        else:
            name = display_heading(heading)
            headings.append(name)
            parts.append(f"== {name} ==\n{prose}")
    if not parts:
        return None
    return {"text": "\n\n".join(parts), "revId": parsed.get("revid"),
            "resolvedArticle": parsed.get("title") or article,
            "sections": headings, "language": language}


def cache_for(env=None):
    """The `wiki` namespace, or None when it is switched off."""
    return caching.wiki(env)
