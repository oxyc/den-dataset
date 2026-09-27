#!/usr/bin/env python3
"""The FRANCHISES stage: which titles are one franchise, in eras, from Wikidata, asking Jev what it cannot say.

oxyc/den-atlas#92: a film or TV series like Beck is a franchise the way Spider-Man is, with a row of its own in
release order and its members left out of More Like This. One franchise, split into eras (Spider-Man: Raimi,
Webb, the MCU, Spider-Verse; Beck: the 1960s–70s films, Gösta Ekman, Peter Haber, the TV series); a TV series
joins its films; a shared universe (the MCU) is an umbrella, not the franchise. The dataset is the source of
truth, so every input is Wikidata's and every answer ours.

**Groups** (`franchise_groups`, free). A title's P179 series, P8345 media franchise, its source's book series
(P144 → P179) and its P155/P156 sequel chain, each series with the bigger ones it is part of. Where they
leave one answer the franchise is decided here and nobody is asked.

**Ask** (paid, only with `--spend`). Every other title is sent its article's lead and one more section,
"Franchise candidates (from Wikidata)", listing its candidate groups A to D with their titles in release
order; the questions below are the same for every title, and point at the letters. The article file the
pass reads is written here (`FRANCHISE_STATES`, one shard per content digest), so `run_combined`'s call,
validation, resume, lock and manifest are used unchanged, as `run_delta.py` and `genres_moods` use them.
Answers go to the shard of the same digest, and a title answered in any shard is never asked again.

**Derive** (free, every run). `franchises.json`: every franchise, its members in release order with their
era, and each title's franchise. An asked title joins the group it chose; where the groups a franchise's
titles chose are said to be one franchise, they are merged, each an era. The result must clear the golden
set (`pipeline/eval_franchises.py`) or nothing is written.

The candidates each answer was asked under are read back from the states shard that sent them, because a
letter means nothing without its list. An answer whose title's candidates have changed since is not used,
and is counted.
"""
import argparse
import collections
import dataclasses
import hashlib
import json
import os
import sys
import tempfile

from lib import wikidata_facts as wd
from lib import wikidata
from . import artifacts, audit_combined, finalize
from . import eval_franchises
from . import franchise_decisions as decisions
from . import franchise_groups as fg
from . import run_combined as rc
from .article_sections import parse_sections
from .combined_questions import PINNED_MODEL, taxonomy
from .contract import REPO, StageError

NAME = "franchises"
PRODUCER = "pipeline/franchises.py"
HOW = "./den stage franchises (add --spend to ask Jev about the titles Wikidata leaves open first)"
PUBLISHES = False
#: The ask buys from Jev: a lead and a short list per title, a few thousand titles.
SPENDS = True
#: The groups and the derive buy nothing, so `den run` runs this stage without `--spend` too.
FREE_WITHOUT_SPEND = True

INPUTS = (artifacts.FACTS, artifacts.ARTICLES, artifacts.MANIFEST)
OUTPUTS = (artifacts.FRANCHISE_STATES, artifacts.FRANCHISE_ANSWERS, artifacts.FRANCHISE_ANSWERS_MANIFEST,
           artifacts.FRANCHISE_DECISIONS, artifacts.FRANCHISES)

GOLDEN = os.path.join(REPO, "data", "franchise-golden.json")
HEADING = "Franchise candidates (from Wikidata)"
#: The `source` of a franchise: decided from Wikidata alone, or with Jev's answers.
WIKIDATA, JEV = "wikidata", "jev-franchise-v1"
#: An answer at or above this is taken; under it the title is left without a franchise.
TAKE = 0.5
#: A group flagged as a possible catalogue needs a clear verdict across its own members. Per-title
#: borderline choices otherwise turn exactly the thematic companion sets this flag exists for into a
#: franchise (the completed corpus pass exposed the Three Flavours Cornetto trilogy at 0.51--0.77).
CATALOGUE_TAKE = 0.8
#: ``separate adaptation`` is an era boundary, not a membership vote. Jev tends to call every film based
#: on a different novel a separate adaptation at about 0.5, splitting a continuing film series title by
#: title. Reserve that boundary for a clear answer.
SEPARATE_TAKE = 0.75
#: A source-only candidate plus a clear separate-adaptation answer is weak franchise evidence. Require a
#: correspondingly clear group choice; this leaves a loose relocation such as The Laughing Policeman out,
#: while a confidently recognised reboot remains a separate era.
SEPARATE_SOURCE_TAKE = 0.85

