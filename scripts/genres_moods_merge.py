#!/usr/bin/env python3
"""`./den genres-moods merge`, and the validator the labelling agents run. See `genres_moods_enrich.py`.

An answer batch is valid when it answers every key of its input batch exactly once and nothing else, with
exactly `{key, primary_genre, subgenres, moods}`: a primary genre from the vocabulary, and per family at
most three distinct `{label, confidence}` from that family's vocabulary with confidence a number in
[0, 1]. With `--web`, `<batch>.sources.json` must name at least one http(s) URL for every key.
"""
import datetime
import json
import os
import random
import re
import subprocess
import sys
import tempfile
from collections import Counter

import genres_moods_enrich as gm
from pipeline.contract import StageError

EVAL = os.path.join(gm.HERE, "eval-taxonomy.py")
GOLDEN = os.path.join(gm.REPO, "data", "eval", "golden-large.json")
FLOORS = os.path.join(gm.REPO, "data", "eval", "quality-floors.json")
#: `eval-taxonomy.py`'s `WOULD_LOWER`: --record declining to lower the baseline. Kept as a number rather
#: than imported, because the script's name is not an importable module.
WOULD_LOWER = 3
#: July's `assemble` cut-offs: blended subgenres, themes, moods; and the cap per family.
T_SUB, T_THEME, T_MOOD, CAP = 0.55, 0.50, 0.55, 3
MODEL = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
FIELDS = {"key", "primary_genre", "subgenres", "moods"}


def check_batch(items, answers, vocab):
    """The problems with one answer batch against its input, empty when it is fine."""
    if not isinstance(answers, list):
        return ["top level must be a JSON array"]
    want, seen, problems = {i["key"] for i in items}, Counter(), []
    families = {"subgenres": set(vocab["sub"]) | set(vocab["theme"]), "moods": set(vocab["mood"])}
    for n, a in enumerate(answers):
        if not isinstance(a, dict):
            problems.append(f"item {n}: not an object")
            continue
        where = a.get("key") or f"item {n}"
        seen[a.get("key")] += 1
        if a.get("key") not in want:
            problems.append(f"{where}: key not in the input batch")
        if set(a) != FIELDS:
            problems.append(f"{where}: fields must be exactly {sorted(FIELDS)}, got {sorted(a)}")
        if a.get("primary_genre") not in vocab["primary"]:
            problems.append(f"{where}: primary_genre {a.get('primary_genre')!r} is not a primary genre")
        for field, allowed in families.items():
            entries = a.get(field)
            if not isinstance(entries, list):
                problems.append(f"{where}: {field} must be a list")
                continue
            if len(entries) > CAP:
                problems.append(f"{where}: {field} has {len(entries)} labels (at most {CAP})")
            labels = []
            for e in entries:
                if not isinstance(e, dict) or set(e) != {"label", "confidence"}:
                    problems.append(f"{where}: {field} entry {e!r} must be exactly {{label, confidence}}")
                    continue
                if e["label"] not in allowed:
                    problems.append(f"{where}: {e['label']!r} is not in the {field} vocabulary")
                c = e["confidence"]
                if isinstance(c, bool) or not isinstance(c, (int, float)) or not 0 <= c <= 1:
                    problems.append(f"{where}: {e['label']!r} confidence {c!r} is not a number in [0, 1]")
                labels.append(e["label"])
            if len(set(labels)) != len(labels):
                problems.append(f"{where}: {field} repeats a label")
    problems += [f"{k}: answered {n} times" for k, n in seen.items() if n > 1 and k in want]
    problems += [f"{k}: not answered" for k in sorted(want - set(seen))]
    return problems


def check_sources(items, sources):
    if not isinstance(sources, dict):
        return ["sources must be a JSON object of key -> list of URLs"]
    want, problems = {i["key"] for i in items}, []
    problems += [f"{k}: sources for a key not in the batch" for k in sorted(set(sources) - want)]
    for key in sorted(want):
        urls = sources.get(key)
        if not isinstance(urls, list) or not urls or not all(
                isinstance(u, str) and re.match(r"https?://\S+$", u) for u in urls):
            problems.append(f"{key}: sources must list at least one http(s) URL")
    return problems


def load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh), None
    except FileNotFoundError:
        return None, f"{path}: missing"
    except ValueError as exc:
        return None, f"{path}: not readable JSON: {exc}"


