#!/usr/bin/env python3
"""Precompute the More Like This cascade for every title in the store (#132).

The pilot (`more_like_cascade.py`) passed its preregistered rule on 573 anchors. This runs the same two steps
for every anchor, with the pilot's own functions, so an anchor whose state is byte-identical to the pilot's
reuses the pilot's answer instead of paying for it again:

1. **Screen** — one title/year call per anchor over the first 100 titles of atlas's live row; titles released
   in or after 2025 are not screened, and such an anchor is not screened at all.
2. **Evidence** — atlas's top ten plus up to five screen promotions, one lead + story call per anchor, the
   overall Noul per finalist.

The output is each anchor's finalists with their overall Noul (`export`), which the store carries as a sparse
section. An anchor with no evidence, a failed call, or fewer than two finalists gets no scores, which leaves
atlas's row as it is.

Spend is reserved in chunks rather than up front: an anchor is admitted to a chunk only while the recorded
spend plus every admitted anchor's screen ceiling and worst-case evidence ceiling stays under the cap, so a run
stops cleanly between chunks with every paid screen followed by its evidence call. Anchors run most popular
first (by their type's popularity order), so a run that reaches the cap leaves the long tail unscored rather
than an arbitrary slice. Every answer is appended as it arrives; rerunning resumes.
"""
import argparse
import concurrent.futures
import datetime
import gzip
import hashlib
import json
import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import more_like_cascade as pilot  # noqa: E402
from more_like_cascade import (CUTOFF_YEAR, MAX_PROMOTIONS, POOL_K, TOP, blinded, ceiling,  # noqa: E402
                               evidence_questions, evidence_state, finalists_for, kind, screen_questions)
from more_like_gate import MODEL, TypeSafe, canonical, digest, file_digest, read_jsonl  # noqa: E402
from lib.typesafe_client import api_key  # noqa: E402

SCHEMA = "jev-more-like-cascade-full-v1"
EXPORT_SCHEMA = "jev-more-like-v1"
CHUNK = 500


