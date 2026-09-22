#!/usr/bin/env python3
"""Validate and summarize a completed combined Jev corpus artifact.

The paid runner validates each response before append.  This is the independent, no-network readback: it
reconstructs every state and question set from the frozen article input, checks stored provenance and coverage,
then measures the publication gates and section-role disagreements without publishing anything.
"""
import argparse
from collections import Counter, defaultdict
import json
import os
import re
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from combined_questions import PROMPT, ROOT, TAXONOMY, section_question
from pipeline.article_sections import (encoded_chars, is_oversized, public_section, select_global_sections,
                                       sha256_text, state_for)
from run_combined import (IMPLEMENTATION as SOURCES, SCHEMA_VERSION, article_key, attach_enriched_evidence,
                          canonical, load_articles, sections_for_record, sha256_file, validate_answers)
from lib.typesafe_client import TypeSafe


HERE = os.path.dirname(os.path.abspath(__file__))
#: Superseded digests of the source files the pass hashes into every manifest, each with the commit that
#: superseded it and why its rows still mean the same thing. See `validate_implementation`.
LINEAGE = os.path.join(HERE, "implementation-lineage.json")

#: The pass's own source files, which `run_combined.manifest_config` hashes into every shard's manifest
#: under `implementationSha256`, by name. A manifest must record every one of them: see
#: `validate_implementation`. Where each one lives is `SOURCES` — the paths the pass hashes, not a copy.
IMPLEMENTATION = tuple(SOURCES)

#: Where the pass keeps each committed input today, for a manifest that names it somewhere else. See
#: `manifest_file`.
CURRENT_FILE = {"prompt": PROMPT, "taxonomy": TAXONOMY}

PLOT_AXES = {
    "archetype", "chronology", "conflict", "continuity", "ending", "ensemble", "era", "pacing",
    "scope", "setting", "timespan", "tone",
}
EXCLUDED_VALUES = {"does-not-apply"}
VALUE_PROBABILITY = 0.70
VALUE_MARGIN = 0.25
VALIDITY_PROBABILITY = 0.80


def fail(where, message):
    raise ValueError(f"{where}: {message}")


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * fraction)]


def distribution(values):
    return {
        "p10": round(percentile(values, 0.10), 4),
        "median": round(statistics.median(values), 4),
        "p90": round(percentile(values, 0.90), 4),
    } if values else {"p10": None, "median": None, "p90": None}


def choice_strength(answer):
    ordered = sorted(answer["probabilities"].values(), reverse=True)
    return answer["probabilities"][answer["choice"]], ordered[0] - ordered[1]


def role_answer(section):
    role = section["role"]
    return {
        "type": "choice",
        "choice": role["value"],
        "confidence": role["confidence"],
        "probabilities": role["probabilities"],
    }


def expected_questions(global_questions, section_ids, phase):
    if phase == "global-after-section-audit":
        return global_questions
    regional = {f"section__{section_id}": section_question(section_id) for section_id in section_ids}
    return {**global_questions, **regional} if phase == "combined" else regional


def validate_call(where, call, rec, sections_by_id, global_questions, pinned_model):
    required = {
        "phase", "sectionIds", "stateChars", "stateSha256", "questionsSha256", "questionCount",
        "responseModel", "inputTokens", "outputTokens",
    }
    if set(call) != required:
        fail(where, f"call fields differ: {sorted(set(call) ^ required)}")
    phase = call["phase"]
    if phase != "combined" and phase != "global-after-section-audit" and not phase.startswith("section-group-"):
        fail(where, f"unknown phase {phase!r}")
    try:
        selected = [sections_by_id[section_id] for section_id in call["sectionIds"]]
    except KeyError as exc:
        fail(where, f"call refers to unknown section {exc.args[0]}")
    state = state_for(rec, selected)
    questions = expected_questions(global_questions, call["sectionIds"], phase)
    if call["stateChars"] != encoded_chars(state) or call["stateSha256"] != sha256_text(canonical(state)):
        fail(where, "stored state provenance differs from reconstructed state")
    if call["questionCount"] != len(questions) \
            or call["questionsSha256"] != sha256_text(canonical(questions)):
        fail(where, "stored question provenance differs from reconstructed questions")
    if call["responseModel"] != pinned_model:
        fail(where, f"response model {call['responseModel']!r} differs from pinned {pinned_model!r}")
    for field in ("inputTokens", "outputTokens"):
        if isinstance(call[field], bool) or not isinstance(call[field], int) or call[field] < 0:
            fail(where, f"invalid {field} {call[field]!r}")