def batch_problems(work, name, vocab, web):
    """(problems, answers, sources) for the batch called `name` in the work dir."""
    items, _ = load_json(os.path.join(work, "in", name))
    if items is None:
        return [f"no input batch in/{name}"], None, None
    answers, err = load_json(os.path.join(work, "out", name))
    problems = [err] if err else check_batch(items, answers, vocab)
    sources = None
    if web:
        sources, err = load_json(os.path.join(work, "out", name[:-5] + ".sources.json"))
        problems += [err] if err else check_sources(items, sources)
    return problems, answers, sources


def validate_main(argv, work):
    """The work dir's `validate.py`: `ok`, or the problems, for one answer batch."""
    web = "--web" in argv
    paths = [a for a in argv if a != "--web"]
    if len(paths) != 1:
        print("usage: python3 validate.py [--web] out/batch-NNN.json")
        return 2
    problems = batch_problems(work, os.path.basename(paths[0]), gm.vocabulary(), web)[0]
    print("ok" if not problems else "\n".join(f"- {p}" for p in problems))
    return 1 if problems else 0


def assemble(answer, vocab):
    """July's `assemble` for one pass: per family, the labels at or above their cut-off, strongest first
    (ties by label), top 3."""
    def keep(entries, threshold):
        kept = [(float(e["confidence"]), e["label"]) for e in entries]
        kept = sorted((x for x in kept if x[0] >= threshold(x[1])), key=lambda x: (-x[0], x[1]))
        return [{"confidence": c, "label": l} for c, l in kept[:CAP]]
    sub = set(vocab["sub"])
    return {"primaryGenre": answer["primary_genre"],
            "subgenres": keep(answer["subgenres"], lambda l: T_SUB if l in sub else T_THEME),
            "moods": keep(answer["moods"], lambda l: T_MOOD)}


def dump_curated(head, titles):
    """The curated file's encoding: the head on the first line, then one title per line."""
    lines = [json.dumps(head, ensure_ascii=False, sort_keys=True)[:-1] + ',"titles":{']
    items = list(titles.items())
    for i, (key, entry) in enumerate(items):
        comma = "," if i < len(items) - 1 else ""
        lines.append(f"{json.dumps(key)}:{json.dumps(entry, ensure_ascii=False, sort_keys=True)}{comma}")
    lines.append("}}")
    return "\n".join(lines) + "\n"


def run_eval(path, golden, floors, flag):
    return subprocess.run([sys.executable, EVAL, path, "--golden", golden, "--floors", floors, flag],
                          capture_output=True, text=True)


