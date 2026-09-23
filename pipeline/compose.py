#!/usr/bin/env python3
"""The embedding document — the one part of this pipeline where a wrong answer looks right.

A document assembled slightly differently still embeds, still ranks, and still returns ten plausible
neighbours; nothing downstream can see that the space moved. So this reproduces the Swift composer's
output byte for byte (measured over the whole corpus), including the two places where its string model is
not Python's:

  * **the plot cap counts CHARACTERS as Swift does — extended grapheme clusters, not code points.** A plot
    with combining marks is shorter in Swift's count than in `len()`, so capping by `len()` cuts it
    earlier and composes a different document. `graphemes` implements the cluster rules the corpus's text
    actually exercises (CRLF, combining and spacing marks, joiners, emoji modifiers, flag pairs);
  * **trimming is Foundation's character sets**, not `str.strip`, which also strips U+001C–U+001F.

The shape is the CC0 "lean" document: no title, no year, no cast, every fact clause from Wikidata —
`Created by`, `Genres`, `Themes` (our own tags) and the capped Wikipedia plot. The director clause is
dropped (the shipped store's composition, recovered by re-embedding probes against it), so it is not
composed at all rather than composed and discarded.
"""
import hashlib
import json
import os
import unicodedata

from .articles import key, ordered_batches
from .contract import StageError

#: `CharacterSet.whitespaces` as CoreFoundation implements it: the space separators, TAB — and U+200B ZERO
#: WIDTH SPACE, which is a format character by category and trimmed anyway (measured against the binary).
#: `.whitespacesAndNewlines` adds U+000A–U+000D, U+0085 and the line and paragraph separators.
_SPACES = frozenset("\t     　" + "".join(chr(c) for c in range(0x2000, 0x200C)))
_NEWLINES = frozenset("\n\x0b\x0c\r\x85  ")


def _space(char):
    return char in _SPACES


def _space_or_newline(char):
    return _space(char) or char in _NEWLINES


def _trim(text, test):
    start, end = 0, len(text)
    while start < end and test(text[start]):
        start += 1
    while end > start and test(text[end - 1]):
        end -= 1
    return text[start:end]


def _extends(char):
    """Joins the cluster before it: marks (GB9, GB9a — including Thai and Lao SARA AM, spacing marks that
    are letters by category), ZWNJ, emoji modifiers and tag characters."""
    code = ord(char)
    return (unicodedata.category(char) in ("Mn", "Me", "Mc") or code in (0x200C, 0x0E33, 0x0EB3)
            or 0x1F3FB <= code <= 0x1F3FF or 0xE0020 <= code <= 0xE007F)


#: GB9c (Unicode 15.1): a consonant, a virama and a consonant are ONE character in the six scripts that
#: mark conjuncts this way. The virama per script, and each script's consonant block.
_LINKERS = frozenset("्্્୍్്")
_CONSONANTS = ((0x0915, 0x0939), (0x0958, 0x095F), (0x0978, 0x097F), (0x0995, 0x09A8), (0x09AA, 0x09B0),
               (0x09B2, 0x09B2), (0x09B6, 0x09B9), (0x09DC, 0x09DD), (0x09DF, 0x09DF), (0x09F0, 0x09F1),
               (0x0A95, 0x0AA8), (0x0AAA, 0x0AB0), (0x0AB2, 0x0AB3), (0x0AB5, 0x0AB9), (0x0AF9, 0x0AF9),
               (0x0B15, 0x0B28), (0x0B2A, 0x0B30), (0x0B32, 0x0B33), (0x0B35, 0x0B39), (0x0B5C, 0x0B5D),
               (0x0B5F, 0x0B5F), (0x0B71, 0x0B71), (0x0C15, 0x0C28), (0x0C2A, 0x0C39), (0x0C58, 0x0C5A),
               (0x0D15, 0x0D3A))


def _consonant(char):
    code = ord(char)
    return any(low <= code <= high for low, high in _CONSONANTS)


def _hangul(char):
    """The Hangul syllable type GB6–GB8 join on: leading, vowel and trailing jamo, and the two kinds of
    precomposed syllable."""
    code = ord(char)
    if 0x1100 <= code <= 0x115F or 0xA960 <= code <= 0xA97C:
        return "L"
    if 0x1160 <= code <= 0x11A7 or 0xD7B0 <= code <= 0xD7C6:
        return "V"
    if 0x11A8 <= code <= 0x11FF or 0xD7CB <= code <= 0xD7FB:
        return "T"
    if 0xAC00 <= code <= 0xD7A3:
        return "LV" if (code - 0xAC00) % 28 == 0 else "LVT"
    return None


_HANGUL_JOINS = {"L": ("L", "V", "LV", "LVT"), "V": ("V", "T"), "LV": ("V", "T"), "T": ("T",), "LVT": ("T",)}


def _pictographic(char):
    code = ord(char)
    return unicodedata.category(char) == "So" or 0x1F000 <= code <= 0x1FAFF or 0x2600 <= code <= 0x27BF


def _regional(char):
    return 0x1F1E6 <= ord(char) <= 0x1F1FF