def validate_row(row, rec, manifest, global_questions):
    key = article_key(rec)
    where = key
    if row.get("schemaVersion") != SCHEMA_VERSION:
        fail(where, f"schema {row.get('schemaVersion')!r}")
    if row.get("runId") != manifest["runId"] or row.get("configSha256") != manifest["configSha256"]:
        fail(where, "run provenance differs from manifest")
    if row.get("requestedModel") != manifest["config"]["requestedModel"]:
        fail(where, "requested model differs from manifest")
    if row.get("articleSha256") != sha256_text(rec["text"]):
        fail(where, "article content hash differs from frozen input")
    for row_name, rec_name in [
        ("title", "title"), ("year", "year"), ("article", "article"), ("language", "language"),
        ("articleRevId", "revId"),
    ]:
        if row.get(row_name) != rec.get(rec_name):
            fail(where, f"{row_name} differs from frozen input")
    extractor_rev = rec.get("extractorArticleRevId", rec.get("revId"))
    if row.get("extractorArticleRevId") != extractor_rev \
            or row.get("sectionAuditSameRevision") != (extractor_rev == rec.get("revId")):
        fail(where, "extractor revision provenance differs")
    if row.get("articleChars") != len(rec["text"]):
        fail(where, "article character count differs")

    validate_answers(row.get("answers"), global_questions)
    sections = sections_for_record(rec)
    stored_sections = row.get("sections")
    if not isinstance(stored_sections, list) or len(stored_sections) != len(sections):
        fail(where, "section count differs from reconstructed article")
    section_answers = {}
    for expected, stored in zip(sections, stored_sections):
        answer = role_answer(stored)
        validate_answers({f"section__{expected['id']}": answer},
                         {f"section__{expected['id']}": section_question(expected["id"])})
        if stored != public_section(expected, answer):
            fail(where, f"section {expected['id']} differs from reconstructed article")
        section_answers[expected["id"]] = answer

    expected_oversized = is_oversized(rec, sections, manifest["config"]["maxStateChars"])
    if row.get("oversized") != expected_oversized:
        fail(where, "oversized flag differs from reconstructed state")
    selected = select_global_sections(
        rec, sections, section_answers, manifest["config"]["maxStateChars"],
    ) if expected_oversized else sections
    selected_ids = [section["id"] for section in selected]
    omitted_ids = [section["id"] for section in sections if section not in selected]
    if row.get("globalStateSectionIds") != selected_ids or row.get("omittedFromGlobalState") != omitted_ids:
        fail(where, "global state section selection differs")

    selected_counts, extractor_counts = Counter(), Counter(rec.get("plotSections") or ())
    for section in sections:
        if section["extractorSelected"]:
            selected_counts[section["heading"]] += 1
    missing = []
    for heading, count in extractor_counts.items():
        missing.extend([heading] * max(0, count - selected_counts[heading]))
    if row.get("extractorSectionsMissingFromArticle") != missing:
        fail(where, "missing extractor headings differ")

    calls = row.get("calls")
    if not isinstance(calls, list) or not calls:
        fail(where, "calls are missing")
    by_id = {section["id"]: section for section in sections}
    for index, call in enumerate(calls):
        validate_call(f"{where}.calls[{index}]", call, rec, by_id, global_questions,
                      manifest["config"]["requestedModel"])
    if expected_oversized:
        regional_calls = calls[:-1]
        if not regional_calls or any(
            call["phase"] != f"section-group-{index}" for index, call in enumerate(regional_calls, 1)
        ):
            fail(where, "oversized section groups are absent or out of order")
        audited = [section_id for call in regional_calls for section_id in call["sectionIds"]]
        if audited != [section["id"] for section in sections] \
                or calls[-1]["phase"] != "global-after-section-audit" \
                or calls[-1]["sectionIds"] != selected_ids:
            fail(where, "oversized call plan does not audit every section before the global call")
    elif len(calls) != 1 or calls[0]["phase"] != "combined" \
            or calls[0]["sectionIds"] != [section["id"] for section in sections]:
        fail(where, "ordinary title does not have exactly one combined call")
    if row.get("responseModels") != sorted({call["responseModel"] for call in calls}):
        fail(where, "responseModels differs from calls")
    return sections, section_answers, calls


