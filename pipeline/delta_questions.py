#!/usr/bin/env python3
"""The delta question set: what `combined-v1-r2` did not ask.

Same shape as `combined_questions.py` — typed questions keyed by id — so the existing runner can carry
them. See `scripts/v2/JEV-QUESTIONS.md` for what is deliberately NOT here and why.

The wording below is the whole of the design. Each group has one trap it is written to avoid, named in its
comment, because a question that falls into it costs the corpus state to discover.
"""

# ---------------------------------------------------------------------------------------------------
# subject_of_critique — the only proven gap.
#
# THE TRAP: asking where a work is SET instead of what it ARGUES. `setting = institution` already exists,
# covers 1,064 tv titles, and its members include Night Court, Saved by the Bell and Are You Being Served?.
# A question that reproduces that list has spent the corpus state on a location field we already have.
#
# The single brake is "merely SET in one does not count". A first draft added two more — "as one of its
# SUBJECTS" and "the article must support this; do not infer" — and measured far too cold: `institution`
# fired above 0.70 on 1 title in 500, with The Wire's own 0.64 at the TOP of the distribution. Three brakes
# on one question is one instruction telling the model to say no.
#
# Nouls, not a Choice: The Wire critiques policing AND the justice system AND class, and a Choice would
# split the mass so all three read weak.
CRITIQUE = {
    "institution": "a large organisation as a system — its hierarchy, incentives and self-preservation",
    "the-state": "government, bureaucracy or the machinery of public power",
    "capitalism-or-market": "commerce, profit, labour or economic power",
    "justice-system": "courts, prisons, sentencing or the law as an apparatus",
    "policing": "police forces, their conduct, culture or authority",
    "war-or-military": "war, the armed forces or their conduct",
    "media": "journalism, broadcasting, publicity or the press",
    "religion": "faith, clergy or religious institutions",
    "family": "the family as an institution rather than as this story's characters",
    "class": "social class, poverty or inequality",
    "race": "race, racism or ethnic power",
    "gender": "gender, sexism or patriarchy",
    "education": "schools, universities or teaching",
    "healthcare": "medicine, hospitals or care systems",
    "technology": "technology's effect on people or society",
    "colonialism": "empire, occupation or colonial power",
    "the-self": "identity, memory or the nature of the self",
}

SUBJECT_OF_CRITIQUE = {
    f"critique__{key}": {
        "type": "noul",
        "instructions": (
            f"Is {gloss} one of the requested work's significant themes — something it explores, examines "
            f"or takes a view about? A work merely SET in one, or featuring one incidentally, does not "
            f"count: the work must engage with it."
        ),
    }
    for key, gloss in CRITIQUE.items()
}

# ---------------------------------------------------------------------------------------------------
# Depiction — Nouls, never Scores.
#
# THE TRAP: conflating subject matter with depiction, and asking for a number the article cannot supply.
# "A film about drug dealing" and "a film that shows drug use" are different claims. A Score would be a
# genre transform — crime 0.7, family 0.1 — because a model asked for a number always produces one.
#
# No profanity question at all: a plot summary essentially never describes dialogue register.
DEPICTS = {
    "graphic_violence": "violence shown in graphic or sustained detail",
    "sexual_content": "sexual activity or nudity",
    "drug_use": "characters using drugs or alcohol to excess",
    "self_harm": "suicide or self-harm",
    "animal_harm": "harm to animals",
}

DEPICTION = {
    f"depicts__{key}": {
        "type": "noul",
        "instructions": (
            f"Does the article state or clearly describe that the requested work DEPICTS {gloss} on screen? "
            f"Depiction, not subject: a work about addiction that never shows use answers no. Answer from "
            f"the article only — if it does not say, answer no rather than inferring from genre."
        ),
    }
    for key, gloss in DEPICTS.items()
}

DEPICTION["intended_to_frighten"] = {
    "type": "noul",
    "instructions": (
        "Is the requested work intended to frighten or unsettle its audience, as a purpose rather than an "
        "incidental effect? A tense thriller is not necessarily this; a horror film is. Answer from the "
        "article only."
    ),
}

# ---------------------------------------------------------------------------------------------------
# Audience intent — distinct from content safety.
#
# THE TRAP: reading "contains nothing objectionable" as "made for children". A slow French drama is
# harmless to an eight-year-old and is for nobody's eight-year-old.
AUDIENCE = {
    "made_for_children": {
        "type": "noul",
        "instructions": (
            "Was the requested work MADE FOR children — its intended audience, as the article describes it? "
            "Judge intent, not harmlessness: a gentle arthouse drama with no objectionable content was not "
            "made for children."
        ),
    },
    "made_for_teens": {
        "type": "noul",
        "instructions": (
            "Was the requested work made primarily for a teenage audience? Judge intent, not whether teens "
            "appear in it and not whether teens might enjoy it."
        ),
    },
}

# ---------------------------------------------------------------------------------------------------
# Animation technique — Nouls, because stop-motion IS animated and a documentary is also live-action.
#
# THE TRAP: an exclusive Choice forcing the model to arbitrate a subset relation. `animated` is currently a
# hard gate that starves animated anchors — Spirited Away returns 10 neighbours of 20, Inside Out 8 —
# and softening it needs a better input than a boolean: "anime vs Pixar vs Aardman" is what a viewer means.
TECHNIQUES = {
    "hand_drawn": "traditional hand-drawn or cel animation",
    "cg_animation": "computer-generated 3D animation",
    "stop_motion": "stop-motion, claymation or model animation",
    "anime": "Japanese animation, in the style the term denotes",
    "puppetry": "puppets, marionettes or animatronics",
    "rotoscope": "rotoscoping or motion-capture-driven animation",
    "archival_footage": "archival or documentary footage as a principal element",
    "live_action": "live action — filmed performers rather than animation",
}

TECHNIQUE = {
    f"technique__{key}": {
        "type": "noul",
        "instructions": (
            f"Does the requested work substantially use {gloss}? More than one may be true — a live-action "
            f"film with an animated sequence is both. Answer from the article only."
        ),
    }
    for key, gloss in TECHNIQUES.items()
}


def delta_questions():
    """Every question the delta pass adds, keyed by id."""
    out = {**SUBJECT_OF_CRITIQUE, **DEPICTION, **AUDIENCE, **TECHNIQUE}
    if len(out) != len(SUBJECT_OF_CRITIQUE) + len(DEPICTION) + len(AUDIENCE) + len(TECHNIQUE):
        raise ValueError("delta question id collision")
    return out


if __name__ == "__main__":
    import json

    qs = delta_questions()
    print(json.dumps({"count": len(qs), "ids": sorted(qs)}, indent=1))