def graphemes(text, stop):
    """Code-point offsets where the first `stop` clusters end, or fewer when the text runs out first."""
    ends, i, n = [], 0, len(text)
    while i < n and len(ends) < stop:
        char = text[i]
        i += 1
        if char == "\r" and i < n and text[i] == "\n":
            i += 1
        elif unicodedata.category(char) in ("Cc", "Zl", "Zp"):
            pass
        else:
            if _regional(char) and i < n and _regional(text[i]):
                i += 1
            hangul, consonant, linked = _hangul(char), _consonant(char), False
            while i < n:
                nxt = text[i]
                if hangul and _hangul(nxt) in _HANGUL_JOINS[hangul]:
                    hangul = _hangul(nxt)
                elif _extends(nxt):
                    hangul, linked = None, linked or (consonant and nxt in _LINKERS)
                elif nxt == "‍":
                    hangul = None
                    if i + 1 < n and _pictographic(text[i + 1]):
                        i += 1
                elif linked and _consonant(nxt):
                    linked = False
                else:
                    break
                i += 1
        ends.append(i)
    return ends


def capped_plot(plot, cap):
    """The plot cut to `cap` characters, back to the last sentence boundary within them, so the document
    reads as prose rather than ending mid-word. Keeps the head only."""
    if len(plot) <= cap:
        return plot
    ends = graphemes(plot, cap + 1)
    if len(ends) <= cap:
        return plot
    head = plot[:ends[cap - 1]] if cap else ""
    stop = head.rfind(". ")
    return head[:stop] + "." if stop >= 0 else head


def lean(creators, genres, tags, plot):
    """`Created by …. Genres: …. Themes: …. Plot: …` — each fact clause only when it has content, and the
    Plot clause always, so a title with no plot still reads as a document."""
    parts = []
    if creators:
        parts.append(f"Created by {', '.join(creators)}.")
    if genres:
        parts.append(f"Genres: {', '.join(genres)}.")
    if tags:
        parts.append(f"Themes: {', '.join(tags)}.")
    parts.append(f"Plot: {_trim(plot, _space_or_newline)}")
    return _trim(" ".join(parts), _space)


def source_plot(row, plot_cap):
    """The plot a title's document carries, exactly as it follows `Plot: `: its Wikipedia plot capped and
    trimmed, or "" for a row with none. A translation's `source_sha256` is the hash of this text, so a
    translation made from any other version of the plot never matches."""
    if not row.get("hasWikiPlot"):
        return ""
    return _trim(capped_plot(row.get("overview") or "", plot_cap), _space_or_newline)


def plot_sha(plot):
    return hashlib.sha256(plot.encode("utf-8")).hexdigest()


def read_translations(path):
    """`{(key, source_sha256): english}` from `tools/translate`'s append-only cache, the later line winning.

    A killed translator can leave its last line torn, which is skipped; an unreadable line anywhere else
    is a damaged file and is refused rather than read as fewer translations."""
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().split("\n")
    out = {}
    for n, line in enumerate(lines, 1):
        if not line:
            continue
        try:
            row = json.loads(line)
            out[(row["key"], row["source_sha256"])] = row["english"]
        except (ValueError, KeyError, TypeError):
            if n == len(lines):
                continue
            raise StageError(f"{path}:{n} is not a translation row: {line[:80]!r}") from None
    return out


def documents(enriched_dir, labels, doc_facts, done, plot_cap, tally, translations=None):
    """`(key, record, document)` for every labelled title not in `done`; `tally["missing"]` counts the
    enriched titles with no label.

    Batches are read NEWEST first and the first occurrence wins, which is newest-wins: 505 keys appear in
    several batches disagreeing about `hasWikiPlot`, and oldest-first would embed One Piece with no plot.

    A plot that is not in English is embedded as its English translation when `translations` holds one
    made from exactly this plot (oxyc/den-dataset#89): untranslated, bge-m3 places those titles by their
    language before their story. A translation of an older plot is ignored and the original is embedded.
    The translation is capped like any plot, because it is often longer than its source.
    """
    seen = set(done)
    for name in reversed(ordered_batches(enriched_dir)):
        with open(os.path.join(enriched_dir, name), encoding="utf-8") as handle:
            batch = json.load(handle)
        for row in batch:
            title = key(row)
            if title in seen:
                continue
            record = labels.get(title)
            if record is None:
                tally["missing"] = tally.get("missing", 0) + 1
                continue
            tags = [item["label"] for item in record["subgenres"]] + [item["label"] for item in record["moods"]]
            # Only a WIKIPEDIA plot, which is CC0-clean; a title with none composes on facts and tags.
            plot = source_plot(row, plot_cap)
            english = translations.get((title, plot_sha(plot))) if translations and plot else None
            if english is not None:
                plot = capped_plot(english, plot_cap)
            facts = doc_facts.get(title) or {}
            seen.add(title)
            # `createdBy` is Wikidata (P170) on a batch the current enrichment wrote; an older batch can still
            # carry TMDB `created_by` names where Wikidata had none, until it is enriched again.
            yield title, record, lean(row.get("createdBy") or [], facts.get("genres") or [], tags, plot)