def short_ref(ref):
    """A lineage entry's superseding commit, abbreviated — unless it is not a hash.

    An entry recorded by the commit that supersedes it cannot name that commit's own hash, so it names
    the change instead; cutting a sentence to seven characters would print a fragment of one.
    """
    ref = ref or "?"
    return ref[:7] if re.fullmatch(r"[0-9a-f]{7,64}", ref) else ref


def _lineage_section(path, section):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh).get(section, {})


def load_lineage(path=LINEAGE):
    """The recorded exceptions, `{filename: [entry, …]}`. An absent file means no exception is allowed."""
    return _lineage_section(path, "superseded")


def load_input_lineage(path=LINEAGE):
    """The recorded exceptions for the committed INPUTS, `{role: [entry, …]}` — `prompt`, `taxonomy`.

    Kept apart from `load_lineage`: the four source files are what the pass IS, and the prompt and the
    vocabulary are what it was handed. Filing a data file under a source file's name would say a source
    file changed when none did, and would let a vocabulary edit borrow a reason written about code.
    """
    return _lineage_section(path, "supersededInputs")


def validate_implementation(where, config, lineage=None):
    """The pass's own source files, against the digests the shard's manifest recorded.

    A digest that matches the working tree needs nothing. A digest that does not is one of two things and
    the difference is the whole point of the check: the pass was edited in a way that changes what the rows
    mean, or it was edited in a way that does not. Nothing can tell those apart by hashing, so the second
    one has to be written down — `implementation-lineage.json`, naming the commit and the reason — and an
    unrecorded difference is a refusal.

    The alternative, re-stamping the manifest onto today's digests, is what this refuses to do: the
    manifest is the provenance of a run that was paid for once and will not be repeated, and rewriting it
    to keep a checker quiet destroys the only record of what produced those rows.

    A manifest that records no digest for one of `IMPLEMENTATION`, or none at all, is refused too: nothing
    to compare is not a match, and it says nothing about which version of the pass produced the rows.

    Returns the allowances it granted, so a caller can say out loud which ones it is running on.
    """
    lineage = load_lineage() if lineage is None else lineage
    recorded = config.get("implementationSha256")
    if not isinstance(recorded, dict):
        fail(where, "the manifest records no implementationSha256, so nothing says which version of the pass "
                    "produced the shard's rows")
    missing = [name for name in IMPLEMENTATION if name not in recorded]
    if missing:
        fail(where, f"the manifest records no implementation hash for {', '.join(missing)}, so nothing says "
                    f"which version of {'that file' if len(missing) == 1 else 'those files'} produced the "
                    f"shard's rows")
    allowed = []
    for name, expected in recorded.items():
        path = SOURCES.get(name)
        if path is None:
            fail(where, f"the manifest records a digest for {name}, which is not a source file of the pass")
        if sha256_file(path) == expected:
            continue
        entry = next((e for e in lineage.get(name, []) if e.get("sha256") == expected), None)
        if entry is None:
            fail(where, f"implementation hash differs for {name}: the shard was produced by {expected[:12]} "
                        f"and this tree holds {sha256_file(path)[:12]}. If that edit cannot change the rows, "
                        f"record it in scripts/v2/implementation-lineage.json with the commit and the "
                        f"reason; do not re-stamp the manifest.")
        allowed.append({"file": name, "sha256": expected, **{
            key: entry[key] for key in ("commit", "supersededBy", "why") if key in entry}})
    return allowed


