#!/usr/bin/env python3
"""One step, several models, one table (oxyc/den-dataset#183): the bake-off #182 had to script by hand.

  tools/bakeoff.py premise_tags --phase <gen dir> --models openai:gpt-5.6-luna anthropic:claude-haiku-4-5-20251001 \
      gemini:gemini-3.5-flash-lite --out <dir> --max-spend-usd 2
  tools/bakeoff.py fan_picks --work <full-run dir> --keys <keys.json> --corpus <corpus.jsonl.gz> \
      --franchises <franchises.json> --models gemini:gemini-3.7-flash:low openai:gpt-5.6-luna --out <dir> ...

A model is `provider:model[:thinking]`; everything else comes from the step's entry in `data/models.json`.
Each model answers every title online through `lib/llm.py`, with no fallback, so a refusal counts against the
model that made it. Every model's rows go to `<out>/<provider>-<model>.json` and the table to `<out>/table.md`:
answered, refused, format failures, $/title, $/year at `--per-day` titles, and the step's own quality
number — tags a title for premise tags, the share of named picks that match a store title and the kept
picks a title for fan picks. The premise triplet score needs den-embed and is run on the written tags with
`tools/rulers/score_triplets.py`.
"""
import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from lib import llm  # noqa: E402
from pipeline import premise_daily  # noqa: E402

import fan_picks  # noqa: E402


class FanPicksTask:
    name, version = "fan_picks", "fan-picks-prompt@" + fan_picks.digest(
        fan_picks.prompt({"key": "movie:0", "title": "X", "year": 2000, "lead": "L"}))[:12]

    @staticmethod
    def key(title):
        return title["key"]

    @staticmethod
    def request(titles):
        return fan_picks.question(titles[0])

    @staticmethod
    def parse(titles, answer):
        known, picks = fan_picks.parse(answer["text"])
        return {titles[0]["key"]: {"known": known, "picks": picks}}


def premise_items(args):
    manifest = premise_daily._json(os.path.join(args.phase, "manifest.json"))
    return [row for index in range(manifest["batches"])
            for row in premise_daily._json(os.path.join(args.phase, "in", f"batch-{index:04d}.json"))]


def fan_pick_items(args):
    w = fan_picks.Work(args.work)
    with open(args.keys, encoding="utf-8") as fh:
        return [w.titles[key] for key in json.load(fh) if key in w.titles]


def premise_quality(rows):
    tagged = [row["value"] for row in rows.values() if "value" in row]
    return {"tagsPerTitle": round(sum(map(len, tagged)) / max(len(tagged), 1), 2)}


def fan_pick_quality(rows, args):
    corpus = fan_picks.corpus_rows(args.corpus)
    with open(args.franchises, encoding="utf-8") as fh:
        franchises = json.load(fh)
    follows = {}
    if args.follows:
        with open(args.follows, encoding="utf-8") as fh:
            follows = json.load(fh)
    names, owned = fan_picks.Names(corpus), fan_picks.related(corpus, franchises, follows)
    named = found = kept = answered = 0
    for key, row in rows.items():
        if "value" not in row:
            continue
        answered += 1
        matched = fan_picks.match_answer(row["value"], key, names, owned.get(key, set()))
        named += len(matched)
        found += sum(1 for pick in matched if pick["status"] in ("matched", "seed", "franchise or version",
                                                                "duplicate"))
        kept += len(fan_picks.merged([matched]))
    return {"matchRate": round(found / max(named, 1), 3), "keptPerTitle": round(kept / max(answered, 1), 2)}


def model_cfg(step, spec):
    provider, model, *thinking = spec.split(":")
    overrides = {"provider": provider, "model": model, "mode": "online", "fallback": None}
    if thinking:
        overrides["thinking"] = thinking[0]
    cfg = llm.step(step, overrides)
    cfg["fallback"] = None
    return cfg


def run(args, log=sys.stderr):
    if args.step == "premise_tags":
        items, task_of, quality = premise_items(args), premise_daily.Task, premise_quality
    else:
        items, task_of, quality = fan_pick_items(args), (lambda cfg: FanPicksTask()), \
            (lambda rows: fan_pick_quality(rows, args))
    os.makedirs(args.out, exist_ok=True)
    budget = llm.Budget(args.max_spend_usd)
    table = []
    for spec in args.models:
        cfg = model_cfg(args.step, spec)
        if args.step == "fan_picks":
            cfg["titlesPerCall"] = 1
        rows, calls = llm.generate(cfg, task_of(cfg), items, budget, args.workers)
        spent = sum(call["costUSD"] for call in calls)
        print(json.dumps({"model": spec, "calls": len(calls), "costUSD": round(spent, 6),
                          "spentUSD": round(budget.spent, 6)}), file=log)
        with open(os.path.join(args.out, f"{cfg['provider']}-{cfg['model']}.json"), "w", encoding="utf-8") as fh:
            json.dump({"cfg": cfg, "rows": rows, "calls": calls}, fh, ensure_ascii=False, indent=1, sort_keys=True)
        per_title = spent / max(len(items), 1)
        table.append({"model": spec, "titles": len(items),
                      "answered": sum(1 for row in rows.values() if "value" in row),
                      "refused": sum(1 for row in rows.values() if row.get("refused")),
                      "formatFailures": sum(1 for row in rows.values() if "error" in row and not row.get("unavailable")),
                      "perTitleUSD": round(per_title, 6), "perYearUSD": round(per_title * args.per_day * 365, 2),
                      **quality(rows)})
    columns = list(table[0]) if table else []
    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    lines += ["| " + " | ".join(str(row[c]) for c in columns) + " |" for row in table]
    with open(os.path.join(args.out, "table.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return table


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("step", choices=("premise_tags", "fan_picks"))
    parser.add_argument("--models", nargs="+", required=True, help="provider:model[:thinking], each")
    parser.add_argument("--out", required=True)
    parser.add_argument("--max-spend-usd", type=float, required=True, help="over every model together")
    parser.add_argument("--per-day", type=float, default=5, help="titles a day, for $/year")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--phase", help="premise_tags: a worklist's gen/ directory")
    parser.add_argument("--work", help="fan_picks: a prepared full-run directory")
    parser.add_argument("--keys", help="fan_picks: the sample's keys")
    parser.add_argument("--corpus", help="fan_picks: the corpus the picks are matched against")
    parser.add_argument("--franchises", help="fan_picks: franchises.json")
    parser.add_argument("--follows", help="fan_picks: sequel links' corpus keys (`fan_picks.py match --follows`)")
    args = parser.parse_args(argv)
    needed = ("phase",) if args.step == "premise_tags" else ("work", "keys", "corpus", "franchises")
    missing = [f"--{name}" for name in needed if not getattr(args, name)]
    if missing:
        parser.error(f"{args.step} needs {', '.join(missing)}")
    print(json.dumps(run(args), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