SECTION = f"the `{HEADING}` section"
EVIDENCE = ("Judge the requested work (film or series) from the supplied article lead and what is widely known "
            f"about it. {SECTION} lists candidate franchise groups from Wikidata, lettered A to D. ")


def questions():
    """(questions, label mapping). Rules from `docs/JEV-QUESTIONS.md`: `not-stated` and `other` on the
    Choice, Nouls for yes/no, every value stored and every threshold in the derive."""
    letters = {letter: f"The work is an entry of the group lettered {letter} in {SECTION}. Pick only a letter "
                       "that section lists." for letter in fg.LETTERS}
    qs = {
        "fr__group": {
            "type": "choice",
            "instructions": EVIDENCE + "Which listed group is the franchise this work is an entry of: the same "
                            "fictional world or continuing characters, told as a series, the group a viewer would "
                            "name as its series? Prefer the group named after its story over a shared universe "
                            "or a studio that holds several stories.",
            "criteria": {**letters,
                         "none": "It is an entry of none of them: one film of a studio's or director's "
                                 "catalogue, a thematic companion with no shared characters or world, or a work "
                                 "that only shares a real subject.",
                         "other": "It is an entry of a franchise the section does not list.",
                         "not-stated": "Neither the lead nor what is widely known says."},
        },
        "fr__one_franchise": {
            "type": "noul",
            "instructions": EVIDENCE + "Are the listed groups this work belongs to one franchise, the same "
                            "continuing characters or fictional world, so that the smaller or later groups are "
                            "eras of it (a recast, a reboot, a national version, a TV run)?",
        },
        "fr__separate_adaptation": {
            "type": "noul",
            "instructions": EVIDENCE + "Is this work a remake, or a separate adaptation of the same book, "
                            "character or legend, rather than a continuation of the other titles in its group?",
        },
    }
    return qs, {}


# ---------------------------------------------------------------------------------------------- groups


def read_facts(path):
    with open(path, encoding="utf-8") as fh:
        blob = json.load(fh)
    records = blob.get("records") if isinstance(blob, dict) else None
    if not isinstance(records, list) or not records:
        raise StageError(f"{path} holds no facts records. Build it with: ./den stage facts")
    return records, blob.get("entities") or {}


def _year(record):
    for field in ("released", "started"):
        value = record.get(field)
        date = value.get("date") if isinstance(value, dict) else value
        if isinstance(date, str) and date[:4].isdigit():
            return int(date[:4])
    return None


def titles_from(records, source_series, tmdb_keys):
    """`{key: fg.Title}` from the facts records, with each source's book series and each sequel link's
    corpus key resolved."""
    out = {}
    for r in records:
        key = f"{r['mediaType']}:{r['tmdbId']}"
        names = r.get("titles") or {}
        sources = sorted({s for b in r.get("basedOn") or [] for s in source_series.get(b, ())})
        follows = sorted({k for q in (r.get("follows") or []) + (r.get("followedBy") or [])
                          for k in tmdb_keys.get(q, ())} - {key})
        out[key] = fg.Title(key, names.get("en") or names.get("orig") or key, _year(r),
                            series=r.get("franchise") or [], franchises=r.get("mediaFranchise") or [],
                            sources=sources, follows=follows, characters=r.get("characters") or [],
                            people=(r.get("cast") or []) + (r.get("directors") or []))
    return out


