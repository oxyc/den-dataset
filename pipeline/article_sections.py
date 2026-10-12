#!/usr/bin/env python3
"""Stable Wikipedia section parsing and Jev state planning."""
from collections import Counter, defaultdict
import hashlib
import json
import re

HEADING = re.compile(r"^(={2,6})\s*(.*?)\s*\1\s*$", re.MULTILINE)


def sha256_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_sections(text, extractor_headings=()):
    """Return a synthetic lead plus every source-order heading with exact body spans.

    IDs are positional because heading text is neither unique nor stable enough to key a diff.  The heading
    occurrence is also retained to make duplicate headings inspectable.
    """
    matches = list(HEADING.finditer(text))
    raw = [("Lead", 1, 0, matches[0].start() if matches else len(text))]
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        raw.append((match.group(2).strip(), len(match.group(1)), start, end))

    occurrences = defaultdict(int)
    selected_left = Counter(extractor_headings or ())
    sections = []
    for index, (heading, level, start, end) in enumerate(raw):
        occurrences[heading] += 1
        body = text[start:end]
        selected = selected_left[heading] > 0
        if selected:
            selected_left[heading] -= 1
        sections.append({
            "id": f"s{index:03d}",
            "heading": heading,
            "headingOccurrence": occurrences[heading],
            "level": level,
            "start": start,
            "end": end,
            "text": body.strip(),
            "textSha256": sha256_text(body),
            "extractorSelected": selected,
        })
    return sections


def target(rec):
    """The work the classify pass asks about. `alsoKnownAs` (the work's other names on Wikidata, among them
    the one the article's language uses) is there only when the row has some: an article in another
    language is headed by its own title, not the English one. A row without it — every row dumped before
    it existed — gives the state it always gave, so a shipped shard still reconstructs to what it recorded.
    """
    requested = {
        "mediaType": "film" if rec["mediaType"] == "movie" else "television program",
        "title": rec.get("title") or "",
        "year": rec.get("year"),
        "tmdbId": rec["tmdbId"],
    }
    if rec.get("alsoKnownAs"):
        requested["alsoKnownAs"] = rec["alsoKnownAs"]
    return requested


def state_for(rec, sections):
    """A structured state whose section paths are referenced verbatim by the questions."""
    return {
        "requestedTarget": target(rec),
        "article": {
            "wikipediaTitle": rec.get("article"),
            "language": rec.get("language"),
            "sections": {
                section["id"]: {"heading": section["heading"], "text": section["text"]}
                for section in sections
            },
        },
    }


def encoded_chars(state):
    return len(json.dumps(state, ensure_ascii=False, separators=(",", ":")))


#: Jev's documented limits (https://docs.typesafe.ai/models.md, Jev 1.13): 64k tokens a request for the state
#: plus every question, and 32k for the state plus the LONGEST single question. The second is the one a state
#: meets first, because the questions of a request are many and short. The longest question is ~1.2k
#: characters (about 400 tokens), so a state of ~31.5k tokens is the most a request can carry; this leaves
#: the rest of that room as margin for an estimate that is a model, not a tokenizer.
MAX_STATE_TOKENS = 28_000
#: Tokens a character costs, by what it is. Fitted on the shipped pass's 47.5k calls (`inputTokens` against
#: `stateChars` per language, 72 tokens a question taken out): English 0.28, German 0.31, French and Italian
#: 0.35, Russian 0.72, Korean 0.86, Japanese 1.10. Rounded UP, because the failure being avoided costs the
#: title its classification and the estimate may be wrong in the other direction only by asking more calls.
LATIN_TOKENS, ALPHABETIC_TOKENS, OTHER_TOKENS = 0.35, 0.85, 1.25
#: Latin-1 is what `str.encode("latin-1", "ignore")` keeps; U+0250-U+058F is the other alphabets that a
#: character carries one sound of (IPA, Greek, Cyrillic, Armenian). Everything else — CJK, Thai, Arabic,
#: Devanagari — is priced as Japanese.
ALPHABETIC = re.compile(r"[ɐ-֏]")


def estimated_tokens(state):
    """What Jev will count the state as, from its characters: no tokenizer is available, and the API only
    reports `usage` after it has answered, or refused with `max_tokens_exceeded` and no number."""
    text = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    latin = len(text.encode("latin-1", "ignore"))
    alphabetic = len(ALPHABETIC.findall(text))
    return latin * LATIN_TOKENS + alphabetic * ALPHABETIC_TOKENS + (len(text) - latin - alphabetic) * OTHER_TOKENS


def fits(state, max_state_chars, max_state_tokens=None):
    """Whether one request can carry the state: under the character ceiling AND, when one is given, under the
    token ceiling.

    Characters alone cannot say: Japanese costs four times English per character, so an article the ceiling
    allows (110k characters) is several times over Jev's limit in Japanese: tv:96451, tv:45799 and tv:36408
    each answered `HTTP 400 max_tokens_exceeded`.

    `max_state_tokens` is None for a shard bought before the token ceiling existed, whose manifest records no
    `maxStateTokens`: the auditor reconstructs what such a shard was asked with the rule it was asked under."""
    return encoded_chars(state) <= max_state_chars and (
        max_state_tokens is None or estimated_tokens(state) <= max_state_tokens)


class SectionTooLarge(ValueError):
    """A single section no request can carry. The title cannot be asked as it stands; it is not a fault in
    the run, so a caller holds the title instead of stopping."""


def section_groups(rec, sections, max_state_chars, max_state_tokens=None):
    """Greedily group complete sections under the state ceiling; never make an invisible-section question."""
    groups = []
    current = []
    for section in sections:
        trial = current + [section]
        if current and not fits(state_for(rec, trial), max_state_chars, max_state_tokens):
            groups.append(current)
            current = [section]
        else:
            current = trial
        if not fits(state_for(rec, current), max_state_chars, max_state_tokens):
            raise SectionTooLarge(
                f"{rec['mediaType']}:{rec['tmdbId']} section {section['id']} exceeds max state by itself; "
                "paragraph fallback is required before launch"
            )
    if current:
        groups.append(current)
    return groups


def is_oversized(rec, sections, max_state_chars, max_state_tokens=None):
    return not fits(state_for(rec, sections), max_state_chars, max_state_tokens)


def select_global_sections(rec, sections, role_answers, max_state_chars, max_state_tokens=None):
    """For an oversized article, retain the strongest useful evidence and preserve source order.

    Role answers come from the prerequisite section-only calls.  The lead is mandatory.  Production and
    reception prose loses to story, theme, and concise work context, which is the reason for the audit.
    """
    lead = sections[0]
    chosen = [lead]

    def utility(section):
        answer = role_answers[section["id"]]
        p = answer["probabilities"]
        return (3.0 * p["story-premise"] + 2.0 * p["theme-subject"]
                + p["work-context"] - 2.0 * p["irrelevant-production-reception-navigation"])

    for section in sorted(sections[1:], key=lambda item: (-utility(item), item["start"])):
        trial = sorted(chosen + [section], key=lambda item: item["start"])
        if fits(state_for(rec, trial), max_state_chars, max_state_tokens):
            chosen = trial
    return sorted(chosen, key=lambda item: item["start"])


def public_section(section, role_answer=None):
    result = {key: section[key] for key in (
        "id", "heading", "headingOccurrence", "level", "start", "end", "textSha256",
        "extractorSelected")}
    result["chars"] = len(section["text"])
    if role_answer is not None:
        result["role"] = {
            "value": role_answer["choice"],
            "confidence": role_answer["confidence"],
            "probabilities": role_answer["probabilities"],
        }
    return result

