#!/usr/bin/env python3
"""The GENRES & MOODS stage: ask Jev about the titles the curated file cannot answer, then derive.

Genres & moods are a primary genre, up to three subgenres/themes and up to three moods per title. The ones
in `data/genres-moods-curated.json` came from Claude labellers and a hand enrichment and are kept as they
are. This stage labels the rest with Jev, the automated labeller chosen in oxyc/den-dataset#56:

**Ask** (paid, only with `--spend`). Titles the curated file lacks, and curated titles with neither
subgenres nor moods, are sent the lead plus every section the classify pass rated story-premise or
theme-subject — the selection `pipeline/genres_moods_enrich.py` makes for the hand enrichment — with the v3
question set below. A title classify judged not about the requested work, with no article, or whose article
changed since classify read it, is not asked. The call, validation, resume, lock and manifest are
`run_combined`'s, imported unchanged, as `run_delta.py` does. Answers go to one shard per article dump, and a
title answered in any shard is never asked again.

**Derive** (free, every run). `genres-moods.json` is the curated file plus, for every answered title,
genres & moods under `data/genres-moods-rule.json`: the primary genre is the Choice's argmax; subgenres
and moods keep each label whose Noul clears its own threshold, strongest first, top three, with the
family's "most defining" pick added when it reaches the rule's `pick`. A curated title with neither
subgenres nor moods gets the derived ones and keeps its primary genre. A title classify now judges not
about the requested work gets nothing. The result must clear the quality baseline
(`pipeline/eval_taxonomy.py --gate`) or nothing is written.

**A rebuilt out-dir** (oxyc/den-dataset#27) holds only today's answer shards. The entries derived from earlier
days' answers come from the live dataset's `genres-moods.json` (`published_genres_moods`), and are applied
as those answers were — a title the curated file lacks, or a curated title with neither subgenres nor moods —
under this run's answers and the curated file as it is now.

The rule's digest is recorded in the output rather than in the answers' manifest: the manifest decides
whether a shard can be resumed, and a new rule must not make bought answers unresumable.
"""
import argparse
import hashlib
import json
import os
import sys
import tempfile

from . import artifacts, finalize
from . import genres_moods_enrich as gm
from . import genres_moods_merge as gmm
from . import run_combined as rc
from .combined_questions import PINNED_MODEL, slug, taxonomy
from .contract import REPO, StageError

NAME = "genres_moods"
PRODUCER = "pipeline/genres_moods.py"
HOW = "./den stage genres_moods (add --spend to ask Jev about unanswered titles first)"
PUBLISHES = False
#: The ask buys from Jev: ~$0.0004 a title, ~$18 for the whole corpus.
SPENDS = True
#: The derive step buys nothing, so `den run` runs this stage without `--spend` too, with `ctx.spend`
#: false: it derives from the answers already on disk and asks nothing.
FREE_WITHOUT_SPEND = True

INPUTS = (artifacts.ARTICLES, artifacts.ENRICHED, artifacts.COMBINED, artifacts.WITHDRAWN,
          artifacts.PUBLISHED_GENRES_MOODS)
OUTPUTS = (artifacts.GENRES_MOODS_ANSWERS, artifacts.GENRES_MOODS_ANSWERS_MANIFEST, artifacts.GENRES_MOODS)

CURATED = gm.CURATED
RULE = os.path.join(REPO, "data", "genres-moods-rule.json")
GOLDEN = gmm.GOLDEN
FLOORS = gmm.FLOORS
#: The `source` a derived entry carries.
SOURCE = "jev-v3"

EVIDENCE = ("Judge the requested work (film or series) described by the supplied article sections, together "
            "with what is widely known about that work. ")
NOUL = ("Is `{label}` one of the few labels that genuinely characterise the requested work? "
        "`{label}` means: {definition}")