def _read_gz_json(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def _write_gz_json(path, value):
    with gzip.open(path + ".tmp", "wt", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    os.replace(path + ".tmp", path)


def corpus_keys(corpus_path):
    with gzip.open(corpus_path, "rt", encoding="utf-8") as fh:
        return [json.loads(line)["key"] for line in fh if line.strip()]


# --- fetch ---------------------------------------------------------------------------------------------

def fetch(atlas, expect_version, corpus_path, out, workers=8):
    """Atlas's live mixed row (first 100) for every corpus title, and each type's popularity order."""
    meta = pilot._get(atlas, "/dataset.json")
    if meta.get("datasetVersion") != expect_version:
        raise SystemExit(f"atlas serves {meta.get('datasetVersion')}, not {expect_version}")
    anchors = corpus_keys(corpus_path)
    if meta.get("count") != len(anchors):
        raise SystemExit(f"atlas holds {meta.get('count')} titles, the corpus {len(anchors)}")
    popularity = pilot.popularity_order(atlas)

    def row(key):
        answer = pilot._get(atlas, f"/index/similar/{pilot.atlas_path(key)}.json?limit={POOL_K}")
        return key, [pilot.dataset_key(item["type"], item["id"]) for item in answer["mixed"]][:POOL_K]

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        rows = dict(pool.map(row, anchors))
    unnamed = sorted({key for anchor, keys in rows.items() for key in [anchor, *keys]} - set(popularity))

    def named(key):
        card = pilot._get(atlas, f"/index/title/{pilot.atlas_path(key)}.json")
        return key, {"rank": None, "typeSize": None, "title": card.get("title"), "year": card.get("year")}

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        popularity.update(pool.map(named, unnamed))
    if pilot._get(atlas, "/dataset.json").get("datasetVersion") != expect_version:
        raise SystemExit("atlas changed dataset while the rows were fetched")
    _write_gz_json(out, {"datasetVersion": expect_version, "storeSha256": meta.get("storeSha256"),
                         "fetchedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                         "rowLimit": POOL_K, "popularity": popularity, "rows": rows})
    return {"out": out, "sha256": file_digest(out), "anchors": len(rows), "titles": len(popularity),
            "emptyRows": sum(not r for r in rows.values()), "shortRows": sum(len(r) < POOL_K for r in rows.values())}


# --- prepare --------------------------------------------------------------------------------------------

def prepare(pool_path, articles_path, enriched_dir, work, cap):
    """Freeze every title's identity and article evidence, exactly as the pilot built them."""
    pool = _read_gz_json(pool_path)
    wanted = {key for anchor, row in pool["rows"].items() for key in [anchor, *row]}
    evidence = pilot.load_evidence(articles_path, enriched_dir, wanted)
    identity = {}
    for key in wanted:
        named = pool["popularity"].get(key) or {}
        title = named.get("title") or evidence[key].get("title")
        year = named.get("year") or evidence[key].get("year")
        if title:
            identity[key] = {"title": title, "year": year, "type": kind(key)}
    os.makedirs(work, exist_ok=True)
    files = {"evidence.json.gz": evidence, "identity.json.gz": identity}
    manifest = {
        "schema": SCHEMA, "issue": "oxyc/den-dataset#132", "model": MODEL,
        "datasetVersion": pool["datasetVersion"], "storeSha256": pool["storeSha256"],
        "inputs": {"pool": file_digest(pool_path), "articles": file_digest(articles_path)},
        "files": {name: digest(value) for name, value in files.items()},
        "parameters": {"poolK": POOL_K, "top": TOP, "maxPromotions": MAX_PROMOTIONS,
                       "recognised": pilot.RECOGNISED, "margin": pilot.MARGIN, "cutoffYear": CUTOFF_YEAR,
                       "evidenceBudgetChars": pilot.EVIDENCE_BUDGET_CHARS},
        "questionTemplatesSha256": digest([screen_questions(["cX"]), evidence_questions(["cX"])]),
        "spendCapUSD": cap,
    }
    path = os.path.join(work, "manifest.json")
    if os.path.exists(path):
        if pilot._read_json(path) != manifest:
            raise ValueError(f"{path}: refusing to replace a different manifest")
        return manifest
    for name, value in files.items():
        _write_gz_json(os.path.join(work, name), value)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return {**manifest, "anchors": len(pool["rows"]),
            "withEvidence": sum("evidence" in v for v in evidence.values()), "titles": len(wanted)}


class Frozen:
    """The manifest's inputs, each checked against the hash it was frozen with."""

    def __init__(self, work, pool_path):
        self.work = work
        self.manifest = pilot._read_json(os.path.join(work, "manifest.json"))
        if self.manifest.get("schema") != SCHEMA or self.manifest.get("model") != MODEL:
            raise ValueError(f"{work}: incompatible manifest")
        if digest([screen_questions(["cX"]), evidence_questions(["cX"])]) \
                != self.manifest["questionTemplatesSha256"]:
            raise ValueError("question templates changed after the manifest was frozen")
        if file_digest(pool_path) != self.manifest["inputs"]["pool"]:
            raise ValueError(f"{pool_path}: not the frozen pool")
        self.pool = _read_gz_json(pool_path)
        for name in ("evidence.json.gz", "identity.json.gz"):
            value = _read_gz_json(os.path.join(work, name))
            if digest(value) != self.manifest["files"][name]:
                raise ValueError(f"{name}: changed after the manifest was frozen")
            setattr(self, name.split(".")[0], value)

    def has_evidence(self, key):
        return "evidence" in self.evidence.get(key, {}) and key in self.identity

    def screen_row(self, anchor):
        """The screen call for an anchor, None when it is not screened, or the reason it gets no scores."""
        if not self.has_evidence(anchor):
            return "anchor has no evidence"
        if (self.identity[anchor]["year"] or 0) >= CUTOFF_YEAR:
            return None
        row = self.pool["rows"][anchor]
        screened = [k for k in row if k in self.identity and (self.identity[k]["year"] or 0) < CUTOFF_YEAR]
        labels = blinded(anchor, screened, 3)
        state = {"anchor": self.identity[anchor],
                 "candidates": {label: self.identity[key] for label, key in labels.items()}}
        qs = screen_questions(labels)
        return {"anchor": anchor, "labels": labels, "state": state, "stateSha256": digest(state),
                "questionsSha256": digest(qs), "ceiling": ceiling(state, qs)}

    def evidence_row(self, anchor, screen_scores):
        entry = {"row": self.pool["rows"][anchor]}
        finalists, fixed, promoted = finalists_for(entry, screen_scores, self.has_evidence)
        if len(finalists) < 2:
            return "fewer than two finalists"
        state, labels = evidence_state(anchor, finalists, self.identity, self.evidence)
        qs = evidence_questions(labels)
        return {"anchor": anchor, "labels": labels, "promoted": promoted, "state": state,
                "stateSha256": digest(state), "questionsSha256": digest(qs), "ceiling": ceiling(state, qs)}

    def worst_evidence_ceiling(self, anchor):
        """The largest evidence call an anchor can make: its first fifteen as finalists, as the pilot priced."""
        finalists = [k for k in self.pool["rows"][anchor][:TOP + MAX_PROMOTIONS] if self.has_evidence(k)]
        if len(finalists) < 2:
            return 0
        state, labels = evidence_state(anchor, finalists, self.identity, self.evidence)
        return ceiling(state, evidence_questions(labels))

    def order(self):
        """Most popular first within its own type (share of the type), then unranked, ties by key."""
        def rank(key):
            item = self.pool["popularity"].get(key) or {}
            return (item["rank"] / item["typeSize"] if item.get("rank") else 2.0, key)
        return sorted(self.pool["rows"], key=rank)


# --- answers --------------------------------------------------------------------------------------------

PHASES = ("screen", "evidence")


def _answers_path(work, phase):
    return os.path.join(work, f"{phase}-answers.jsonl")


def recorded(work, phase):
    """Recorded answers of a phase by anchor. Their provenance is checked against a rebuilt row at use."""
    done = {}
    path = _answers_path(work, phase)
    if os.path.exists(path):
        for answer in read_jsonl(path):
            if answer["anchor"] in done:
                raise ValueError(f"{path}: two answers for {answer['anchor']}")
            done[answer["anchor"]] = answer
    return done


def new_tokens(work):
    """Input tokens this run paid for: reused pilot answers cost nothing new."""
    return sum(a["usage"]["input_tokens"] for phase in PHASES for a in recorded(work, phase).values()
               if not a.get("reused"))


def pilot_answers(pilot_work):
    """The pilot's answers by (phase, anchor), for reuse where the state hash matches."""
    out = {}
    for phase in PHASES:
        path = os.path.join(pilot_work, f"{phase}-answers.jsonl")
        for answer in read_jsonl(path) if os.path.exists(path) else ():
            out[(phase, answer["anchor"])] = answer
    return out


def check(answer, row):
    if answer.get("stateSha256") != row["stateSha256"] or answer.get("questionsSha256") != row["questionsSha256"] \
            or answer.get("model") != MODEL or set(answer.get("scores", {})) != set(row["labels"]):
        raise ValueError(f"answer provenance mismatch for {row['anchor']}")
    return answer


class Caller:
    def __init__(self, work, client, workers):
        self.work, self.client, self.workers = work, client, workers
        self.lock = threading.Lock()

    def ask(self, phase, rows, make_questions):
        """One call per row; answers appended as they arrive, failures to `<phase>-errors.jsonl`."""
        errors = []
        with open(_answers_path(self.work, phase), "a", encoding="utf-8") as fh:
            def one(row):
                qs = make_questions(row["labels"])
                try:
                    got, metadata = self.client.ask_with_metadata(row["state"], qs)
                    pilot.combined.validate_answers(got, qs)
                    usage = metadata.get("usage") or {}
                    if metadata.get("model") != MODEL or not isinstance(usage.get("input_tokens"), int) \
                            or usage["input_tokens"] > row["ceiling"]:
                        raise ValueError(f"model {metadata.get('model')!r} or usage outside the ceiling")
                except Exception as exc:  # recorded, not swallowed: the anchor gets no scores
                    with self.lock:
                        errors.append({"anchor": row["anchor"], "error": f"{type(exc).__name__}: {exc}"[:300]})
                    return None
                answer = {"anchor": row["anchor"], "stateSha256": row["stateSha256"],
                          "questionsSha256": row["questionsSha256"], "model": MODEL,
                          "scores": {label: got[label]["noul"] for label in qs}, "usage": usage}
                if phase == "evidence":
                    answer["keys"] = row["labels"]
                with self.lock:
                    fh.write(canonical(answer) + "\n")
                    fh.flush()
                return answer
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as pool:
                got = [a for a in pool.map(one, rows) if a]
        if errors:
            with open(os.path.join(self.work, f"{phase}-errors.jsonl"), "a", encoding="utf-8") as out:
                out.writelines(canonical(e) + "\n" for e in errors)
        return got, errors


def _reuse(work, phase, row, pilots):
    """Append the pilot's answer for a byte-identical state, marked reused. None when there is none."""
    prior = pilots.get((phase, row["anchor"]))
    if prior is None or prior.get("stateSha256") != row["stateSha256"] \
            or prior.get("questionsSha256") != row["questionsSha256"]:
        return None
    answer = {**check(prior, row), "reused": "pilot"}
    if phase == "evidence":
        answer["keys"] = row["labels"]
    with open(_answers_path(work, phase), "a", encoding="utf-8") as fh:
        fh.write(canonical(answer) + "\n")
    return answer


def run(work, pool_path, pilot_work, spend=False, workers=12, chunk=CHUNK, limit=None, client=None,
        log=sys.stderr):
    frozen = Frozen(work, pool_path)
    cap = frozen.manifest["spendCapUSD"]
    rate = TypeSafe.RATE_PER_INPUT_TOKEN
    pilots = pilot_answers(pilot_work)
    screened, weighed = recorded(work, "screen"), recorded(work, "evidence")
    outcomes = {}
    order = frozen.order()[:limit]
    caller = Caller(work, client or (TypeSafe(key=api_key(), model=MODEL) if spend else None), workers)
    stopped = None
    at, ceilings = 0, 0
    while at < len(order):
        spent = new_tokens(work) * rate
        # Admit anchors while every admitted anchor's ceilings still fit under the cap. A dry run admits
        # everything and reports the ceilings' total.
        batch, reserved = [], 0
        while at < len(order) and len(batch) < chunk:
            anchor = order[at]
            if anchor in weighed:
                at += 1
                continue
            screen = frozen.screen_row(anchor)
            if isinstance(screen, str):
                outcomes[anchor] = screen
                at += 1
                continue
            need = (0 if screen is None or anchor in screened else screen["ceiling"]) \
                + frozen.worst_evidence_ceiling(anchor)
            if spend and spent + (reserved + need) * rate > cap:
                break
            reserved += need
            batch.append((anchor, screen))
            at += 1
        ceilings += reserved
        if not batch:
            if at < len(order):
                stopped = f"cap: ${spent:.4f} recorded, the next anchor's ceiling does not fit under ${cap:.2f}"
            break
        if not spend:
            continue
        todo = []
        for anchor, screen in batch:
            if screen is None or anchor in screened:
                if screen is not None:
                    check(screened[anchor], screen)
                continue
            reused = _reuse(work, "screen", screen, pilots)
            if reused:
                screened[anchor] = reused
            else:
                todo.append(screen)
        for answer in caller.ask("screen", todo, screen_questions)[0]:
            screened[answer["anchor"]] = answer
        todo = []
        for anchor, screen in batch:
            scores = None
            if screen is not None:
                if anchor not in screened:
                    outcomes[anchor] = "screen failed"
                    continue
                scores = {key: screened[anchor]["scores"][label] for label, key in screen["labels"].items()}
            row = frozen.evidence_row(anchor, scores)
            if isinstance(row, str):
                outcomes[anchor] = row
                continue
            reused = _reuse(work, "evidence", row, pilots)
            if reused:
                weighed[anchor] = reused
            else:
                todo.append(row)
        answered, errors = caller.ask("evidence", todo, evidence_questions)
        for answer in answered:
            weighed[answer["anchor"]] = answer
        for error in errors:
            outcomes[error["anchor"]] = "evidence failed"
        print(json.dumps({"done": at, "of": len(order), "weighed": len(weighed),
                          "spentUSD": round(new_tokens(work) * rate, 6)}), file=log, flush=True)
    summary = {"anchors": len(order), "weighed": len(weighed), "screened": len(screened),
               "reused": {p: sum(1 for a in recorded(work, p).values() if a.get("reused")) for p in PHASES},
               "newInputTokens": new_tokens(work), "spentUSD": new_tokens(work) * rate, "capUSD": cap,
               "noScores": {o: sum(1 for v in outcomes.values() if v == o) for o in sorted(set(outcomes.values()))},
               "stopped": stopped, "dryRun": not spend}
    if not spend:
        summary["ceilingUSD"] = ceilings * rate
        return summary
    with open(os.path.join(work, "run-summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return summary


# --- export ---------------------------------------------------------------------------------------------

def export(work, pool_path, out):
    """Each weighed anchor's finalists, in atlas's order, with their overall Noul: the store's input."""
    frozen = Frozen(work, pool_path)
    anchors = {}
    for anchor, answer in sorted(recorded(work, "evidence").items()):
        rank = {key: i for i, key in enumerate(frozen.pool["rows"][anchor])}
        keys = answer["keys"]
        if set(keys) != set(answer["scores"]) or any(key not in rank for key in keys.values()):
            raise ValueError(f"{anchor}: answer keys do not match its row")
        anchors[anchor] = [[key, answer["scores"][label]]
                           for label, key in sorted(keys.items(), key=lambda item: rank[item[1]])]
    value = {"schema": EXPORT_SCHEMA, "issue": "oxyc/den-dataset#132", "model": MODEL,
             "datasetVersion": frozen.manifest["datasetVersion"],
             "manifestSha256": file_digest(os.path.join(work, "manifest.json")),
             "evidenceAnswersSha256": file_digest(_answers_path(work, "evidence")),
             "count": len(anchors), "anchors": anchors}
    with open(out + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    os.replace(out + ".tmp", out)
    return {"out": out, "sha256": file_digest(out), "anchors": len(anchors),
            "pairs": sum(len(v) for v in anchors.values())}


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    get = sub.add_parser("fetch")
    get.add_argument("--atlas", required=True, help="atlas base URL; never written into an artifact")
    get.add_argument("--expect-version", required=True)
    get.add_argument("--corpus", required=True)
    get.add_argument("--out", required=True)
    prep = sub.add_parser("prepare")
    for name in ("pool", "articles", "enriched-dir", "work"):
        prep.add_argument(f"--{name}", required=True)
    prep.add_argument("--max-spend-usd", type=float, required=True)
    go = sub.add_parser("run")
    for name in ("work", "pool", "pilot-work"):
        go.add_argument(f"--{name}", required=True)
    go.add_argument("--spend", action="store_true")
    go.add_argument("--workers", type=int, default=12)
    go.add_argument("--chunk", type=int, default=CHUNK)
    go.add_argument("--limit", type=int, help="only the first N anchors in run order")
    ex = sub.add_parser("export")
    for name in ("work", "pool", "out"):
        ex.add_argument(f"--{name}", required=True)
    args = parser.parse_args(argv)
    if args.command == "fetch":
        result = fetch(args.atlas, args.expect_version, args.corpus, args.out)
    elif args.command == "prepare":
        result = prepare(args.pool, args.articles, args.enriched_dir, args.work, args.max_spend_usd)
    elif args.command == "run":
        result = run(args.work, args.pool, args.pilot_work, args.spend, args.workers, args.chunk, args.limit)
    else:
        result = export(args.work, args.pool, args.out)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
