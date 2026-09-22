#!/usr/bin/env python3
"""Run the provenance-complete Jev taxonomy/facet/section census.

Normal articles are one request: the whole structured article state plus every global and section-role
question.  Oversized articles use section-only groups first, then a global request containing the strongest
story/theme/context sections selected from those distributions.  No question ever refers to unseen text.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
import re
import sys
import threading
import uuid

if not __package__:
    # Run as a file, which is how the classify stage runs it: the repo, not pipeline/, is the import root.
    sys.path[0] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from lib import typesafe_client
from lib.typesafe_client import TypeSafe, TypeSafeError
from pipeline import article_sections, combined_questions
from pipeline.article_sections import (encoded_chars, is_oversized, parse_sections, public_section,
                                       section_groups, select_global_sections, sha256_text, state_for)
from pipeline.combined_questions import (PINNED_MODEL, PROMPT, ROOT, TAXONOMY, global_questions,
                                         section_question)

SCHEMA_VERSION = "combined-jev-v1"
#: Bumped when the shape of the recorded configuration changes rather than when the questions do. `-v2`
#: records the taxonomy repo-relative (see `manifest_config`), so a manifest written before it cannot be
#: resumed — `load_or_create_manifest` says so in those words. Converting the vocabulary from Swift to
#: JSON did NOT bump it: every key is still written the same way and still means the same thing, and the
#: two values that moved with the file, `taxonomy` and `taxonomySha256`, are compared on their own.
PLANNER_VERSION = "whole-or-role-selected-v2"
DEFAULT_MAX_STATE_CHARS = 110_000

#: The pass's own source files, which `manifest_config` hashes into every manifest under
#: `implementationSha256`, and `audit_combined.validate_implementation` hashes again. Keyed by FILE NAME,
#: which is what every shipped manifest records, so a file that moves keeps its key. The paths are each
#: module's own rather than one shared directory: the four do not live together (oxyc/den-dataset#73).
IMPLEMENTATION = {name: os.path.abspath(path) for name, path in (
    ("run_combined.py", __file__),
    ("article_sections.py", article_sections.__file__),
    ("combined_questions.py", combined_questions.__file__),
    ("typesafe_client.py", typesafe_client.__file__),
)}


class RunAborted(RuntimeError):
    """A worker observed the shared circuit breaker before making another paid call."""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def probability(value, where):
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value) or not 0 <= value <= 1:
        raise TypeSafeError(f"{where}: invalid probability {value!r}")


def validate_answers(answers, questions):
    """Validate all three documented System One primitive response shapes."""
    if not isinstance(answers, dict):
        raise TypeSafeError("answers is not an object")
    expected, actual = set(questions), set(answers)
    if actual != expected:
        raise TypeSafeError(
            f"answer keys differ: missing={sorted(expected - actual)} extra={sorted(actual - expected)}")

    for question_id, question in questions.items():
        answer = answers[question_id]
        if not isinstance(answer, dict):
            raise TypeSafeError(f"{question_id}: answer is not an object")
        kind = question.get("type")
        if answer.get("type") != kind:
            raise TypeSafeError(f"{question_id}: response type {answer.get('type')!r}, expected {kind!r}")
        if kind == "noul":
            if set(answer) != {"type", "noul"}:
                raise TypeSafeError(f"{question_id}: malformed Noul fields {sorted(answer)}")
            probability(answer["noul"], f"{question_id}.noul")
            continue
        if kind == "choice":
            allowed = set(question["criteria"])
            if answer.get("choice") not in allowed:
                raise TypeSafeError(f"{question_id}: invalid choice {answer.get('choice')!r}")
            probability(answer.get("confidence"), f"{question_id}.confidence")
            probabilities = answer.get("probabilities")
            if not isinstance(probabilities, dict) or set(probabilities) != allowed:
                raise TypeSafeError(f"{question_id}: Choice probability keys differ from criteria")
            for key, value in probabilities.items():
                probability(value, f"{question_id}.probabilities.{key}")
            if abs(sum(probabilities.values()) - 1) > 0.03:
                raise TypeSafeError(f"{question_id}: Choice probabilities do not sum to 1")
            continue
        if kind == "score":
            levels = question["criteria"]
            keys = {str(index) for index in range(len(levels))}
            probabilities = answer.get("probabilities")
            legend = answer.get("legend")
            if not isinstance(probabilities, dict) or set(probabilities) != keys:
                raise TypeSafeError(f"{question_id}: Score probability keys differ from levels")
            if legend != {str(index): value for index, value in enumerate(levels)}:
                raise TypeSafeError(f"{question_id}: Score legend differs from levels")
            for key, value in probabilities.items():
                probability(value, f"{question_id}.probabilities.{key}")
            if abs(sum(probabilities.values()) - 1) > 0.03:
                raise TypeSafeError(f"{question_id}: Score probabilities do not sum to 1")
            score = answer.get("score")
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) \
                    or not 0 <= score <= len(levels) - 1:
                raise TypeSafeError(f"{question_id}: invalid score {score!r}")
            probability(answer.get("confidence"), f"{question_id}.confidence")
            expected_score = sum(int(key) * value for key, value in probabilities.items())
            # The API rounds the displayed score and each displayed level probability independently. With
            # five levels those rounded fields can legitimately differ by a few hundredths even though the
            # model's unrounded score was their weighted mean (observed live: 3.89 versus 3.92).
            if abs(score - expected_score) > 0.051:
                raise TypeSafeError(
                    f"{question_id}: score {score:.6f} disagrees with distribution {expected_score:.6f}")
            continue
        raise TypeSafeError(f"{question_id}: unsupported question type {kind!r}")


def article_key(rec):
    return f"{rec['mediaType']}:{rec['tmdbId']}"


def load_articles(path):
    records, keys = [], set()
    with open(path, encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, 1):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{line_number}: malformed JSON: {exc}") from None
            key = article_key(rec)
            if key in keys:
                raise SystemExit(f"{path}:{line_number}: duplicate key {key}")
            if not isinstance(rec.get("text"), str):
                raise SystemExit(f"{path}:{line_number}: {key} has no article text")
            keys.add(key)
            records.append(rec)
    return records, keys


def attach_enriched_evidence(records, directory):
    """Attach newest-wins target year and extractor ``plotSections`` to an older article dump.

    The article dump historically wrote all article headings as ``sections`` but not the extractor's chosen
    headings.  The enriched batches are the authoritative source for the diff and target year.  New dumps
    carry both directly; this fallback lets the complete frozen dump run without re-fetching Wikipedia.
    """
    missing = {article_key(rec) for rec in records
               if "year" not in rec or "plotSections" not in rec}
    if missing:
        if not directory:
            raise SystemExit(
                f"article dump lacks year/plotSections for {len(missing):,} rows; pass --enriched-dir")
        try:
            names = os.listdir(directory)
        except OSError as exc:
            raise SystemExit(f"cannot read --enriched-dir {directory}: {exc}") from None
        batches = []
        for name in names:
            match = re.fullmatch(r"batch-(\d+)\.json", name)
            if match:
                batches.append((int(match.group(1)), name))
        found = {}
        for _, name in sorted(batches, reverse=True):
            with open(os.path.join(directory, name), encoding="utf-8") as fh:
                batch = json.load(fh)
            for item in batch:
                key = f"{item.get('mediaType')}:{item.get('tmdbId')}"
                if key in missing and key not in found:
                    found[key] = item
            if len(found) == len(missing):
                break
        absent = missing - set(found)
        if absent:
            raise SystemExit(f"--enriched-dir has no newest record for {len(absent):,} article rows")
        for rec in records:
            if (item := found.get(article_key(rec))) is not None:
                if rec.get("article") != item.get("plotArticle"):
                    raise SystemExit(
                        f"{article_key(rec)} article differs between dump and enriched evidence")
                if "year" not in rec:
                    rec["year"] = item.get("year")
                if "plotSections" not in rec:
                    rec["plotSections"] = item.get("plotSections") or []
                rec["extractorArticleRevId"] = item.get("plotRevId")

    evidence = {
        article_key(rec): {
            "year": rec.get("year"), "plotSections": rec.get("plotSections") or [],
            "article": rec.get("article"), "articleRevId": rec.get("revId"),
            "extractorArticleRevId": rec.get("extractorArticleRevId", rec.get("revId")),
        }
        for rec in records
    }
    return sha256_text(canonical(evidence))


def repo_relative(path):
    """A path inside the repo, spelled from its root; anything outside it stays absolute.

    The taxonomy is a committed file, so where the checkout sits is not part of what a run bought. It was
    recorded absolute, which put `/Users/<somebody>/…` inside `configSha256` and made the same pass in two
    checkouts two configurations — and made moving the file (oxyc/den-dataset#27) a change to the run's
    identity rather than to a filename.

    The article dump and the enriched batches keep their absolute spelling: they are out-dir working
    files that genuinely live wherever they were written, and `audit_combined_bundle.py` reopens a
    shard's own inputs by those recorded paths. The prompt is committed like the taxonomy and would read
    the same way here, but it has not moved, and every field of this config is part of what a paid run
    is — so it is left as it was recorded rather than respelled for tidiness.
    """
    absolute = os.path.abspath(path)
    inside = os.path.join(ROOT, "")
    return os.path.relpath(absolute, ROOT) if absolute.startswith(inside) else absolute


def manifest_config(args, global_qs, label_mapping, tax, enriched_evidence_sha):
    with open(args.prompt, encoding="utf-8") as fh:
        prompt_sha = sha256_text(fh.read())
    return {
        "schemaVersion": SCHEMA_VERSION,
        "plannerVersion": PLANNER_VERSION,
        "implementationSha256": {name: sha256_file(path) for name, path in IMPLEMENTATION.items()},
        "articles": os.path.abspath(args.articles),
        "articlesSha256": sha256_file(args.articles),
        "enrichedDir": os.path.abspath(args.enriched_dir) if args.enriched_dir else None,
        "enrichedEvidenceSha256": enriched_evidence_sha,
        "prompt": os.path.abspath(args.prompt),
        "promptSha256": prompt_sha,
        "taxonomy": repo_relative(args.taxonomy),
        "taxonomySha256": sha256_file(args.taxonomy),
        "taxonomyVersion": tax["version"],
        "requestedModel": args.model,
        "maxStateChars": args.max_state_chars,
        "globalQuestions": global_qs,
        "globalQuestionsSha256": sha256_text(canonical(global_qs)),
        "sectionQuestionTemplate": section_question("SECTION_ID"),
        "sectionQuestionTemplateSha256": sha256_text(canonical(section_question("SECTION_ID"))),
        "labelQuestionMapping": label_mapping,
        "labelQuestionMappingSha256": sha256_text(canonical(label_mapping)),
    }


def config_differences(recorded, config):
    """The config keys that differ, as `[(key, recorded, now)]`, so a refusal can name them."""
    if not isinstance(recorded, dict):
        return [("<config>", type(recorded).__name__, "object")]
    return [(key, recorded.get(key, "<absent>"), config.get(key, "<absent>"))
            for key in sorted(set(recorded) | set(config))
            if recorded.get(key, "<absent>") != config.get(key, "<absent>")]


def brief(value):
    """One line of a config value, for a refusal message."""
    text = value if isinstance(value, str) else canonical(value)
    return text if len(text) <= 80 else text[:77] + "..."


#: The config keys that say where the vocabulary is written down and in what format, rather than what is
#: in it. `taxonomyVersion` is not one of them: a version bump IS a vocabulary change.
VOCABULARY_SPELLING_KEYS = {"plannerVersion", "taxonomy", "taxonomySha256"}


def vocabulary_only_rewritten(differences):
    """Whether the difference is confined to where the vocabulary lives and how it is written.

    It moved out of `Sources/` and was then converted from Swift source to JSON (oxyc/den-dataset#27),
    which changed its digest without changing a label. That claim is checked rather than asserted:
    `globalQuestionsSha256` and `labelQuestionMappingSha256` are in this same config, so their absence
    from `differences` means the questions this run would ask are the ones the manifest recorded.
    """
    keys = {key for key, _, _ in differences}
    return bool(keys) and keys <= VOCABULARY_SPELLING_KEYS and "taxonomy" in keys


def load_or_create_manifest(path, config):
    config_sha = sha256_text(canonical(config))
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        if manifest.get("configSha256") != config_sha or manifest.get("config") != config:
            differences = config_differences(manifest.get("config"), config)
            lines = [f"{path} belongs to a different input/question/model/planner configuration:"]
            lines += [f"  {key}: manifest {brief(was)} != this run {brief(now)}"
                      for key, was, now in differences]
            if vocabulary_only_rewritten(differences):
                lines.append(
                    "  The questions are unchanged: globalQuestionsSha256 and labelQuestionMappingSha256 "
                    "are not among the keys above, so this run asks exactly what the manifest bought. "
                    "taxonomySha256 differs because the vocabulary moved out of "
                    "Sources/DenDataset/Taxonomy.swift and was converted from Swift source to "
                    "data/genres-moods-vocabulary.json (oxyc/den-dataset#27) — the same labels and the "
                    "same version, in a format that hashes differently. Regional labels are the one part "
                    "no question is built from, so they are the one part that equality does not cover.")
            lines.append(
                "  The rows already in the output were bought under the manifest's configuration and "
                "stay valid; re-stamping the manifest onto this one would erase what produced them. "
                "Finish the remaining titles in a separately manifested shard beside this one and check "
                "the set with pipeline/audit_combined_bundle.py.")
            raise SystemExit("\n".join(lines))
        return manifest
    manifest = {
        "runId": str(uuid.uuid4()),
        "runStartedAt": datetime.now(timezone.utc).isoformat(),
        "configSha256": config_sha,
        "config": config,
    }
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, sort_keys=True, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temporary, path)
    return manifest


def load_done(output, manifest, input_keys):
    done = {}
    if not os.path.exists(output):
        return done
    with open(output, encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                raise SystemExit(
                    f"{output}:{line_number}: malformed JSON; preserve and repair explicitly before resuming")
            key = f"{row.get('mediaType')}:{row.get('tmdbId')}"
            if key in done:
                raise SystemExit(f"{output} contains duplicate key {key}")
            if row.get("runId") != manifest["runId"] \
                    or row.get("configSha256") != manifest["configSha256"]:
                raise SystemExit(f"{output}:{line_number} has incompatible run provenance")
            done[key] = row
    extra = set(done) - input_keys
    if extra:
        raise SystemExit(f"{output} contains {len(extra)} keys absent from the article input")
    return done


def call(client, state, questions, phase, section_ids, abort_event=None):
    if abort_event is not None and abort_event.is_set():
        raise RunAborted("another worker opened the circuit breaker")
    # Every article parses to at least its lead, so a state with no section is a title sent without its
    # article: the model would answer from the title alone and nothing downstream could tell.
    if not state["article"]["sections"]:
        raise ValueError(f"{phase}: refusing to send a state that carries no article section")
    answers, metadata = client.ask_with_metadata(state, questions)
    validate_answers(answers, questions)
    response_model = metadata.get("model")
    if not isinstance(response_model, str) or not response_model:
        raise TypeSafeError(f"{phase}: provider response omitted its model identifier")
    if not client.model.endswith("-latest") and response_model != client.model:
        raise TypeSafeError(
            f"{phase}: provider returned model {response_model!r}, requested pinned {client.model!r}")
    return answers, {
        "phase": phase,
        "sectionIds": section_ids,
        "stateChars": encoded_chars(state),
        "stateSha256": sha256_text(canonical(state)),
        "questionsSha256": sha256_text(canonical(questions)),
        "questionCount": len(questions),
        "responseModel": response_model,
        "inputTokens": metadata["usage"]["input_tokens"],
        "outputTokens": metadata["usage"]["output_tokens"],
    }


def sections_for_record(rec):
    sections = parse_sections(rec["text"], rec.get("plotSections") or ())
    if "sections" in rec and [section["heading"] for section in sections[1:]] != rec["sections"]:
        raise ValueError(f"{article_key(rec)} parsed headings differ from the article dump")
    return sections


def sections_by_id(rec, sections, section_ids):
    """The named sections in source order, refusing a name the article lacks and an empty state."""
    wanted = set(section_ids)
    unknown = sorted(wanted - {section["id"] for section in sections})
    if unknown or not wanted:
        raise ValueError(f"{article_key(rec)}: the state names {unknown or 'no'} sections of an article "
                         f"with {len(sections)}")
    return [section for section in sections if section["id"] in wanted]


def classify(rec, client, global_qs, manifest, max_state_chars, abort_event=None, state_section_ids=None):
    """Answer ``global_qs`` about one article, and every section's role unless ``state_section_ids`` is given.

    ``state_section_ids`` is for a pass that sends a state chosen by an earlier one (the delta pass sends the
    sections the corpus pass sent): it asks no section question, and the state is those sections.
    """
    sections = sections_for_record(rec)
    role_answers, calls = {}, []
    oversized = is_oversized(rec, sections, max_state_chars)
    if state_section_ids is not None:
        global_sections = sections_by_id(rec, sections, state_section_ids)
        oversized = len(global_sections) < len(sections)
        global_answers, provenance = call(
            client, state_for(rec, global_sections), global_qs,
            "global-after-section-audit" if oversized else "global",
            [s["id"] for s in global_sections], abort_event)
        calls.append(provenance)
    elif not oversized:
        section_qs = {f"section__{section['id']}": section_question(section["id"])
                      for section in sections}
        questions = {**global_qs, **section_qs}
        state = state_for(rec, sections)
        answers, provenance = call(
            client, state, questions, "combined", [s["id"] for s in sections], abort_event)
        calls.append(provenance)
        global_answers = {key: answers[key] for key in global_qs}
        role_answers = {section["id"]: answers[f"section__{section['id']}"] for section in sections}
        global_sections = sections
    else:
        groups = section_groups(rec, sections, max_state_chars)
        for group_number, group in enumerate(groups, 1):
            questions = {f"section__{section['id']}": section_question(section["id"])
                         for section in group}
            answers, provenance = call(
                client, state_for(rec, group), questions, f"section-group-{group_number}",
                [s["id"] for s in group], abort_event)
            calls.append(provenance)
            role_answers.update({section["id"]: answers[f"section__{section['id']}"]
                                 for section in group})
        global_sections = select_global_sections(rec, sections, role_answers, max_state_chars)
        global_answers, provenance = call(
            client, state_for(rec, global_sections), global_qs, "global-after-section-audit",
            [s["id"] for s in global_sections], abort_event)
        calls.append(provenance)

    response_models = sorted({entry["responseModel"] for entry in calls})
    selected_counts = {}
    for section in sections:
        if section["extractorSelected"]:
            selected_counts[section["heading"]] = selected_counts.get(section["heading"], 0) + 1
    extractor_counts = {}
    for heading in rec.get("plotSections") or []:
        extractor_counts[heading] = extractor_counts.get(heading, 0) + 1
    missing_extractor_sections = []
    for heading, count in extractor_counts.items():
        missing_extractor_sections.extend([heading] * max(0, count - selected_counts.get(heading, 0)))
    return {
        "schemaVersion": SCHEMA_VERSION,
        "runId": manifest["runId"],
        "configSha256": manifest["configSha256"],
        "mediaType": rec["mediaType"],
        "tmdbId": rec["tmdbId"],
        "title": rec.get("title"),
        "year": rec.get("year"),
        "article": rec.get("article"),
        "language": rec.get("language"),
        "articleRevId": rec.get("revId"),
        "extractorArticleRevId": rec.get("extractorArticleRevId", rec.get("revId")),
        "sectionAuditSameRevision": (
            rec.get("extractorArticleRevId", rec.get("revId")) == rec.get("revId")),
        "extractorSectionsMissingFromArticle": missing_extractor_sections,
        "articleChars": len(rec["text"]),
        "articleSha256": sha256_text(rec["text"]),
        "requestedModel": manifest["config"]["requestedModel"],
        "responseModels": response_models,
        "oversized": oversized,
        "globalStateSectionIds": [section["id"] for section in global_sections],
        "omittedFromGlobalState": [section["id"] for section in sections
                                   if section not in global_sections],
        "calls": calls,
        "answers": global_answers,
        "sections": [public_section(section, role_answers.get(section["id"])) for section in sections],
    }


def plan(records, global_qs, max_state_chars, state_ids_by_key=None):
    """Price a run. ``state_ids_by_key`` (article key -> state section ids) prices
    ``classify(state_section_ids=...)``, refusing a title it has no state for."""
    rows = calls = section_count = decisions = oversized_count = 0
    max_sections = 0
    article_chars = state_chars = question_chars = 0
    oversized_details = []
    for rec in records:
        sections = sections_for_record(rec)
        rows += 1
        section_count += len(sections)
        max_sections = max(max_sections, len(sections))
        article_chars += len(rec["text"])
        if state_ids_by_key is not None:
            if article_key(rec) not in state_ids_by_key:
                raise ValueError(f"{article_key(rec)} has no state to send")
            chosen = sections_by_id(rec, sections, state_ids_by_key[article_key(rec)])
            state_chars += encoded_chars(state_for(rec, chosen))
            question_chars += len(canonical(global_qs))
            calls += 1
            if len(chosen) < len(sections):
                oversized_count += 1
                oversized_details.append({
                    "key": article_key(rec), "title": rec.get("title"), "articleChars": len(rec["text"]),
                    "sections": len(sections), "sectionsSent": len(chosen),
                })
            continue
        decisions += len(sections)
        state = state_for(rec, sections)
        state_chars += min(encoded_chars(state), max_state_chars)
        section_qs = {f"section__{s['id']}": section_question(s["id"]) for s in sections}
        question_chars += len(canonical({**global_qs, **section_qs}))
        if is_oversized(rec, sections, max_state_chars):
            oversized_count += 1
            groups = section_groups(rec, sections, max_state_chars)
            calls += len(groups) + 1
            oversized_details.append({
                "key": article_key(rec), "title": rec.get("title"), "articleChars": len(rec["text"]),
                "sections": len(sections), "sectionGroups": len(groups),
            })
        else:
            calls += 1
    # Approximation only. A smoke call supplies the calibrated budget before full launch.
    estimated_tokens = round((state_chars + question_chars) / 4)
    return {
        "titles": rows,
        "calls": calls,
        "globalQuestions": len(global_qs),
        "sectionDecisions": decisions,
        "meanSections": round(section_count / rows, 2) if rows else 0,
        "maxSections": max_sections,
        "oversizedTitles": oversized_count,
        "oversized": oversized_details,
        "articleChars": article_chars,
        "roughInputTokens": estimated_tokens,
        "roughCostUSD": round(estimated_tokens * TypeSafe.RATE_PER_INPUT_TOKEN, 2),
        "estimateCaveat": "character/4 planning estimate; calibrate with the combined smoke run",
    }


def acquire_output_lock(path):
    """Hold a non-blocking process lock for the entire paid run.

    The output remains append-only, but append-only alone cannot stop two agents from reading the same resume
    set and buying the same calls.  A stale lock file is harmless; the kernel lock, not file existence, is the
    authority.
    """
    handle = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise SystemExit(f"another process holds the paid-run lock {path}") from None
    return handle


def release_output_lock(handle):
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def paid_run(args, global_qs, label_mapping, tax, enriched_evidence_sha,
             records, input_keys, selected_records, state_ids_by_key=None):
    manifest_path = args.manifest or args.out + ".manifest.json"
    config = manifest_config(args, global_qs, label_mapping, tax, enriched_evidence_sha)
    if state_ids_by_key is not None:
        # Which sections each state holds is part of what produced a row.
        config["stateSectionIdsSha256"] = sha256_text(canonical(state_ids_by_key))
    manifest = load_or_create_manifest(manifest_path, config)
    done = load_done(args.out, manifest, input_keys)
    todo = [record for record in selected_records if article_key(record) not in done]
    if args.limit:
        todo = todo[:args.limit]
    if not todo:
        print("nothing to do", file=sys.stderr)
        return 0

    print(f"  {len(todo):,} titles · {len(global_qs)} global questions · model {args.model}", file=sys.stderr)
    client = TypeSafe(model=args.model)
    lock = threading.Lock()
    abort_event = threading.Event()
    abort_reason = None
    output = open(args.out, "a", encoding="utf-8")
    written = failed = 0

    def work(rec):
        nonlocal written, failed, abort_reason
        if abort_event.is_set():
            return
        try:
            state_ids = None if state_ids_by_key is None else state_ids_by_key[article_key(rec)]
            row = classify(rec, client, global_qs, manifest, args.max_state_chars, abort_event, state_ids)
        except RunAborted:
            return
        except (TypeSafeError, ValueError) as exc:
            with lock:
                if not abort_event.is_set():
                    failed += 1
                    abort_reason = f"{article_key(rec)} {rec.get('title')}: {exc}"
                    print(f"  ABORTING after {abort_reason}", file=sys.stderr)
                    abort_event.set()
            return
        with lock:
            output.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            output.flush()
            written += 1
            if written % 250 == 0:
                print(f"  {written:,}/{len(todo):,} · {client.summary()}", file=sys.stderr)

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(work, todo))
    finally:
        output.close()

    print(json.dumps({
        "written": written, "failed": failed, "calls": client.calls,
        "aborted": abort_event.is_set(), "abortReason": abort_reason,
        "inputTokens": client.input_tokens, "spendUSD": round(client.spend, 4),
        "out": args.out, "manifest": manifest_path,
    }))
    return 1 if failed else 0


def argument_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("--articles", required=True)
    parser.add_argument("--enriched-dir",
                        help="newest-wins enriched batches; required for old dumps without plotSections/year")
    parser.add_argument("--out", required=True)
    parser.add_argument("--manifest", help="default: <out>.manifest.json")
    parser.add_argument("--prompt", default=PROMPT)
    parser.add_argument("--taxonomy", default=TAXONOMY)
    parser.add_argument("--model", default=PINNED_MODEL)
    parser.add_argument("--allow-mutable-model", action="store_true")
    parser.add_argument("--max-state-chars", type=int, default=DEFAULT_MAX_STATE_CHARS)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--only-key", help="classify one exact movie:<id>/tv:<id> key (targeted smoke)")
    parser.add_argument("--plan", action="store_true", help="validate and report; make no API calls or files")
    return parser


def main(argv=None):
    args = argument_parser().parse_args(argv)
    if args.model.endswith("-latest") and not args.allow_mutable_model:
        raise SystemExit("refusing mutable model alias; use a pinned model or --allow-mutable-model")
    if args.max_state_chars < 10_000:
        raise SystemExit("--max-state-chars is implausibly small")
    global_qs, label_mapping, tax = global_questions(args.prompt, args.taxonomy)
    records, input_keys = load_articles(args.articles)
    enriched_evidence_sha = attach_enriched_evidence(records, args.enriched_dir)
    if args.only_key and args.limit:
        raise SystemExit("use only one of --only-key and --limit")
    if args.only_key:
        selected_records = [record for record in records if article_key(record) == args.only_key]
        if not selected_records:
            raise SystemExit(f"--only-key {args.only_key} is absent from the article input")
    else:
        selected_records = records[:args.limit] if args.limit else records
    if args.plan:
        print(json.dumps(plan(selected_records, global_qs, args.max_state_chars), ensure_ascii=False, indent=2))
        return 0

    lock_handle = acquire_output_lock(args.out + ".lock")
    try:
        return paid_run(args, global_qs, label_mapping, tax, enriched_evidence_sha,
                        records, input_keys, selected_records)
    finally:
        release_output_lock(lock_handle)


if __name__ == "__main__":
    sys.exit(main())