def questions():
    """(questions, label mapping, taxonomy) — the v3 set the rule was fitted on.

    One Choice for the primary genre, one "most defining" Choice per family, and one Noul per subgenre,
    theme and mood. The definitions are `data/genres-moods-definitions.json`, which `gm.vocabulary()`
    refuses unless it defines exactly the taxonomy's labels.
    """
    vocab = gm.vocabulary()
    defs = vocab["definitions"]
    sub_labels = vocab["sub"] + vocab["theme"]
    qs = {
        "gm__primary_genre": {
            "type": "choice",
            "instructions": EVIDENCE + "Which ONE genre shelf does it fundamentally belong on? Pick what the work "
                            "is, not its setting or a secondary ingredient. Animation is a format, not a genre: "
                            "an animated work gets its story genre, and children's animation is Family.",
            "criteria": {g: defs["primaryGenres"][g] for g in vocab["primary"]},
        },
        "gm__pick__subgenre": {
            "type": "choice",
            "instructions": EVIDENCE + "Which ONE subgenre or theme label most defines it? `none-fits` only if no "
                            "label below clearly applies.",
            "criteria": {**{l: defs["subgenres"][l] for l in sub_labels},
                         "none-fits": "No listed subgenre or theme clearly applies."},
        },
        "gm__pick__mood": {
            "type": "choice",
            "instructions": EVIDENCE + "Which ONE mood best describes the experience of watching it? `none-fits` "
                            "only if no mood below clearly applies.",
            "criteria": {**{l: defs["moods"][l] for l in vocab["mood"]},
                         "none-fits": "No listed mood clearly applies."},
        },
    }
    mapping = {}
    for family, labels, table in (("subgenre", sub_labels, defs["subgenres"]),
                                  ("mood", vocab["mood"], defs["moods"])):
        for label in labels:
            qid = f"gm__{family}__{slug(label)}"
            qs[qid] = {"type": "noul", "instructions": NOUL.format(label=label, definition=table[label])}
            mapping[qid] = {"family": family, "label": label}
    return qs, mapping, taxonomy()


def load_rule(path, vocab):
    """The derive rule, refusing a family, type or label it does not know."""
    with open(path, encoding="utf-8") as fh:
        rule = json.load(fh)
    known = {"subgenre": set(vocab["sub"]) | set(vocab["theme"]), "mood": set(vocab["mood"])}
    if set(rule) != set(known):
        raise StageError(f"{path}: families {sorted(rule)}, want {sorted(known)}")
    for family, spec in rule.items():
        if spec.get("type") != "perlabel":
            raise StageError(f"{path}: {family} is {spec.get('type')!r}; only per-label thresholds are derived")
        unknown = sorted(set(spec["t"]) - known[family])
        if unknown:
            raise StageError(f"{path}: {family} thresholds for labels outside the taxonomy: {unknown}")
    return rule


def family_labels(answers, mapping, family, spec):
    """One family's labels: each label whose Noul clears its threshold, strongest first (ties by label),
    capped; the family's pick goes first when it reaches `pick` and is not already kept."""
    probs = {m["label"]: answers[q]["noul"] for q, m in mapping.items() if m["family"] == family}
    ranked = sorted(probs.items(), key=lambda x: (-x[1], x[0]))
    labels = [l for l, p in ranked if p >= spec["t"].get(l, spec["default"])][:spec["cap"]]
    pick = answers[f"gm__pick__{family}"]
    if spec.get("pick") is not None and pick["choice"] != "none-fits" \
            and pick["probabilities"][pick["choice"]] >= spec["pick"] and pick["choice"] not in labels:
        labels = ([pick["choice"]] + labels)[:spec["cap"]]
    return [{"confidence": probs[l], "label": l} for l in labels]


def derive_record(answers, mapping, rule):
    return {"primaryGenre": answers["gm__primary_genre"]["choice"],
            "subgenres": family_labels(answers, mapping, "subgenre", rule["subgenre"]),
            "moods": family_labels(answers, mapping, "mood", rule["mood"])}


def read(path):
    """`genres-moods.json` as `{mediaType:tmdbId: record}`, each entry in the record shape `docfacts`, `embed`,
    `worklist` and `finalize` read: the one place this file's shape is converted for them.

    The file keys titles by `mediaType:tmdbId` and gives each entry its own `source`/`primaryGenreSource`;
    the record carries the key's two halves and the `source` every model-made label has in the embed
    stores and `labels-t02.json` (`llm`). A file with no titles is refused: read as an empty map it would
    embed nothing and tell a delta that nothing is published.
    """
    with open(path, encoding="utf-8") as fh:
        blob = json.load(fh)
    titles = blob.get("titles") if isinstance(blob, dict) else None
    if not isinstance(titles, dict) or not titles:
        raise StageError(f"{path} holds no genres & moods titles. Build it with: {HOW}")
    out = {}
    for key, entry in titles.items():
        media, _, ident = key.partition(":")
        if not (ident.isascii() and ident.isdigit()) or not isinstance(entry, dict):
            raise StageError(f"{path}: {key!r} is not a mediaType:tmdbId genres & moods entry")
        out[key] = finalize.parse_record({**entry, "mediaType": media, "tmdbId": int(ident), "source": "llm"},
                                         f"{path} {key}")
    return out


