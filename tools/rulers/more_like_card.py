#!/usr/bin/env python3
"""Preregister the compact-card arm of the #132 More Like This gate, and compare it with the story arm.

A card is what the dataset already knows about a title: its premise tags, its published plot facets, its
genres & moods, and its Wikipedia lead. It is built once per title and reused in every pair that title is
in. The arm takes pairs, blinding and title/year from a frozen story-arm worklist, so the two arms differ
only in the evidence; the questions are the story arm's, byte for byte, so `more_like_gate.py run` and
`score` run it unchanged.
"""
import argparse
import gzip
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from more_like_gate import (MAX_EVIDENCE_CHARS, MAX_STATE_CHARS, MODEL, SCHEMA, _auc, _interval,  # noqa: E402
                            canonical, digest, file_digest, load_plan, parse_sections, questions,
                            read_jsonl)
from store.facets import FACET_AXES, publishable, row_applicability  # noqa: E402

ARM = "card"
ROLES = ("anchor", "a", "b")


def _lead(text):
    lead = parse_sections(text)[0]
    return f"## {lead['heading']}\n{lead['text']}".strip()[:MAX_EVIDENCE_CHARS]


def _facets(row):
    """Only the facets the store publishes: the same gates, so the card claims nothing the dataset withholds."""
    if row is None:
        return {}
    validity, narrative = row_applicability(row)
    result = {}
    for axis in FACET_AXES:
        facet = (row.get("facets") or {}).get(axis)
        if publishable(axis, facet, validity, narrative)[0]:
            result[axis] = facet["choice"]
    return result


def _genres_moods(entry):
    if entry is None:
        return {}
    return {"primaryGenre": entry.get("primaryGenre"),
            "subgenres": [item["label"] for item in entry.get("subgenres") or ()],
            "moods": [item["label"] for item in entry.get("moods") or ()],
            "animated": entry.get("animated")}