def ask_wikidata(records, cache):
    """What the groups need that the facts do not hold: each source's book series, each sequel link's TMDB
    id, and every series' parents, walked up until no new one appears."""
    sources = sorted({b for r in records for b in r.get("basedOn") or []})
    source_series = wd.source_series(sources, cache)
    linked = sorted({q for r in records for q in (r.get("follows") or []) + (r.get("followedBy") or [])})
    tmdb_keys = wd.tmdb_keys(linked, cache)
    parents, frontier = {}, {q for r in records for q in (r.get("franchise") or []) + (r.get("mediaFranchise") or [])}
    frontier |= {s for found in source_series.values() for s in found}
    while frontier:
        found = wd.parents(sorted(frontier), cache)
        parents.update(found)
        frontier = {p for ps in found.values() for p in ps} - set(parents) - frontier
    return source_series, tmdb_keys, parents


def grouped(ctx, cache=None):
    """(titles, groups, flags, automatic, asked, names) for this out-dir's facts."""
    records, entities = read_facts(ctx.require(artifacts.FACTS))
    cache = wikidata.cache_for() if cache is None else cache
    source_series, tmdb_keys, parents = ask_wikidata(records, cache)
    names = {q: e.get("en") for q, e in entities.items() if isinstance(e, dict) and e.get("en")}
    unnamed = sorted(({s for found in source_series.values() for s in found} |
                      {p for ps in parents.values() for p in ps}) - set(names))
    if unnamed:
        names.update({q: d["name"] for q, d in wd.entity_details(unnamed).items() if d.get("name")})
    titles = titles_from(records, source_series, tmdb_keys)
    groups = fg.build(titles, names, parents)
    flagged = fg.flags(groups, titles)
    automatic, asked = fg.plan(titles, groups, flagged)
    return titles, groups, flagged, automatic, asked, names


# ------------------------------------------------------------------------------------------------ ask


def _article_records(ctx):
    try:
        records, _ = rc.load_articles(ctx.require(artifacts.ARTICLES))
    except SystemExit as exc:
        raise StageError(f"franchises: {exc}") from None
    return {rc.article_key(r): r for r in records}


def state_record(article, text, year=None):
    """The title's article with the candidates section appended: a normal article-dump row, so the pass
    reads it as one. It states its target year (the dump's, else Wikidata's) and plot headings itself, as a
    new dump does, so no enrichment directory is needed; the state is the lead and the candidates, which no
    plot heading selects."""
    rec = dict(article)
    rec.setdefault("year", year)
    rec.setdefault("plotSections", [])
    rec["text"] = article["text"].rstrip() + f"\n\n== {HEADING} ==\n{text}\n"
    if "sections" in article:
        rec["sections"] = list(article["sections"]) + [HEADING]
    return rec


def _candidate_json(ids):
    return [list(c) if isinstance(c, tuple) else c for c in ids]


def _candidate_ids(stored):
    return [(c[0], tuple(c[1])) if isinstance(c, list) else c for c in stored]


def write_states(ctx, asked, groups, titles, answered):
    """Write the states shard for the titles to ask, named by its digest. Returns (path, records, state ids,
    how many were skipped for want of an article). A title already answered in any shard is not written."""
    articles = _article_records(ctx)
    rows, states, skipped = [], {}, 0
    for key in sorted(asked):
        if key in answered:
            continue
        article = articles.get(key)
        if article is None:
            skipped += 1
            continue
        rec = state_record(article, fg.section(key, asked[key], groups, titles), titles[key].year)
        rec["franchiseCandidates"] = _candidate_json(asked[key])
        sections = parse_sections(rec["text"], rec.get("plotSections") or ())
        states[key] = [sections[0]["id"], sections[-1]["id"]]
        rows.append(rec)
    if not rows:
        return None, [], {}, skipped
    body = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for r in rows)
    digest = hashlib.sha256(body.encode()).hexdigest()[:12]
    path = ctx.shard(artifacts.FRANCHISE_STATES)[:-len(".jsonl")] + f"-{digest}.jsonl"
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path, rows, states, skipped


def answers_path(ctx, states_path):
    digest = os.path.basename(states_path)[:-len(".jsonl")].rpartition("-")[2]
    return ctx.shard(artifacts.FRANCHISE_ANSWERS)[:-len(".jsonl")] + f"-{digest}.jsonl"


