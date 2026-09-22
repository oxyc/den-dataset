#!/usr/bin/env python3
"""Canonical typed questions for the combined Jev corpus pass.

The controlled vocabulary is read from the committed file rather than copied.  Regional labels are
deliberately excluded: they describe origin/language and are derived from metadata, not inferred from prose.
"""
import json
import os
import re

from pipeline.facet_questions import questions as facet_questions
from run_facets import VALIDITY

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
#: The genres & moods vocabulary, as data: JSON under `data/` with the other committed inputs
#: (oxyc/den-dataset#27). The constant, the `--taxonomy` flag and the manifest keys `taxonomy`,
#: `taxonomySha256` and `taxonomyVersion` keep their names — shipped manifests and the store carry them,
#: and a recorded key that is renamed is a key its readers no longer find.
TAXONOMY = os.path.join(ROOT, "data", "genres-moods-vocabulary.json")
PROMPT = os.path.join(ROOT, "data", "prompts", "facets-v2.md")
PINNED_MODEL = "jev-1.13.0"

#: The label families, in the order the file writes them. `version` is read beside them.
FAMILIES = ("primaryGenres", "subgenres", "thematic", "regional", "moods")


def taxonomy(path=TAXONOMY):
    """The vocabulary as `{version, <family>: [label, …]}`, refusing a file that cannot mean one thing.

    Every question the paid pass asks is built from these names, so a family that is absent or a label
    written twice has to stop here: the second spelling would either collide with the first question id
    or quietly ask the same question under two names.
    """
    with open(path, encoding="utf-8") as fh:
        document = json.load(fh)
    version = document.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError(f"{path} has no version")
    vocabulary = {"version": version}
    seen = {}
    for family in FAMILIES:
        labels = document.get(family)
        if not isinstance(labels, list) or not labels or not all(
                isinstance(label, str) and label for label in labels):
            raise ValueError(f"{path} has no {family} labels")
        for label in labels:
            if label in seen:
                raise ValueError(f"{path}: {label!r} is in both {seen[label]} and {family} — a label is "
                                 f"written once, in the family it belongs to")
            seen[label] = family
        vocabulary[family] = labels
    return vocabulary


def slug(value):
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


APPLICABILITY = {
    "narrative_applicability": {
        "type": "choice",
        "instructions": (
            "Classify the requested screen work's narrative/content form from the supplied article. This is "
            "an applicability gate, not a genre or quality judgment."
        ),
        "criteria": {
            "bounded-fictional-narrative": (
                "A fictional or dramatized work with one dominant bounded story arc, including a film, "
                "miniseries, or concluded serial centered on one arc."
            ),
            "open-or-multi-arc-narrative": (
                "An ongoing or long-running scripted series, soap, or work with several comparably central arcs."
            ),
            "anthology": "Separate stories, casts, installments, or segments rather than one dominant arc.",
            "documentary-or-factual": (
                "A documentary or factual program, including a documentary about a person's life."
            ),
            "non-narrative-program": (
                "Talk, variety, game, news, competition, unscripted reality, or another program without a "
                "bounded story narrative."
            ),
            "insufficient-evidence": "The article does not establish which content form applies.",
        },
    }
}


SCORES = {
    "score__intensity": {
        "type": "score",
        "instructions": "How intense is the requested work's depicted danger, violence, darkness, or distress?",
        "criteria": [
            "Gentle: little danger, violence, darkness, or distress.",
            "Mild: occasional threat or distress with limited impact.",
            "Moderate: meaningful danger, violence, darkness, or distress, but not sustained.",
            "Strong: sustained danger, violence, darkness, or distress is central to the experience.",
            "Extreme: exceptionally graphic, harrowing, brutal, or relentlessly distressing material.",
        ],
    },
    "score__humour": {
        "type": "score",
        "instructions": "How much deliberate humour shapes the requested work's experience?",
        "criteria": [
            "None: no meaningful comic intent is evident.",
            "Occasional: isolated jokes or moments of levity.",
            "Recurring: humour is a regular secondary ingredient.",
            "Strongly comic: humour drives much of the work alongside other aims.",
            "Comedy-dominant: creating humour is the work's central mode.",
        ],
    },
    "score__emotional_weight": {
        "type": "score",
        "instructions": "How emotionally heavy is the requested work's dominant experience?",
        "criteria": [
            "Light or detached: little sustained emotional burden.",
            "Modest: some emotional stakes without sustained heaviness.",
            "Substantial: recurring serious emotional stakes shape the work.",
            "Heavy: grief, trauma, loss, or moral pain dominates much of the experience.",
            "Devastating: exceptionally sustained or overwhelming emotional burden.",
        ],
    },
    "score__complexity": {
        "type": "score",
        "instructions": "How demanding is the requested work's narrative structure or causal/story information?",
        "criteria": [
            "Straightforward: one readily followed line with little ambiguity or information load.",
            "Some layering: a few subplots, shifts, or withheld details, but easy to follow.",
            "Moderately intricate: several meaningful threads, reveals, or structural demands.",
            "Highly intricate: dense interconnections, chronology, viewpoints, or ambiguity demand attention.",
            "Exceptionally demanding: unusually dense or experimental structure resists a single easy reading.",
        ],
    },
}


SECTION_ROLE_CRITERIA = {
    "story-premise": "Plot, premise, storylines, episode narratives, character actions, or dramatized events.",
    "theme-subject": "Themes, interpretation, or real-world subject matter; not production or reception.",
    "work-context": "Lead identity, format, setting, release, or adaptation context for the requested work.",
    "irrelevant-production-reception-navigation": (
        "Production, cast, credits, marketing, reception, awards, references, links, navigation, or unrelated text."
    ),
}


def taxonomy_questions(path=TAXONOMY):
    tax = taxonomy(path)
    primary = {
        "primary_genre": {
            "type": "choice",
            "instructions": (
                "What is the requested work's single best primary story genre? Animation is a format, not a "
                "genre. Answer from the supplied article only."
            ),
            "criteria": {value: f"The work's primary story genre is {value}."
                         for value in tax["primaryGenres"]},
        }
    }
    nouls = {}
    mapping = {}
    for family in ("subgenres", "thematic", "moods"):
        singular = {"subgenres": "subgenre", "thematic": "theme", "moods": "mood"}[family]
        for label in tax[family]:
            question_id = f"tax__{singular}__{slug(label)}"
            if question_id in nouls:
                raise ValueError(f"taxonomy question id collision: {question_id}")
            nouls[question_id] = {
                "type": "noul",
                "instructions": (
                    f"From article evidence, does the requested work materially fit the {singular} label "
                    f"`{label}` (not merely mention it)?"
                ),
            }
            mapping[question_id] = {"family": family, "label": label}
    return {**primary, **nouls}, mapping, tax


def global_questions(prompt=PROMPT, taxonomy_path=TAXONOMY):
    taxonomy_qs, mapping, tax = taxonomy_questions(taxonomy_path)
    return {
        **facet_questions(prompt),
        **VALIDITY,
        **APPLICABILITY,
        **taxonomy_qs,
        **SCORES,
    }, mapping, tax


def section_question(section_id):
    return {
        "type": "choice",
        "instructions": (
            f"What is `article.sections.{section_id}` primarily evidence for? Judge its text, not its heading alone."
        ),
        "criteria": SECTION_ROLE_CRITERIA,
    }