def answered_keys(paths):
    """Every title any shard holds an answer for. Kept separate from `read_answers` because the ask needs
    only the keys, and an answer row is 78 probabilities it would otherwise hold for the whole corpus."""
    keys = set()
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    keys.add(gm.key_of(json.loads(line)))
    return keys


def read_answers(paths):
    """`key` → (answers, label mapping) over every shard, and each shard's provenance.

    A shard is read only beside its manifest and only with rows that manifest bought; a title answered in
    two shards is refused, because which answer counts would then be an accident of file order.
    """
    out, provenance = {}, []
    for path in paths:
        manifest_path = path + ".manifest.json"
        if not os.path.exists(manifest_path):
            raise StageError(f"{path} has no manifest at {manifest_path}, so nothing says what bought it")
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        mapping = manifest["config"]["labelQuestionMapping"]
        rows = 0
        with open(path, encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                row = json.loads(line)
                key = gm.key_of(row)
                if row.get("runId") != manifest["runId"] or row.get("configSha256") != manifest["configSha256"]:
                    raise StageError(f"{path}:{n}: {key} was not bought by the run its manifest describes")
                if key in out:
                    raise StageError(f"{key} is answered in two shards; the second is {path}")
                out[key] = (row["answers"], mapping)
                rows += 1
        provenance.append({"shard": os.path.basename(path), "runId": manifest["runId"], "rows": rows,
                           "configSha256": manifest["configSha256"],
                           "questionsSha256": manifest["config"]["globalQuestionsSha256"],
                           "requestedModel": manifest["config"]["requestedModel"],
                           "taxonomyVersion": manifest["config"]["taxonomyVersion"]})
    return out, provenance


def _articles(ctx):
    """The article dump with the enrichment's year and plot headings attached, as the classify pass read it,
    and the evidence digest its manifest records."""
    enriched = ctx.path(artifacts.ENRICHED)
    try:
        records, keys = rc.load_articles(ctx.require(artifacts.ARTICLES))
        evidence = rc.attach_enriched_evidence(records, enriched if os.path.isdir(enriched) else None)
    except SystemExit as exc:
        raise StageError(f"genres_moods: {exc}") from None
    return records, keys, evidence


def selection(ctx, records):
    """What to ask, and the state each askable title is sent.

    `states` covers every title a premise can be built for, not only the ones asked: the answers' manifest
    hashes it, so it must not move when the curated file does or a shard fills up.
    """
    _, titles = gm.read_curated(CURATED)
    classify = gm.read_classify(ctx.require_all(artifacts.COMBINED), ctx.require(artifacts.WITHDRAWN))
    by_key = {rc.article_key(r): r for r in records}
    states, unaskable = {}, {}
    for key, info in classify.items():
        article = by_key.get(key)
        reason = gm.skip_reason(info, article)
        if reason is None:
            text = gm.premise(article, info["keep"])
            reason = ("the article changed since the classify pass judged its sections" if text is None
                      else "empty premise" if not text.strip() else None)
        if reason:
            unaskable[key] = reason
        else:
            states[key] = [sid for sid, _ in info["keep"]]
    animated = gm.read_animated(ctx.path(artifacts.ENRICHED))
    answered = answered_keys(ctx.paths(artifacts.GENRES_MOODS_ANSWERS))
    todo, skipped, done = [], {}, 0
    for key in gm.select("missing", titles, classify):
        reason = unaskable.get(key) or ("new, with no enrichment row to take `animated` from"
                                        if key not in titles and key not in animated else None)
        if reason:
            skipped.setdefault(reason, []).append(key)
        elif key in answered:
            done += 1
        else:
            todo.append(key)
    return {"todo": todo, "states": states, "skipped": skipped, "answered": done,
            "new": sum(1 for k in todo if k not in titles)}


def shard_path(ctx, articles_sha):
    """The shard this article dump's answers go to: the declared name with the dump's digest in the
    wildcard, unless the run names one outright."""
    if artifacts.GENRES_MOODS_ANSWERS.name in ctx.overrides:
        return ctx.shard(artifacts.GENRES_MOODS_ANSWERS)
    return ctx.shard(artifacts.GENRES_MOODS_ANSWERS)[:-len(".jsonl")] + f"-{articles_sha[:12]}.jsonl"


def plan(ctx):
    """The ask's plan, printed: titles, calls and the rough cost. Reads everything, writes nothing."""
    qs, _, _ = questions()
    records, _, _ = _articles(ctx)
    chosen = selection(ctx, records)
    todo = set(chosen["todo"])
    estimate = rc.plan([r for r in records if rc.article_key(r) in todo], qs, rc.DEFAULT_MAX_STATE_CHARS,
                       state_ids_by_key=chosen["states"])
    report = {"ask": len(todo), "askNew": chosen["new"], "askCuratedEmpty": len(todo) - chosen["new"],
              "alreadyAnswered": chosen["answered"],
              "skipped": {reason: len(keys) for reason, keys in sorted(chosen["skipped"].items())},
              **{k: estimate[k] for k in ("calls", "globalQuestions", "roughInputTokens", "roughCostUSD",
                                          "estimateCaveat")}}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def ask(ctx):
    """Buy the answers for every title selected and not yet answered. Returns how many were asked."""
    qs, mapping, tax = questions()
    records, input_keys, evidence = _articles(ctx)
    chosen = selection(ctx, records)
    for reason, keys in sorted(chosen["skipped"].items()):
        print(f"genres_moods: not asking {len(keys)}: {reason}, e.g. {', '.join(keys[:4])}", file=sys.stderr)
    todo = set(chosen["todo"])
    if not todo:
        return 0
    articles_path = ctx.require(artifacts.ARTICLES)
    out = shard_path(ctx, rc.sha256_file(articles_path))
    enriched = ctx.path(artifacts.ENRICHED)
    args = argparse.Namespace(articles=articles_path, enriched_dir=enriched if os.path.isdir(enriched) else None,
                              out=out, manifest=None, prompt=rc.PROMPT, taxonomy=rc.TAXONOMY,
                              model=PINNED_MODEL, max_state_chars=rc.DEFAULT_MAX_STATE_CHARS, workers=8,
                              limit=ctx.limit)
    selected = [r for r in records if rc.article_key(r) in todo]
    lock = None
    try:
        lock = rc.acquire_output_lock(out + ".lock")
        failed = rc.paid_run(args, qs, mapping, tax, evidence, records, input_keys, selected,
                             state_ids_by_key=chosen["states"])
    except SystemExit as exc:
        raise StageError(f"genres_moods: {exc}") from None
    finally:
        if lock is not None:
            rc.release_output_lock(lock)
    if failed:
        raise StageError(f"genres_moods: the ask stopped on a failed call; what was bought is in {out} and a "
                         f"rerun resumes from it")
    # `--limit` stops the pass short, so what it asked is not the whole selection.
    return min(len(todo), ctx.limit) if ctx.limit else len(todo)


def derive(ctx):
    """Write `genres-moods.json`: the curated entries plus the derived ones, behind the quality gate."""
    vocab = gm.vocabulary()
    head, titles = gm.read_curated(CURATED)
    if head.get("taxonomyVersion") != vocab["version"]:
        raise StageError(f"{CURATED} is taxonomy {head.get('taxonomyVersion')}, not {vocab['version']}")
    rule = load_rule(RULE, vocab)
    answers, provenance = read_answers(ctx.paths(artifacts.GENRES_MOODS_ANSWERS))
    stale = sorted({p["taxonomyVersion"] for p in provenance} - {vocab["version"]})
    if stale:
        raise StageError(f"genres_moods: answers asked under taxonomy {stale}, the taxonomy is {vocab['version']}")
    # The classify rows are required whether or not anything was answered — they are this stage's input,
    # and an out-dir without them is not one this stage can speak for. Parsing them is what waits on an
    # answer: the shards are 680 MB, and with nothing derived nothing would be asked of them.
    base = ctx.require(artifacts.PUBLISHED_GENRES_MOODS)
    shards = ctx.paths(artifacts.COMBINED) if base else ctx.require_all(artifacts.COMBINED)
    classify = gm.read_classify(shards, ctx.require(artifacts.WITHDRAWN)) if answers else {}
    animated = gm.read_animated(ctx.path(artifacts.ENRICHED)) if answers else {}

    counts = {"kept": 0, "derived": 0, "filled": 0, "not about the requested work": 0,
              "no enrichment row": 0, "nothing cleared the rule": 0}
    new = {}
    if base:
        # Earlier days' derivations, applied as they were made; this run's answers replace them below.
        with open(base, encoding="utf-8") as fh:
            published = json.load(fh)
        for key, entry in (published.get("titles") or {}).items():
            if entry.get("source") != SOURCE or key in answers:
                continue
            prior = titles.get(key)
            if prior is None and entry.get("primaryGenreSource") == SOURCE:
                new[key] = entry
                counts["derived"] += 1
            elif prior is not None and not (prior.get("subgenres") or prior.get("moods")):
                titles[key] = {**prior, "subgenres": entry["subgenres"], "moods": entry["moods"], "source": SOURCE}
                counts["filled"] += 1
        head_base = {"published": hashlib.sha256(json.dumps(published, sort_keys=True).encode()).hexdigest(),
                     "answers": (published.get("derivation") or {}).get("answers", [])}
    for key in sorted(answers, key=gm.sort_key):
        prior = titles.get(key)
        if prior is not None and (prior.get("subgenres") or prior.get("moods")):
            counts["kept"] += 1
            continue
        info = classify.get(key)
        if info is None or info["validity"] != gm.VALID:
            counts["not about the requested work"] += 1
            continue
        record = derive_record(answers[key][0], answers[key][1], rule)
        if prior is not None:
            if not record["subgenres"] and not record["moods"]:
                counts["nothing cleared the rule"] += 1
                continue
            titles[key] = {**prior, "subgenres": record["subgenres"], "moods": record["moods"], "source": SOURCE}
            counts["filled"] += 1
        elif key not in animated:
            counts["no enrichment row"] += 1
        else:
            new[key] = {**record, "animated": animated[key], "primaryGenreSource": SOURCE, "source": SOURCE}
            counts["derived"] += 1
    titles.update(new)

    with open(RULE, "rb") as fh:
        rule_sha = hashlib.sha256(fh.read()).hexdigest()
    with open(CURATED, "rb") as fh:
        curated_sha = hashlib.sha256(fh.read()).hexdigest()
    head["sources"] = {**head.get("sources", {}), SOURCE: (
        f"Jev ({PINNED_MODEL}) answering the v3 genres & moods questions (pipeline/genres_moods.py) over the "
        "lead and the sections the classify pass rated story-premise or theme-subject. Primary genre is the "
        "Choice's argmax; subgenres and moods keep each label whose Noul clears its threshold in "
        "data/genres-moods-rule.json, top 3, plus the most-defining pick at >= 0.5. It labels titles the "
        "curated file lacks, and fills the subgenres and moods of curated titles that had neither, keeping "
        "their primary genre.")}
    head["count"] = len(titles)
    head["derivation"] = {"curatedSha256": curated_sha, "rule": os.path.relpath(RULE, REPO),
                          "ruleSha256": rule_sha, "answers": provenance}
    if base:
        head["derivation"]["base"] = head_base

    path = ctx.path(artifacts.GENRES_MOODS)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd, candidate = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".tmp-genres-moods-",
                                     suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(gmm.dump_curated(head, titles))
    gate = gmm.run_eval(candidate, GOLDEN, FLOORS, "--gate")
    if gate.returncode != 0:
        os.unlink(candidate)
        raise StageError(f"genres_moods: the derived genres & moods are below the quality floors, so {path} "
                         f"was not written:\n{gate.stdout}{gate.stderr.replace(candidate, path)}")
    os.replace(candidate, path)
    return counts


def run(ctx):
    """`--plan` prints the ask's plan and writes nothing; `--spend` asks, then derives; otherwise derive."""
    if ctx.plan:
        report = plan(ctx)
        return f"planned only — {report['ask']:,} titles to ask, ~${report['roughCostUSD']:,.2f}; nothing written"
    asked = ask(ctx) if ctx.spend else 0
    counts = derive(ctx)
    return (f"{ctx.path(artifacts.GENRES_MOODS)} — asked {asked:,}; " +
            ", ".join(f"{name} {n:,}" for name, n in counts.items() if n))