def ask(ctx, asked, groups, titles):
    """Buy the answers for every asked title not yet answered. Returns how many were asked."""
    answered = set(read_answers(ctx))
    states_path, rows, states, skipped = write_states(ctx, asked, groups, titles, answered)
    if skipped:
        print(f"franchises: not asking {skipped}: no article", file=sys.stderr)
    if not rows:
        return 0
    qs, mapping = questions()
    out = answers_path(ctx, states_path)
    args = argparse.Namespace(articles=states_path, enriched_dir=None, out=out, manifest=None, prompt=rc.PROMPT,
                              taxonomy=rc.TAXONOMY, model=PINNED_MODEL, max_state_chars=rc.DEFAULT_MAX_STATE_CHARS,
                              workers=8, limit=ctx.limit)
    try:
        records, input_keys = rc.load_articles(states_path)
        evidence = rc.attach_enriched_evidence(records, None)
    except SystemExit as exc:
        raise StageError(f"franchises: {exc}") from None
    lock = None
    try:
        lock = rc.acquire_output_lock(out + ".lock")
        failed = rc.paid_run(args, qs, mapping, taxonomy(), evidence, records, input_keys, records,
                             state_ids_by_key=states)
    except SystemExit as exc:
        raise StageError(f"franchises: {exc}") from None
    finally:
        if lock is not None:
            rc.release_output_lock(lock)
    if failed:
        raise StageError(f"franchises: the ask stopped on a failed call; what was bought is in {out} and a rerun "
                         f"resumes from it")
    return min(len(rows), ctx.limit) if ctx.limit else len(rows)


def selected(ctx, asked):
    """The exact asked titles named for a pilot, or every asked title. Refuse typos before any call."""
    if not ctx.keys:
        return asked
    try:
        with open(ctx.keys, encoding="utf-8") as fh:
            keys = {token for line in fh for token in line.replace(",", " ").split()}
    except OSError as exc:
        raise StageError(f"franchises: cannot read --keys {ctx.keys}: {exc}") from None
    if not keys:
        raise StageError("franchises: --keys file is empty")
    unknown = sorted(keys - set(asked))
    if unknown:
        raise StageError(f"franchises: --keys contains {len(unknown)} title(s) that need no franchise "
                         f"judgment, e.g. {', '.join(unknown[:5])}")
    return {key: asked[key] for key in sorted(keys)}


def plan_report(ctx, asked, automatic):
    articles = _article_records(ctx)
    answered, durable = load_answers(ctx, persist=False)
    todo = [k for k in asked if k not in answered and k in articles]
    report = {"automatic": len(automatic), "asked": len(asked),
              "alreadyAnswered": len(set(asked) & set(answered)),
              "noArticle": sum(1 for k in asked if k not in articles), "ask": len(todo),
              **decisions.projection(durable, len(todo))}
    print(json.dumps(report, indent=2))
    return report


# --------------------------------------------------------------------------------------------- derive