def manifest_file(config, name):
    """The committed file a manifest names, resolved against the repo, or None if it is not there.

    A relative path is repo-relative by construction — that is how the taxonomy is recorded since it
    moved into `data/` (oxyc/den-dataset#27). An absolute one was written by a checkout that need not be
    this one and by a layout that may have moved since, so when nothing is at it the file the pass uses
    for that role today stands in. What decides the audit either way is the digest beside the path: a
    vocabulary or a prompt that really changed still fails, and only a file that moved is forgiven.
    """
    path = config.get(name)
    if not isinstance(path, str):
        return None
    resolved = path if os.path.isabs(path) else os.path.join(ROOT, path)
    if os.path.isfile(resolved):
        return resolved
    fallback = CURRENT_FILE.get(name)
    return fallback if fallback and os.path.isfile(fallback) else None


def validate_inputs(where, config, lineage=None):
    """The committed files the manifest pins by digest — the prompt and the vocabulary.

    The digest is what decides it, which is why `manifest_file` can forgive a path that moved: a file
    that still hashes to what the shard recorded is the file the shard was bought from, wherever it now
    sits. A digest that differs is a real difference in the bytes, and there are two kinds. One changes
    what the pass asked — a label added, a prompt reworded — and must refuse. The other changes only how
    the same content is written down, which is what converting the vocabulary from Swift source to JSON
    did (oxyc/den-dataset#27): the same labels, the same version, a digest that could not stay.

    Nothing can tell those apart by hashing, so the second is written down in
    `implementation-lineage.json` under `supersededInputs`, with the evidence that the questions built
    from it are unchanged. An unrecorded difference is a refusal, and re-stamping the manifest is not an
    option: it is the provenance of a run that was paid for once.

    Returns the allowances it granted.
    """
    lineage = load_input_lineage() if lineage is None else lineage
    allowed = []
    for role, hash_name in (("prompt", "promptSha256"), ("taxonomy", "taxonomySha256")):
        recorded = config.get(hash_name)
        path = manifest_file(config, role)
        if path is None:
            fail(where, f"{role} artifact hash differs: the manifest names no file this checkout can find")
        actual = sha256_file(path)
        if actual == recorded:
            continue
        entry = next((e for e in lineage.get(role, []) if e.get("sha256") == recorded), None)
        if entry is None:
            fail(where, f"{role} artifact hash differs: the shard was bought from "
                        f"{str(recorded)[:12]} and this tree holds {actual[:12]}. If the file was "
                        f"rewritten without changing what the pass asked, record it under "
                        f"supersededInputs in scripts/v2/implementation-lineage.json with the commit, "
                        f"the reason and the evidence; do not re-stamp the manifest.")
        allowed.append({"input": role, "sha256": recorded, **{
            key: entry[key] for key in ("file", "commit", "supersededBy", "why", "evidence")
            if key in entry}})
    return allowed


