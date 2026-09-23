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
    return {
        "mediaType": "film" if rec["mediaType"] == "movie" else "television program",
        "title": rec.get("title") or "",
        "year": rec.get("year"),
        "tmdbId": rec["tmdbId"],
    }


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


def section_groups(rec, sections, max_state_chars):
    """Greedily group complete sections under the state ceiling; never make an invisible-section question."""
    groups = []
    current = []
    for section in sections:
        trial = current + [section]
        if current and encoded_chars(state_for(rec, trial)) > max_state_chars:
            groups.append(current)
            current = [section]
        else:
            current = trial
        if encoded_chars(state_for(rec, current)) > max_state_chars:
            raise ValueError(
                f"{rec['mediaType']}:{rec['tmdbId']} section {section['id']} exceeds max state by itself; "
                "paragraph fallback is required before launch"
            )
    if current:
        groups.append(current)
    return groups


def is_oversized(rec, sections, max_state_chars):
    return encoded_chars(state_for(rec, sections)) > max_state_chars


def select_global_sections(rec, sections, role_answers, max_state_chars):
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
        if encoded_chars(state_for(rec, trial)) <= max_state_chars:
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