def load_answers(ctx, persist=True):
    """`key -> (answers, candidate ids)` over every answer shard, each read beside the states shard of the
    same digest and its manifest. Every paid row is audited against that frozen state and today's exact
    franchise questions. A title answered twice is refused."""
    qs, _ = questions()
    compact_path = ctx.path(artifacts.FRANCHISE_DECISIONS)
    durable, _ = decisions.read(compact_path, qs, PINNED_MODEL)
    out = {key: decisions.validate_decision(key, decision, qs, PINNED_MODEL,
                                            os.path.basename(compact_path))
           for key, decision in durable.items()}
    durable = dict(durable)
    raw_seen = set()
    states_by_digest = {os.path.basename(p)[:-len(".jsonl")].rpartition("-")[2]: p
                        for p in ctx.paths(artifacts.FRANCHISE_STATES)}
    for path in ctx.paths(artifacts.FRANCHISE_ANSWERS):
        manifest_path = path + ".manifest.json"
        if not os.path.exists(manifest_path):
            raise StageError(f"{path} has no manifest at {manifest_path}, so nothing says what bought it")
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        digest = os.path.basename(path)[:-len(".jsonl")].rpartition("-")[2]
        states = states_by_digest.get(digest)
        if states is None:
            raise StageError(f"{path} has no states shard ending -{digest}.jsonl, so its letters mean nothing")
        try:
            records, _ = rc.load_articles(states)
            evidence_sha = rc.attach_enriched_evidence(records, None)
            audit_combined.validate_manifest(manifest, states, evidence_sha)
        except (ValueError, SystemExit) as refusal:
            raise StageError(f"franchises: {refusal}") from None
        qs, mapping = questions()
        config = manifest["config"]
        if config.get("globalQuestions") != qs or config.get("labelQuestionMapping") != mapping:
            raise StageError(f"franchises: {manifest_path} does not record today's franchise questions")
        by_key = {rc.article_key(record): record for record in records}
        asked_with = {key: _candidate_ids(record["franchiseCandidates"])
                      for key, record in by_key.items()}
        state_ids = {}
        for key, record in by_key.items():
            sections = parse_sections(record["text"], record.get("plotSections") or ())
            state_ids[key] = [sections[0]["id"], sections[-1]["id"]]
        if config.get("stateSectionIdsSha256") != rc.sha256_text(rc.canonical(state_ids)):
            raise StageError(f"franchises: {manifest_path} does not describe the states shard's sections")
        with open(path, encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                row = json.loads(line)
                key = rc.article_key(row)
                if key not in by_key:
                    raise StageError(f"{path}:{n}: {key} was not in the states shard that run bought")
                try:
                    audit_combined.validate_row(row, by_key[key], manifest, qs, state_ids[key])
                except (ValueError, rc.TypeSafeError) as refusal:
                    raise StageError(f"franchises: {path}:{n}: {refusal}") from None
                decision = decisions.from_paid_row(key, row, asked_with[key])
                if key in raw_seen:
                    raise StageError(f"{key} is answered in two shards; the second is {path}")
                if key in durable and durable[key] != decision:
                    raise StageError(f"{key} differs between {compact_path} and {path}")
                raw_seen.add(key)
                durable[key] = decision
                out[key] = (row["answers"], asked_with[key])
    if persist:
        decisions.write(compact_path, durable, qs, PINNED_MODEL)
    return out, durable


def read_answers(ctx, persist=True):
    return load_answers(ctx, persist)[0]


def resolve(titles, groups, flagged, automatic, asked, answers, names):
    """`({franchise id: {name, source, members: {key: era}}}, counts)`: the automatic franchises, then each
    answered title's choice, then the groups its titles say are one franchise merged."""
    franchise_of, era_of, source, confidence, umbrella_of = {}, {}, {}, {}, {}
    for key, (root, era) in automatic.items():
        franchise_of[key] = root
        era_of[key] = era
        source.setdefault(root, WIKIDATA)
        confidence[key] = 1.0
    counts = collections.Counter(automatic=len(automatic))
    one = collections.defaultdict(list)
    separate = set()
    catalogue_votes = collections.defaultdict(list)
    for key, listed in asked.items():
        if key not in answers:
            continue
        got, answered_with = answers[key]
        if listed != answered_with:
            continue
        for index, gid in enumerate(listed):
            if isinstance(gid, str) and flagged.get(gid) == "catalogue" and key in groups[gid].members:
                letter = fg.LETTERS[index]
                catalogue_votes[gid].append(got["fr__group"]["probabilities"][letter])
    catalogue_support = {gid: sum(votes) / len(votes) for gid, votes in catalogue_votes.items()}
    for key in sorted(asked):
        if key not in answers:
            counts["not answered"] += 1
            continue
        got, listed = answers[key]
        if listed != asked[key]:
            counts["answered under other candidates"] += 1
            continue
        choice = got["fr__group"]
        letter = choice["choice"]
        if letter not in fg.LETTERS or fg.LETTERS.index(letter) >= len(listed) or choice["confidence"] < TAKE:
            counts["no franchise"] += 1
            continue
        chosen = listed[fg.LETTERS.index(letter)]
        gid = f"characters:{min(chosen[1])}" if isinstance(chosen, tuple) else chosen
        separate_vote = got["fr__separate_adaptation"]["noul"]
        if isinstance(chosen, str) and flagged.get(gid) == "catalogue" \
                and catalogue_support.get(gid, 0.0) < CATALOGUE_TAKE:
            counts["no franchise"] += 1
            continue
        if isinstance(chosen, str) and groups[gid].kind == "book-series" \
                and separate_vote >= SEPARATE_TAKE and choice["confidence"] < SEPARATE_SOURCE_TAKE:
            counts["no franchise"] += 1
            continue
        if isinstance(chosen, tuple) and gid not in groups:
            groups[gid] = fg.Group(gid, "characters", titles[min(chosen[1], key=lambda k: fg.order_key(titles[k]))].name,
                                   chosen[1])
        franchise_of[key] = gid
        source[gid] = JEV
        confidence[key] = choice["confidence"]
        umbrellas = [other for other in listed if isinstance(other, str) and other != gid
                     and flagged.get(other) == "universe" and key in groups[other].members]
        if umbrellas:
            umbrella_of[key] = min(umbrellas, key=lambda group: (len(groups[group].members), group))
        counts["answered"] += 1
        for other in listed:
            if not isinstance(other, tuple) and other != chosen:
                one[frozenset((gid, other))].append(got["fr__one_franchise"]["noul"])
        if separate_vote >= SEPARATE_TAKE:
            separate.add(key)
    # Groups the titles listing both say are one franchise are merged, the bigger one absorbing the other.
    merged, merge_confidence = {}, {}

    def top(g):
        while g in merged:
            g = merged[g]
        return g

    for pair, votes in sorted(one.items(), key=lambda kv: sorted(kv[0])):
        agreement = sum(votes) / len(votes)
        if agreement < TAKE:
            continue
        # A shared universe is useful title metadata, never a primary-franchise merge. Otherwise a 0.5-ish
        # answer from a crossover collapses Thor, Iron Man and every other MCU story into one row.
        if any(flagged.get(gid) == "universe" for gid in pair):
            continue
        a, b = sorted((top(g) for g in pair), key=lambda g: (-len(groups[g].members), g))
        if a != b:
            merged[b] = a
            merge_confidence[b] = agreement

    def root_and_confidence(gid):
        held = 1.0
        while gid in merged:
            held = min(held, merge_confidence[gid])
            gid = merged[gid]
        return gid, held

    out = {}
    for key, gid in franchise_of.items():
        root, merge_conf = root_and_confidence(gid)
        if key in separate:
            era = f"adaptation:{key}"
        elif gid != root:
            era = gid
        else:
            era = era_of.get(key) or fg.era(key, groups[root], groups)
            if era is None and titles[key].media == "tv" \
                    and any(titles[member].media == "movie" for member in groups[root].members):
                era = f"adaptation:{key}"
        entry = out.setdefault(root, {"name": names.get(root) or groups[root].name, "source": WIKIDATA,
                                      "confidence": 1.0, "umbrellaOf": {}, "members": {}})
        if source.get(gid) == JEV:
            entry["source"] = JEV
        entry["confidence"] = min(entry["confidence"], confidence.get(key, 1.0), merge_conf)
        if key in umbrella_of and umbrella_of[key] != root:
            entry["umbrellaOf"][key] = umbrella_of[key]
        entry["members"][key] = era
    return out, counts


def era_name(era, groups, titles, names):
    if era is None:
        return None
    if era.startswith("adaptation:"):
        t = titles[era.partition(":")[2]]
        return f"{t.name} ({t.year})" if t.year else t.name
    return names.get(era) or (groups[era].name if era in groups else era)


def document(franchises, groups, titles, names):
    """`franchises.json`'s `franchises` and `titles`: stable eras and release-ordered mixed members."""
    out, of = {}, {}
    for fid in sorted(franchises):
        entry = franchises[fid]
        members = sorted(entry["members"], key=lambda k: fg.order_key(titles[k]))
        if len(members) < 2:
            continue
        positions = {key: order for order, key in enumerate(members)}
        by_era = collections.defaultdict(list)
        for key in members:
            by_era[entry["members"][key]].append(key)
        era_tokens = sorted(by_era, key=lambda era: (positions[by_era[era][0]], str(era or "")))
        era_ids = {era: f"{fid}:era:{era or 'main'}" for era in era_tokens}
        eras = [{"id": era_ids[era], "name": era_name(era, groups, titles, names) or entry["name"],
                 "order": order,
                 "members": by_era[era]}
                for order, era in enumerate(era_tokens)]
        # The wire stores exact hundredths. Never round a confidence upward across an acceptance boundary.
        confidence = int(entry["confidence"] * 100 + 1e-9) / 100
        franchise = {"id": fid, "name": entry["name"], "confidence": confidence,
                     "source": entry["source"], "eras": eras,
                    "members": [{"key": k, "year": titles[k].year,
                                 "eraId": era_ids[entry["members"][k]], "order": positions[k]}
                                for k in members]}
        out[fid] = franchise
        for k in members:
            of[k] = {"primary": fid}
            if uid := entry["umbrellaOf"].get(k):
                of[k]["umbrella"] = {"id": uid, "name": names.get(uid) or groups[uid].name}
    return out, of


def derive(ctx, titles, groups, automatic, asked, names, flagged):
    answers = read_answers(ctx)
    franchises, counts = resolve(titles, groups, flagged, automatic, asked, answers, names)
    doc, of = document(franchises, groups, titles, names)
    with open(GOLDEN, encoding="utf-8") as fh:
        golden = json.load(fh)
    result = eval_franchises.evaluate({"franchises": doc, "titles": of}, golden, set(titles))
    failures = eval_franchises.below_floors(result, golden["floors"])
    if failures:
        raise StageError("franchises: below the golden set's floors, so nothing was written:\n  " +
                         "\n  ".join(failures) + f"\n{json.dumps(result, indent=2)}")
    with open(GOLDEN, "rb") as fh:
        golden_sha = hashlib.sha256(fh.read()).hexdigest()
    blob = {"schema": 2, "datasetVersion": ctx.dataset_version, "franchises": doc, "titles": of,
            "derivation": {"golden": os.path.relpath(GOLDEN, REPO), "goldenSha256": golden_sha,
                           "eval": result, "counts": dict(counts), "model": PINNED_MODEL}}
    path = ctx.path(artifacts.FRANCHISES)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd, candidate = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".tmp-franchises-",
                                     suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(blob, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(candidate, path)
    return counts, len(doc)


def run(ctx, cache=None):
    """`--plan` prints what would be asked and writes nothing; `--spend` asks, then derives; otherwise derive."""
    ctx.require(artifacts.MANIFEST)
    try:
        ctx = dataclasses.replace(ctx, dataset_version=finalize.manifest_version(ctx))
    except StageError as refusal:
        raise StageError(f"franchises: {refusal}") from None
    titles, groups, flagged, automatic, asked, names = grouped(ctx, cache)
    pilot = selected(ctx, asked)
    if ctx.plan:
        report = plan_report(ctx, pilot, automatic)
        return f"planned only — {report['ask']:,} titles to ask; nothing written"
    bought = ask(ctx, pilot, groups, titles) if ctx.spend else 0
    if ctx.spend and ctx.keys:
        _answers, durable = load_answers(ctx)
        usage = decisions.totals(durable)
        return (f"targeted pilot — asked {bought:,}; {usage['calls']:,} durable decisions, "
                f"{usage['inputTokens']:,} exact input tokens, ${usage['costUSD']:.8f}; "
                "franchises.json not rebuilt from a partial pilot")
    counts, written = derive(ctx, titles, groups, automatic, asked, names, flagged)
    return (f"{ctx.path(artifacts.FRANCHISES)} — {written:,} franchises; asked {bought:,}; " +
            ", ".join(f"{name} {n:,}" for name, n in sorted(counts.items()) if n))
