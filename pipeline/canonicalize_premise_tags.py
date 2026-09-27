#!/usr/bin/env python3
"""Resumable CLI for premise-tag canonicalisation; run ``--help`` for its phases."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
import sys
import threading

if not __package__:
    sys.path[0] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from lib import denembed  # noqa: E402
from lib.typesafe_client import MODEL, TypeSafe, TypeSafeError  # noqa: E402
from pipeline import premise_concepts as pc  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_TAGS = os.path.join(REPO, "data", "premise-tags-v2.json")
DEFAULT_CANARY = os.path.join(REPO, "data", "embed-canary.json")


def jsonl(path):
    with open(path, encoding="utf-8") as fh:
        for number, line in enumerate(fh, 1):
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SystemExit(f"{path}:{number}: {exc}") from None


def prepare(args):
    url = args.url or denembed.base_url()
    stamp = denembed.verify(args.canary, url, lambda message: print(message, file=sys.stderr))
    health = denembed.identity(url)
    forms, completed = pc.initialise_vectors(args.tags, args.work, health["dims"], stamp["spaceId"], args.min_count)
    vectors = os.path.join(args.work, "vectors.i8")
    metadata = os.path.join(args.work, "vectors.meta.json")
    for start in range(completed, len(forms), args.batch):
        block = forms[start:start + args.batch]
        got = denembed.embed_many(url, [item["form"].replace("-", " ") for item in block])
        pc.append_vectors(vectors, got, health["dims"])
        pc.write_json(metadata, {"schema": pc.VECTOR_SCHEMA, "sourceSha256": pc.source_digest(args.tags),
                                 "minimumCount": args.min_count, "dims": health["dims"], "count": len(forms),
                                 "embeddingSpace": stamp["spaceId"], "completed": start + len(block)})
        if (start + len(block)) % 5000 < args.batch:
            print(f"  embedded {start + len(block):,}/{len(forms):,}", file=sys.stderr)
    return 0


def candidates(args):
    with open(os.path.join(args.work, "forms.json"), encoding="utf-8") as fh:
        forms = json.load(fh)
    with open(os.path.join(args.work, "vectors.meta.json"), encoding="utf-8") as fh:
        meta = json.load(fh)
    rows = pc.candidate_rows(forms, os.path.join(args.work, "vectors.i8"), meta["dims"],
                             args.min_count, args.source_min_count, args.options, args.threshold,
                             args.tables, args.bits, args.probe)
    count = 0
    with open(args.out, "w", encoding="utf-8") as out:
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            count += 1
    print(json.dumps({"surfaceForms": len(forms), "requiringDecision": count, "out": args.out}))
    return 0


def adjudicate(args):
    if args.questions_per_call < 1 or args.workers < 1:
        raise SystemExit("--questions-per-call and --workers must be positive")
    rows = list(jsonl(args.candidates))
    by_form = {row["form"]: row for row in rows}
    if len(by_form) != len(rows):
        raise SystemExit(f"{args.candidates}: duplicate form")
    done = {}
    if os.path.exists(args.out):
        for row in jsonl(args.out):
            if row.get("form") in done:
                raise SystemExit(f"{args.out}: duplicate form {row.get('form')!r}")
            candidate = by_form.get(row.get("form"))
            if candidate is None:
                raise SystemExit(f"{args.out}: {row.get('form')!r} is absent from {args.candidates}")
            if row.get("candidatesSha256") != pc.candidates_digest(candidate["candidates"]):
                raise SystemExit(f"{args.out}: {row['form']!r} was answered against different candidates")
            if row.get("requestedModel") != args.model:
                raise SystemExit(f"{args.out}: {row['form']!r} was answered with another requested model")
            done[row["form"]] = row
    todo = [row for row in rows if row["form"] not in done]
    if not args.spend:
        calls = (len(todo) + args.questions_per_call - 1) // args.questions_per_call
        print(json.dumps({"candidateRows": len(rows), "alreadyAnswered": len(done), "callsPlanned": calls,
                          "questionsPlanned": len(todo),
                          "note": "pass --spend to call Jev"}))
        return 0
    client = TypeSafe(model=args.model)
    lock = threading.Lock()
    output = open(args.out, "a", encoding="utf-8")
    failed = written = 0

    def work(block):
        nonlocal failed, written
        questions = {}
        for index, row in enumerate(block):
            question = pc.choice_question(row["candidates"])["canonical"]
            question["instructions"] = (f"Surface form: {row['form'].replace('-', ' ')}. "
                                         + question["instructions"])
            questions[f"canonical_{index}"] = question
        try:
            answers, metadata = client.ask_with_metadata(
                "Canonicalise each named free-form premise tag independently.", questions)
        except TypeSafeError as exc:
            with lock:
                failed += len(block)
                print(f"FAILED {block[0]['form']} … {len(block)} rows: {exc}", file=sys.stderr)
            return
        results = []
        for index, row in enumerate(block):
            try:
                answer = answers.get(f"canonical_{index}")
                concept = pc.validate_choice(answer, row["candidates"])
            except ValueError as exc:
                with lock:
                    failed += 1
                    print(f"FAILED {row['form']}: {exc}", file=sys.stderr)
                continue
            results.append({"form": row["form"], "concept": concept or row["form"],
                            "choice": answer["choice"], "model": metadata.get("model"),
                            "requestedModel": args.model,
                            "candidatesSha256": pc.candidates_digest(row["candidates"])})
        with lock:
            before = written
            for result in results:
                output.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
            output.flush()
            written += len(results)
            if written // 1000 != before // 1000:
                print(f"  {written:,}/{len(todo):,} · {client.summary()}", file=sys.stderr)

    try:
        blocks = [todo[start:start + args.questions_per_call]
                  for start in range(0, len(todo), args.questions_per_call)]
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(work, blocks))
    finally:
        output.close()
    print(json.dumps({"written": written, "failed": failed, "calls": client.calls,
                      "inputTokens": client.input_tokens, "spendUSD": round(client.spend, 4)}))
    return 1 if failed else 0


def verify(args):
    if args.questions_per_call < 1 or args.workers < 1:
        raise SystemExit("--questions-per-call and --workers must be positive")
    selected = [row for row in jsonl(args.answers) if row.get("concept") != row.get("form")]
    by_form = {row["form"]: row for row in selected}
    if len(by_form) != len(selected):
        raise SystemExit(f"{args.answers}: duplicate selected form")
    done = {}
    if os.path.exists(args.out):
        for row in jsonl(args.out):
            source = by_form.get(row.get("form"))
            if source is None or row.get("concept") != source.get("concept"):
                raise SystemExit(f"{args.out}: verification names another decision")
            if row.get("answerSha256") != pc.record_digest(source):
                raise SystemExit(f"{args.out}: {row['form']!r} verifies another answer record")
            if row.get("requestedModel") != args.model:
                raise SystemExit(f"{args.out}: {row['form']!r} was verified with another requested model")
            done[row["form"]] = row
    todo = [row for row in selected if row["form"] not in done]
    if not args.spend:
        calls = (len(todo) + args.questions_per_call - 1) // args.questions_per_call
        print(json.dumps({"selected": len(selected), "alreadyVerified": len(done), "callsPlanned": calls,
                          "questionsPlanned": len(todo), "note": "pass --spend to call Jev"}))
        return 0
    client = TypeSafe(model=args.model)
    lock = threading.Lock()
    output = open(args.out, "a", encoding="utf-8")
    failed = written = 0

    def work(block):
        nonlocal failed, written
        questions = {}
        for index, row in enumerate(block):
            question = pc.verification_question()["relation"]
            question["instructions"] = (f"Surface form: {row['form'].replace('-', ' ')}. "
                                         f"Proposed concept: {row['concept'].replace('-', ' ')}. "
                                         + question["instructions"])
            questions[f"relation_{index}"] = question
        try:
            answers, metadata = client.ask_with_metadata(
                "Verify each proposed premise canonicalisation independently.", questions)
        except TypeSafeError as exc:
            with lock:
                failed += len(block)
                print(f"FAILED {block[0]['form']} … {len(block)} rows: {exc}", file=sys.stderr)
            return
        results = []
        for index, row in enumerate(block):
            try:
                relation = pc.validate_verification(answers.get(f"relation_{index}"))
            except ValueError as exc:
                with lock:
                    failed += 1
                    print(f"FAILED {row['form']}: {exc}", file=sys.stderr)
                continue
            results.append({"form": row["form"], "concept": row["concept"], "relation": relation,
                            "model": metadata.get("model"), "requestedModel": args.model,
                            "answerSha256": pc.record_digest(row)})
        with lock:
            for result in results:
                output.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
            output.flush()
            written += len(results)

    try:
        blocks = [todo[start:start + args.questions_per_call]
                  for start in range(0, len(todo), args.questions_per_call)]
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(work, blocks))
    finally:
        output.close()
    print(json.dumps({"written": written, "failed": failed, "calls": client.calls,
                      "inputTokens": client.input_tokens, "spendUSD": round(client.spend, 4)}))
    return 1 if failed else 0


def materialize(args):
    forms = pc.surface_forms(args.tags)
    candidates = list(jsonl(args.candidates))
    answers = list(jsonl(args.answers))
    verification = list(jsonl(args.verification))
    needed = {row["form"] for row in candidates}
    answered = {row["form"] for row in answers}
    if len(needed) != len(candidates) or len(answered) != len(answers):
        raise SystemExit("candidate or answer file repeats a form")
    if needed != answered:
        raise SystemExit(f"answers differ from candidates: missing={len(needed - answered)} "
                         f"extra={len(answered - needed)}")
    models = {row.get("model") for row in answers}
    if None in models or len(models) > 1:
        raise SystemExit(f"answers do not name one returned model: {sorted(str(m) for m in models)}")
    by_form = {row["form"]: row for row in candidates}
    for row in answers:
        candidates_for_form = by_form[row["form"]]["candidates"]
        if row.get("candidatesSha256") != pc.candidates_digest(candidates_for_form):
            raise SystemExit(f"{row['form']!r} was answered against different candidates")
        try:
            concept = pc.validate_choice({"choice": row.get("choice")}, candidates_for_form)
        except ValueError as invalid:
            raise SystemExit(f"{row['form']!r}: {invalid}") from None
        if row.get("concept") != (concept or row["form"]):
            raise SystemExit(f"{row['form']!r}: stored concept does not match its Choice")
    selected = {row["form"]: row for row in answers if row["concept"] != row["form"]}
    verified = {row["form"]: row for row in verification}
    if set(selected) != set(verified) or len(verified) != len(verification):
        raise SystemExit("verification must cover every selected decision exactly once")
    verification_models = {row.get("model") for row in verification}
    if None in verification_models or len(verification_models) > 1:
        raise SystemExit("verification rows do not name one returned model")
    for form, row in verified.items():
        if row.get("concept") != selected[form]["concept"]:
            raise SystemExit(f"{form!r}: verification names another concept")
        if row.get("answerSha256") != pc.record_digest(selected[form]):
            raise SystemExit(f"{form!r}: verification describes another answer record")
        try:
            pc.validate_verification({"choice": row.get("relation")})
        except ValueError as invalid:
            raise SystemExit(f"{form!r}: {invalid}") from None
    decisions = {form: row["concept"] for form, row in selected.items()
                 if verified[form]["relation"] == "exact"}
    artifact = pc.materialise(forms, decisions, pc.source_digest(args.tags), next(iter(models), args.model))
    artifact["conceptTitles"] = pc.concept_title_counts(args.tags, artifact["map"])
    artifact["canonicalization"] = {
        "candidateThreshold": args.threshold,
        "candidateRows": len(candidates),
        "selectedByCandidateChoice": len(selected),
        "verifiedExact": len(decisions),
        "candidateModel": next(iter(models), args.model),
        "verificationModel": next(iter(verification_models), args.model),
        "candidatesSha256": pc.source_digest(args.candidates),
        "answersSha256": pc.source_digest(args.answers),
        "verificationSha256": pc.source_digest(args.verification),
    }
    pc.write_json(args.out, artifact)
    print(json.dumps({key: artifact[key] for key in ("surfaceForms", "concepts", "model")}))
    return 0


def parser():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="embed every distinct surface form, resumably")
    p.add_argument("--tags", default=DEFAULT_TAGS); p.add_argument("--work", required=True)
    p.add_argument("--url"); p.add_argument("--canary", default=DEFAULT_CANARY); p.add_argument("--batch", type=int, default=32)
    p.add_argument("--min-count", type=int, default=2,
                   help="embed only forms used at least this often (default: reusable head, 2)")
    p.set_defaults(run=prepare)
    p = sub.add_parser("candidates", help="retrieve bounded semantic candidate concepts")
    p.add_argument("--work", required=True); p.add_argument("--out", required=True)
    p.add_argument("--min-count", type=int, default=2); p.add_argument("--options", type=int, default=6)
    p.add_argument("--source-min-count", type=int, default=2,
                   help="only adjudicate forms used at least this often (default: reusable head, 2)")
    p.add_argument("--threshold", type=float, default=.95,
                   help="exact cosine floor; 0.95 is the conservative equal-specificity boundary")
    p.add_argument("--tables", type=int, default=16)
    p.add_argument("--bits", type=int, default=10); p.add_argument("--probe", type=int, default=12)
    p.set_defaults(run=candidates)
    p = sub.add_parser("adjudicate", help="ask one bounded Jev Choice per candidate row")
    p.add_argument("--candidates", required=True); p.add_argument("--out", required=True)
    p.add_argument("--model", default=MODEL); p.add_argument("--workers", type=int, default=8)
    p.add_argument("--questions-per-call", type=int, default=16,
                   help="independent Choice questions sent in one Jev state (default: 16)")
    p.add_argument("--spend", action="store_true"); p.set_defaults(run=adjudicate)
    p = sub.add_parser("verify", help="independently classify every proposed merge; retain only exact")
    p.add_argument("--answers", required=True); p.add_argument("--out", required=True)
    p.add_argument("--model", default=MODEL); p.add_argument("--workers", type=int, default=8)
    p.add_argument("--questions-per-call", type=int, default=16)
    p.add_argument("--spend", action="store_true"); p.set_defaults(run=verify)
    p = sub.add_parser("materialize", help="write the complete surface-to-concept artifact")
    p.add_argument("--tags", default=DEFAULT_TAGS); p.add_argument("--candidates", required=True)
    p.add_argument("--answers", required=True); p.add_argument("--verification", required=True)
    p.add_argument("--threshold", type=float, default=.95)
    p.add_argument("--out", required=True); p.add_argument("--model", default=MODEL); p.set_defaults(run=materialize)
    return ap


def main(argv=None):
    args = parser().parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
