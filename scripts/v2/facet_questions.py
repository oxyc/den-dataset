#!/usr/bin/env python3
"""The nine facet axes, as typed Choice questions, read from `prompts/facets-v1.md`.

The vocabularies are PARSED from the prompt rather than restated here. They already exist in one place, in
the file a human reads, and a second copy is where drift starts — an axis gains a value in the prompt, the
code keeps the old list, and the mismatch shows up as a facet nobody can explain months later.

What the prompt spends paragraphs instructing ("pick from these lists ONLY", "never invent a value", "never
combine two with a slash", "do not put a tone word in `ending`") becomes unrepresentable once the options
are a typed criteria map: a Choice cannot return anything but one of its own keys. The parenthetical glosses
in the prompt — `institution` (school, prison, hospital, barracks) — carry over as each option's
description, which is exactly what `criteria` is for.

`null` in the prompt means "nothing fits". A Choice has no null, so every axis gains an explicit
`does-not-apply`, which the docs recommend anyway ("include `other` or `none of the above` options when the
list might be incomplete"). That is strictly better than the prompt's null: with probabilities attached, a
vocabulary gap becomes measurable instead of silently blank.

Run this file directly to print the questions it would send.
"""
import json
import os
import re

PROMPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts", "facets-v1.md")

# `- **`era`** — when the story is set, not when it was made:` … then values until the next blank line.
_AXIS = re.compile(r"^- \*\*`(?P<axis>[a-z]+)`\*\*\s*[—-]\s*(?P<desc>.+?):\s*$", re.M)
# A value is a backticked token, optionally followed by a parenthetical gloss.
_VALUE = re.compile(r"`(?P<value>[a-z0-9][a-z0-9-]*)`(?:\s*\((?P<gloss>[^)]*)\))?")

NO_FIT = "does-not-apply"


def parse_axes(path=PROMPT):
    """{axis: (description, {value: gloss or None})} in the order the prompt lists them."""
    text = open(path, encoding="utf-8").read()
    starts = [(m.group("axis"), m.group("desc").strip(), m.end()) for m in _AXIS.finditer(text)]
    if not starts:
        raise SystemExit(f"parsed no axes from {path} — the prompt's format changed, fix this parser")
    axes = {}
    for i, (axis, desc, pos) in enumerate(starts):
        end = starts[i + 1][2] if i + 1 < len(starts) else len(text)
        block = text[pos:end]
        block = block[:m.start()] if (m := re.search(r"\n\s*\n", block)) else block
        values = {v.group("value"): (v.group("gloss") or None) for v in _VALUE.finditer(block)}
        if not values:
            raise SystemExit(f"axis `{axis}` parsed no values — fix the parser, do not guess a vocabulary")
        axes[axis] = (desc, values)
    return axes


def questions(path=PROMPT):
    """The axes as a `questions` map ready to POST."""
    out = {}
    for axis, (desc, values) in parse_axes(path).items():
        criteria = {v: gloss for v, gloss in values.items()}
        criteria[NO_FIT] = ("Nothing in this list fits, or the plot does not say. Prefer a real value "
                            "whenever one genuinely applies.")
        out[axis] = {"type": "choice", "instructions": desc, "criteria": criteria}
    return out


if __name__ == "__main__":
    qs = questions()
    for axis, q in qs.items():
        opts = [k for k in q["criteria"] if k != NO_FIT]
        print(f"{axis:<10} {len(opts):>2} options   {q['instructions'][:60]}")
        print(f"           {' · '.join(opts)}")
    print(f"\n{len(qs)} axes, {sum(len(q['criteria']) for q in qs.values())} options in total")
    print(f"question block is {len(json.dumps(qs)):,} chars of input per call")