def validate_manifest(manifest, articles, enriched_sha):
    config = manifest.get("config")
    if not isinstance(config, dict) or manifest.get("configSha256") != sha256_text(canonical(config)):
        fail("manifest", "config hash differs")
    if config.get("schemaVersion") != SCHEMA_VERSION:
        fail("manifest", f"schema {config.get('schemaVersion')!r}")
    if config.get("articlesSha256") != sha256_file(articles):
        fail("manifest", "article artifact hash differs")
    if config.get("enrichedEvidenceSha256") != enriched_sha:
        fail("manifest", "enriched evidence hash differs")
    validate_inputs("manifest", config)
    validate_implementation("manifest", config)
    if config.get("globalQuestionsSha256") != sha256_text(canonical(config.get("globalQuestions"))):
        fail("manifest", "global question hash differs")
    template = config.get("sectionQuestionTemplate")
    if template != section_question("SECTION_ID") \
            or config.get("sectionQuestionTemplateSha256") != sha256_text(canonical(template)):
        fail("manifest", "section question template differs")
    mapping = config.get("labelQuestionMapping")
    if not isinstance(mapping, dict) \
            or config.get("labelQuestionMappingSha256") != sha256_text(canonical(mapping)):
        fail("manifest", "label question mapping hash differs")


def audit(records, output=None, manifest=None, sources=None):
    """Validate and summarize one artifact or an exact, disjoint set of manifested shards.

    ``sources`` entries contain ``output``, ``manifest``, and the keys permitted by that shard's own
    article input.  The ordinary single-artifact CLI uses the same path with every corpus key permitted.
    """
    records_by_key = {article_key(record): record for record in records}
    if sources is None:
        if output is None or manifest is None:
            raise TypeError("output and manifest are required for a single-artifact audit")
        sources = [{"output": output, "manifest": manifest, "allowedKeys": set(records_by_key)}]
    seen = set()
    media = Counter()
    validity = Counter()
    applicability = Counter()
    response_models = Counter()
    choice_values = defaultdict(Counter)
    choice_probabilities = defaultdict(list)
    choice_margins = defaultdict(list)
    publishable = Counter()
    noul_positive = Counter()
    score_values = defaultdict(list)
    score_confidences = defaultdict(list)
    section_roles = Counter()
    section_probabilities = []
    section_comparison = Counter()
    calls_total = input_tokens = output_tokens = oversized = different_revision = missing_heading_rows = 0

    for source in sources:
        source_output = source["output"]
        source_manifest = source["manifest"]
        allowed_keys = source["allowedKeys"]
        global_questions = source_manifest["config"]["globalQuestions"]
        with open(source_output, encoding="utf-8") as fh:
            for line_number, line in enumerate(fh, 1):
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    fail(f"{source_output}:{line_number}", f"malformed JSON: {exc}")
                key = f"{row.get('mediaType')}:{row.get('tmdbId')}"
                if key in seen:
                    fail(f"{source_output}:{line_number}", f"duplicate key {key} across bundle")
                if key not in allowed_keys:
                    fail(f"{source_output}:{line_number}", f"key {key} absent from shard article input")
                rec = records_by_key.get(key)
                if rec is None:
                    fail(f"{source_output}:{line_number}", f"key {key} absent from full article input")
                seen.add(key)
                sections, section_answers, calls = validate_row(
                    row, rec, source_manifest, global_questions,
                )
                media[row["mediaType"]] += 1
                oversized += int(row["oversized"])
                different_revision += int(not row["sectionAuditSameRevision"])
                missing_heading_rows += int(bool(row["extractorSectionsMissingFromArticle"]))
                calls_total += len(calls)
                input_tokens += sum(call["inputTokens"] for call in calls)
                output_tokens += sum(call["outputTokens"] for call in calls)
                response_models.update(call["responseModel"] for call in calls)

                answers = row["answers"]
                valid_answer = answers["validity"]
                validity[valid_answer["choice"]] += 1
                applicability[answers["narrative_applicability"]["choice"]] += 1
                valid_probability = valid_answer["probabilities"]["correct-screen-work"]
                valid_for_publication = valid_probability >= VALIDITY_PROBABILITY
                narrative = answers["narrative_applicability"]["choice"]
                for name, answer in answers.items():
                    if answer["type"] == "choice":
                        probability, margin = choice_strength(answer)
                        choice_values[name][answer["choice"]] += 1
                        choice_probabilities[name].append(probability)
                        choice_margins[name].append(margin)
                        excluded = answer["choice"] in EXCLUDED_VALUES \
                            or (name == "ending" and answer["choice"] == "unknown")
                        content_ok = narrative != "non-narrative-program"
                        if name == "archetype":
                            content_ok = narrative == "bounded-fictional-narrative"
                        if valid_for_publication and (name not in PLOT_AXES or content_ok) \
                                and probability >= VALUE_PROBABILITY and margin >= VALUE_MARGIN \
                                and not excluded:
                            publishable[name] += 1
                    elif answer["type"] == "noul":
                        if valid_for_publication and answer["noul"] >= VALUE_PROBABILITY:
                            noul_positive[name] += 1
                    else:
                        score_values[name].append(answer["score"])
                        score_confidences[name].append(answer["confidence"])

                for section, answer in zip(sections, section_answers.values()):
                    probability, margin = choice_strength(answer)
                    role = answer["choice"]
                    section_roles[role] += 1
                    section_probabilities.append(probability)
                    high = probability >= VALUE_PROBABILITY and margin >= VALUE_MARGIN
                    if row["sectionAuditSameRevision"] \
                            and not row["extractorSectionsMissingFromArticle"] and high:
                        selected = section["extractorSelected"]
                        section_comparison[f"{'selected' if selected else 'unselected'}.{role}"] += 1

    missing = set(records_by_key) - seen
    if missing:
        label = output if output is not None else "combined bundle"
        fail(label, f"missing {len(missing):,} article keys")

    choice_summary = {}
    for name in sorted(choice_values):
        choice_summary[name] = {
            "values": dict(choice_values[name].most_common()),
            "topProbability": distribution(choice_probabilities[name]),
            "margin": distribution(choice_margins[name]),
            "publishable": publishable[name],
            "publishableRate": round(publishable[name] / len(seen), 4),
        }
    score_summary = {
        name: {"score": distribution(score_values[name]), "confidence": distribution(score_confidences[name])}
        for name in sorted(score_values)
    }
    return {
        "integrity": {
            "rows": len(seen), "expectedRows": len(records_by_key), "uniqueKeys": len(seen),
            "mediaTypes": dict(media), "schemaVersion": SCHEMA_VERSION, "shards": len(sources),
        },
        "run": {
            "calls": calls_total, "oversizedTitles": oversized, "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "estimatedSpendUSD": round(input_tokens * TypeSafe.RATE_PER_INPUT_TOKEN, 4),
            "responseModels": dict(response_models),
        },
        "provenance": {
            "sameRevisionRows": len(seen) - different_revision,
            "differentRevisionRows": different_revision,
            "rowsWithMissingExtractorHeadings": missing_heading_rows,
        },
        "validity": dict(validity.most_common()),
        "narrativeApplicability": dict(applicability.most_common()),
        "choices": choice_summary,
        "labelsAtLeast070": dict(sorted(noul_positive.items())),
        "scores": score_summary,
        "sections": {
            "count": sum(section_roles.values()), "roles": dict(section_roles.most_common()),
            "topProbability": distribution(section_probabilities),
            "sameRevisionHighConfidenceComparison": dict(sorted(section_comparison.items())),
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--articles", required=True)
    parser.add_argument("--enriched-dir")
    parser.add_argument("--out", required=True, help="completed combined JSONL artifact")
    parser.add_argument("--manifest", help="default: <out>.manifest.json")
    args = parser.parse_args(argv)
    manifest_path = args.manifest or args.out + ".manifest.json"
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    records, _ = load_articles(args.articles)
    enriched_sha = attach_enriched_evidence(records, args.enriched_dir)
    validate_manifest(manifest, args.articles, enriched_sha)
    print(json.dumps(audit(records, args.out, manifest), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