def merge(work, model, web=False, accept_drop=False, curated=gm.CURATED, golden=GOLDEN, floors=FLOORS,
          today=None, out=print):
    if not MODEL.match(model or ""):
        raise StageError(f"--model {model!r}: lower-case letters, digits, dots and dashes, e.g. sonnet")
    work = os.path.abspath(work)
    record, err = load_json(os.path.join(work, "prepare.json"))
    if record is None:
        raise StageError(f"{err} — run `./den genres-moods prepare` first")
    if not record["batches"]:
        raise StageError(f"{work} was prepared with no titles; there is nothing to merge")
    vocab = gm.vocabulary()
    if record["taxonomyVersion"] != vocab["version"]:
        raise StageError(f"prepared under taxonomy {record['taxonomyVersion']}, the taxonomy is now "
                         f"{vocab['version']}")
    answers, sources, failed = {}, {}, {}
    for name in sorted(record["batches"]):
        problems, batch, cited = batch_problems(work, name, vocab, web)
        if problems:
            failed[name] = problems
            continue
        answers.update({a["key"]: a for a in batch})
        sources.update(cited or {})
    if failed:
        for name, problems in failed.items():
            out(f"{name}: {len(problems)} problem(s)")
            for p in problems[:10]:
                out(f"  - {p}")
        raise StageError(f"{len(failed)} of {len(record['batches'])} batches do not validate; "
                         f"nothing written")

    head, titles = gm.read_curated(curated)
    if head.get("taxonomyVersion") != vocab["version"]:
        raise StageError(f"{curated} is taxonomy {head.get('taxonomyVersion')}, not {vocab['version']}")
    source = f"enrichment-{today or datetime.date.today().isoformat()}-{model}" + ("-web" if web else "")
    before = {k: titles.get(k) for k in answers}
    for key in sorted(answers, key=gm.sort_key):
        prior = titles.get(key)
        if prior is None and key not in record["animated"]:
            raise StageError(f"{key} is not in {curated} and was not new when prepared — prepare again")
        entry = {**assemble(answers[key], vocab),
                 "animated": prior["animated"] if prior else record["animated"][key],
                 "primaryGenreSource": source, "source": source}
        if web:
            entry["webSources"] = sources[key]
        titles[key] = entry
    head["sources"] = {**head.get("sources", {}), source: (
        f"Hand enrichment by {model} labelling agents" + (" with web search" if web else "") +
        " (`./den genres-moods`, scripts/genres-moods-enrich.SPEC.md), assembled at 0.55 subgenre / 0.50 "
        "theme / 0.55 mood, top 3." + (" The pages read per title are in its `webSources`." if web else "") +
        " `animated` is the enriched batch row's, read off Wikidata's genres and types, for a title the file"
        " did not have, and kept otherwise.")}
    head["count"] = len(titles)

    fd, candidate = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(curated)), prefix=".tmp-",
                                     suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(dump_curated(head, titles))
    gate = run_eval(candidate, golden, floors, "--gate")
    gate.stderr = gate.stderr.replace(candidate, curated)  # its hints name the file the operator has
    passed = gate.returncode == 0
    if not passed and not accept_drop:
        os.unlink(candidate)
        out(gate.stdout + gate.stderr)
        raise StageError(f"the result is below the quality floors; {curated} is unchanged. Rerun with "
                         f"--accept-drop to write it anyway")
    os.replace(candidate, curated)
    summary = summarise(before, titles, answers, work, source, out)
    out(gate.stdout.strip())
    if passed:
        recorded = run_eval(curated, golden, floors, "--record")
        if recorded.returncode == WOULD_LOWER:
            # Inside the tolerance, so the gate passed, but still under the baseline. The ratchet only
            # ever rises on its own; lowering it is the operator's `--record --accept-drop` and a commit.
            out(recorded.stderr.strip())
            out(f"\nThe gate passed, and the baseline in {floors} is left where it was. Review the "
                f"summary, then commit {curated}.")
        elif recorded.returncode != 0:
            raise StageError(f"the gate passed but recording the baseline failed:\n{recorded.stderr}")
        else:
            out(f"\nThe gate passed and the baseline in {floors} is recorded against the new file. "
                f"Review the summary, then commit both.")
    else:
        out(gate.stderr.strip())
        out(f"\nWritten below the baseline (--accept-drop). Record it and commit both files:\n"
            f"  scripts/eval-taxonomy.py {curated} --record --accept-drop")
    return {**summary, "source": source, "gatePassed": passed}


def describe(entry):
    if entry is None:
        return "(none)"
    fmt = lambda xs: ", ".join(f"{x['label']} {x['confidence']:g}" for x in xs) or "—"  # noqa: E731
    return f"{entry['primaryGenre']} | {fmt(entry['subgenres'])} | {fmt(entry['moods'])}"


def label_counts(entries):
    counts = Counter()
    for e in entries:
        counts[("primary", e.get("primaryGenre"))] += 1
        counts.update(("subgenre", x["label"]) for x in e.get("subgenres") or [])
        counts.update(("mood", x["label"]) for x in e.get("moods") or [])
    return counts


def summarise(before, titles, answers, work, source, out):
    """What a reviewer reads: titles added and changed, label count shifts, and ten before/after examples."""
    added = sorted((k for k in answers if before[k] is None), key=gm.sort_key)
    changed = sorted((k for k in answers if before[k] is not None
                      and describe(before[k]) != describe(titles[k])), key=gm.sort_key)
    old = label_counts(e for e in before.values() if e)
    new = label_counts(titles[k] for k in answers)
    shifts = {fl: new[fl] - old[fl] for fl in set(old) | set(new) if new[fl] != old[fl]}
    out(f"{source}: {len(answers)} titles — {len(added)} added, {len(changed)} changed, "
        f"{len(answers) - len(added) - len(changed)} unchanged")
    out("label count shifts:")
    for (family, label), delta in sorted(shifts.items(), key=lambda s: (-abs(s[1]), s[0])):
        out(f"  {family:<9} {label:<28} {delta:+d}")
    names = {}
    for name in os.listdir(os.path.join(work, "in")):
        names.update({i["key"]: i.get("title") for i in load_json(os.path.join(work, "in", name))[0]})
    pool = added + changed
    examples = random.Random(source).sample(pool, min(10, len(pool)))
    out("examples (primary | subgenres | moods):")
    for key in examples:
        out(f"  {key} {names.get(key) or ''}\n    before: {describe(before[key])}\n"
            f"    after:  {describe(titles[key])}")
    return {"added": added, "changed": changed, "shifts": shifts, "examples": examples}