def build_cards(keys, articles_path, story_article_sha, premise_tags_path, genres_moods_path, corpus_path):
    with open(premise_tags_path, encoding="utf-8") as fh:
        tags = json.load(fh)["tags"]
    with open(genres_moods_path, encoding="utf-8") as fh:
        labels = json.load(fh)["titles"]
    corpus = {}
    with gzip.open(corpus_path, "rt", encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            if row.get("key") in keys:
                corpus[row["key"]] = row
    leads = {}
    for row in read_jsonl(articles_path):
        key = f"{row['mediaType']}:{row['tmdbId']}"
        if key not in keys:
            continue
        # The lead must come from the exact article text the story arm sent, or the arms are not paired.
        if hashlib.sha256(row["text"].encode()).hexdigest() != story_article_sha[key]:
            raise ValueError(f"{key}: article differs from the one the story arm sent")
        leads[key] = _lead(row["text"])
    if set(leads) != keys:
        raise ValueError(f"articles lack {len(keys - set(leads))} story-arm works")
    cards = {key: {"premiseTags": list(tags.get(key) or ()), "plotFacets": _facets(corpus.get(key)),
                   "genresMoods": _genres_moods(labels.get(key)), "articleLead": leads[key]}
             for key in sorted(keys)}
    coverage = {"works": len(cards),
                "premiseTags": sum(bool(card["premiseTags"]) for card in cards.values()),
                "plotFacets": sum(bool(card["plotFacets"]) for card in cards.values()),
                "genresMoods": sum(bool(card["genresMoods"]) for card in cards.values())}
    return cards, coverage


def prepare(story_work, articles, premise_tags, genres_moods, corpus, work):
    story_registration, story_rows = load_plan(story_work)
    story_article_sha = {}
    for row in story_rows:
        for role in ROLES:
            item = row["state"]["works"][role]
            if story_article_sha.setdefault(item["key"], item["articleSha256"]) != item["articleSha256"]:
                raise ValueError(f"{item['key']}: two article texts in one story worklist")
    cards, coverage = build_cards(set(story_article_sha), articles, story_article_sha,
                                  premise_tags, genres_moods, corpus)
    rows = []
    for source in story_rows:
        works = {}
        for role in ROLES:
            item = source["state"]["works"][role]
            works[role] = {"key": item["key"], "title": item["title"], "year": item["year"],
                           "card": cards[item["key"]], "cardSha256": digest(cards[item["key"]])}
        state = {"task": "More Like This pair comparison", "works": works}
        if len(canonical(state)) > MAX_STATE_CHARS:
            raise ValueError(f"{source['pairId']}: state exceeds {MAX_STATE_CHARS:,} chars")
        rows.append({"pairId": source["pairId"], "state": state, "stateSha256": digest(state),
                     "positiveLabel": source["positiveLabel"], "titleYear": source["titleYear"],
                     "plot": source["plot"], "controls": source["controls"]})
    worklist_text = "".join(canonical(row) + "\n" for row in rows)
    cards_text = canonical(cards)
    registration = {
        **{key: story_registration[key] for key in (
            "issue", "sourceComment", "model", "sampleMethod", "sampleSize", "populationSize", "rulerSha256",
            "questionsSha256", "questions", "primaryScore", "bootstrap", "limitations")},
        "schema": SCHEMA, "arm": ARM,
        "card": ["premise tags", "published plot facets", "genres & moods", "article lead"],
        "storyWorklistSha256": story_registration["worklistSha256"],
        "articlesSha256": file_digest(articles), "premiseTagsSha256": file_digest(premise_tags),
        "genresMoodsSha256": file_digest(genres_moods), "corpusSha256": file_digest(corpus),
        "cardsSha256": hashlib.sha256(cards_text.encode()).hexdigest(), "cardCoverage": coverage,
        "worklistSha256": hashlib.sha256(worklist_text.encode()).hexdigest(),
        "comparison": "card minus story overall-Noul AUC, paired over the same pairs; reported, not gated",
    }
    os.makedirs(work, exist_ok=True)
    path = os.path.join(work, "preregistration.json")
    worklist = os.path.join(work, "worklist.jsonl")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            previous = json.load(fh)
        if previous != registration or file_digest(worklist) != registration["worklistSha256"]:
            raise ValueError(f"{path}: refusing to replace a different card preregistration")
        return previous
    for target, text in ((os.path.join(work, "cards.json"), cards_text + "\n"), (worklist, worklist_text)):
        with open(target + ".tmp", "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(target + ".tmp", target)
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(registration, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(path + ".tmp", path)
    return registration


def _overall(work, answers_path):
    registration, planned = load_plan(work)
    answers = {row["pairId"]: row for row in read_jsonl(answers_path)}
    result = {}
    for source in planned:
        answer = answers.get(source["pairId"])
        if answer is None or answer.get("stateSha256") != source["stateSha256"] \
                or answer.get("questionsSha256") != registration["questionsSha256"]:
            raise ValueError(f"{source['pairId']}: missing or mismatched answer in {answers_path}")
        positive = source["positiveLabel"]
        negative = "b" if positive == "a" else "a"
        result[source["pairId"]] = {"positive": answer["answers"][f"{positive}__overall"]["noul"],
                                    "negative": answer["answers"][f"{negative}__overall"]["noul"],
                                    "tokens": answer["usage"]["input_tokens"],
                                    "stateChars": len(canonical(source["state"]))}
    return registration, result


def compare(card_work, card_answers, story_work, story_answers):
    card_registration, card = _overall(card_work, card_answers)
    story_registration, story = _overall(story_work, story_answers)
    if card_registration.get("storyWorklistSha256") != story_registration["worklistSha256"] \
            or set(card) != set(story):
        raise ValueError("the card arm was not prepared from this story worklist")
    rows = [{"card": card[pair], "story": story[pair]} for pair in sorted(card)]

    def auc(arm):
        return lambda sample: _auc(sample, lambda row: (row[arm]["positive"], row[arm]["negative"]))

    def per_call(arm, field):
        return sum(row[arm][field] for row in rows) / len(rows)

    card_auc, story_auc = auc("card")(rows), auc("story")(rows)
    return {"pairs": len(rows), "cardAuc": card_auc, "cardAuc95": _interval(rows, auc("card")),
            "storyAuc": story_auc, "cardMinusStory": card_auc - story_auc,
            "cardMinusStory95": _interval(rows, lambda sample: auc("card")(sample) - auc("story")(sample)),
            "inputTokensPerCall": {"card": per_call("card", "tokens"), "story": per_call("story", "tokens")},
            "stateCharsPerCall": {"card": per_call("card", "stateChars"),
                                  "story": per_call("story", "stateChars")}}


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--story-work", required=True, help="the frozen story-arm gate work directory")
    prep.add_argument("--articles", required=True)
    prep.add_argument("--premise-tags", required=True)
    prep.add_argument("--genres-moods", required=True)
    prep.add_argument("--corpus", required=True, help="the corpus whose facets the store publishes")
    prep.add_argument("--work", required=True)
    cmp = sub.add_parser("compare")
    cmp.add_argument("--card-work", required=True)
    cmp.add_argument("--card-answers", required=True)
    cmp.add_argument("--story-work", required=True)
    cmp.add_argument("--story-answers", required=True)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        result = prepare(args.story_work, args.articles, args.premise_tags, args.genres_moods, args.corpus,
                         args.work)
    else:
        result = compare(args.card_work, args.card_answers, args.story_work, args.story_answers)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
