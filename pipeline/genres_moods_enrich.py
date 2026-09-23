#!/usr/bin/env python3
"""A hand enrichment of genres & moods, by labelling agents: `./den genres-moods prepare|merge`.

    ./den genres-moods prepare [--out-dir DIR] [--missing | --keys FILE | --since YYYY-MM-DD] [--batch 25]
    ./den genres-moods merge --model NAME [--web] [--accept-drop]

Genres & moods (a primary genre, up to three subgenres and up to three moods per title) for the titles
already in `data/genres-moods-curated.json` came from Claude Code subagents, and nothing here can rebuild
them. New titles get an automated labeller; this is the optional enrichment on top, run by hand with
whatever agents are to hand (Claude Code subagents on Opus, Sonnet or Haiku; Codex), and it overrides the
automated answer in the curated file (oxyc/den-dataset#56). It spends nothing itself: the agents do the
work, one batch file each. AGENTS.md has the procedure.

`prepare` (here) picks titles and writes the kit into a work dir (default `<out-dir>/genres-moods-enrich`,
which the out-dir's gitignore covers): `in/batch-NNN.json`, `SPEC.md` with the vocabulary and definitions,
`validate.py` for the agents, and `prepare.json`, the record `merge` reads back.

- **Which titles.** `--missing` (the default): titles the classify pass answered that the curated file
  lacks, plus curated titles whose subgenres AND moods are both empty. `--keys FILE`: the `mediaType:tmdbId`
  keys in the file, one a line. `--since YYYY-MM-DD`: titles released in that date's year or later — the
  rows carry a release year and nothing finer.
- **What an agent reads.** The lead plus every section the classify pass judged story-premise or
  theme-subject (their probabilities summed at >= 0.5), from `articles.jsonl`. A title is skipped, and
  counted under its reason, when it has no classify row, the pass found the article is not about the
  requested work, it has no article, the article changed since the pass judged its sections, or it is new
  to the curated file and has no enrichment row to take `animated` from.
- **The vocabulary** is `data/genres-moods-vocabulary.json`'s, the file the classify pass hashes; the definitions are
  `data/genres-moods-definitions.json`, refused unless it defines exactly that vocabulary.

`merge` (`genres_moods_merge.py`) validates the answers, keeps labels by July's `assemble` rule and writes
them into the curated file behind the quality gate.
"""
import datetime
import json
import os
import re
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
from pipeline import artifacts, consolidate_corpus  # noqa: E402
from pipeline.article_sections import parse_sections  # noqa: E402
from pipeline.combined_questions import TAXONOMY, taxonomy  # noqa: E402
from pipeline.contract import Context, StageError  # noqa: E402

CURATED = os.path.join(REPO, "data", "genres-moods-curated.json")
DEFINITIONS = os.path.join(REPO, "data", "genres-moods-definitions.json")
SPEC_TEMPLATE = os.path.join(REPO, "data", "genres-moods-enrich.SPEC.md")
#: A section is premise when the classify pass's story-premise and theme-subject probabilities sum to this.
PREMISE_P = 0.5
VALID = "correct-screen-work"


def vocabulary(taxonomy_path=TAXONOMY, definitions_path=DEFINITIONS):
    """The taxonomy's labels per family, with their definitions, refusing a definitions file that does not
    define exactly the taxonomy's labels."""
    tax = taxonomy(taxonomy_path)
    with open(definitions_path, encoding="utf-8") as fh:
        defs = json.load(fh)
    want = {"primaryGenres": tax["primaryGenres"], "subgenres": tax["subgenres"] + tax["thematic"],
            "moods": tax["moods"]}
    for family, labels in want.items():
        have = set(defs.get(family) or {})
        if have != set(labels):
            raise StageError(f"{definitions_path}: {family} do not match the taxonomy (undefined "
                             f"{sorted(set(labels) - have)}, unknown {sorted(have - set(labels))}) — "
                             f"define the taxonomy's labels before labelling with them")
    return {"version": tax["version"], "primary": tax["primaryGenres"], "sub": tax["subgenres"],
            "theme": tax["thematic"], "mood": tax["moods"], "definitions": defs}


def spec_text(vocab):
    """SPEC.md: the committed instructions, with the vocabulary and definitions filled in."""
    defs = vocab["definitions"]
    lines = [f"Taxonomy version {vocab['version']}. Use these names exactly, including punctuation and case.",
             "", "### primary_genre (exactly one)", ""]
    lines += [f"- **{g}**: {defs['primaryGenres'][g]}" for g in vocab["primary"]]
    lines += ["", "### subgenres (blended subgenres and themes; at most 3)", ""]
    lines += [f"- **{l}**: {defs['subgenres'][l]}" for l in vocab["sub"] + vocab["theme"]]
    lines += ["", "### moods (at most 3)", ""]
    lines += [f"- **{l}**: {defs['moods'][l]}" for l in vocab["mood"]]
    with open(SPEC_TEMPLATE, encoding="utf-8") as fh:
        return fh.read().replace("{vocabulary}", "\n".join(lines))


def key_of(row):
    return f"{row['mediaType']}:{row['tmdbId']}"


def sort_key(key):
    media, tmdb_id = key.split(":")
    return media, int(tmdb_id)


def read_curated(path):
    """(head, titles) — the file's own title order is kept, because it is the line order."""
    with open(path, encoding="utf-8") as fh:
        head = json.load(fh)
    return head, head.pop("titles")


def read_classify(paths, withdrawn=None):
    """`key` → what `prepare` needs from its classify row.

    The row that answers for a title is the corpus join's: across shards the latest run wins by key, and a
    title withdrawn before that run started has none (`consolidate_corpus.latest`). Reading the shards
    any other way would label a re-grounded title from the article its old run read. `withdrawn` is the
    out-dir's tombstone file, when there is one.
    """
    def trim(row):
        keep = []
        for s in row.get("sections") or []:
            p = (s.get("role") or {}).get("probabilities") or {}
            story = p.get("story-premise", 0) + p.get("theme-subject", 0)
            if s["id"] == "s000" or story >= PREMISE_P:
                keep.append((s["id"], s["textSha256"]))
        validity = (((row.get("answers") or {}).get("validity")) or {}).get("choice")
        return {"keep": keep, "validity": validity, "year": row.get("year"), "title": row.get("title")}

    try:
        withdrawals = consolidate_corpus.read_withdrawals(withdrawn) if withdrawn else None
        rows, _, _ = consolidate_corpus.latest(paths, "classify", withdrawals, keep=trim)
    except SystemExit as refused:
        raise StageError(str(refused)) from None
    return rows


def read_animated(enriched_dir):
    """`key` → the batch row's `animated` flag — Wikidata's, read off its genres and types by `enrich` —
    the newest batch that states one winning.

    A row that states none is not read as `false`. Older batches carry TMDB's genre ids instead, and that
    flag is TMDB's (oxyc/den-dataset#53): such a title has no enrichment row to take `animated` from until
    it is enriched again."""
    out = {}
    if not os.path.isdir(enriched_dir):
        return out
    names = [n for n in os.listdir(enriched_dir) if re.fullmatch(r"batch-\d+\.json", n)]
    for name in sorted(names, key=lambda n: int(n[6:-5])):
        with open(os.path.join(enriched_dir, name), encoding="utf-8") as fh:
            for row in json.load(fh):
                if isinstance(row.get("animated"), bool):
                    out[key_of(row)] = row["animated"]
    return out


def premise(article, keep):
    """The lead plus the kept sections, or None when the article is not the one the pass judged."""
    by_id = {s["id"]: s for s in parse_sections(article["text"])}
    parts = []
    for sid, sha in keep:
        s = by_id.get(sid)
        if s is None or s["textSha256"] != sha:
            return None
        parts.append(s["text"] if sid == "s000" else f"## {s['heading']}\n{s['text']}")
    return "\n\n".join(p for p in parts if p.strip())


def select(mode, titles, classify, keys=None, since=None):
    """The keys a selection mode names, before anything is skipped."""
    if mode == "keys":
        return list(dict.fromkeys(keys))
    if mode == "since":
        year = datetime.date.fromisoformat(since).year
        return sorted((k for k, i in classify.items() if isinstance(i["year"], int) and i["year"] >= year),
                      key=sort_key)
    empty = {k for k, e in titles.items() if not e.get("subgenres") and not e.get("moods")}
    return sorted((set(classify) - set(titles)) | empty, key=sort_key)


def skip_reason(info, article):
    if info is None:
        return "no classify row"
    if info["validity"] != VALID:
        return f"classify: not about the requested work ({info['validity']})"
    if article is None:
        return "no article"
    return None


def item(key, info, article, text):
    name = article.get("resolvedArticle") or article.get("article") or ""
    return {"key": key, "title": info.get("title") or article.get("title"), "year": info.get("year"),
            "media": "film" if key.startswith("movie:") else "series",
            "articleLanguage": article.get("language"),
            "articleUrl": f"https://{article.get('language')}.wikipedia.org/wiki/"
                          f"{urllib.parse.quote(name.replace(' ', '_'))}",
            "premise": text}


def prepare(out_dir="out", work=None, mode="missing", keys_file=None, since=None, batch=25, curated=CURATED,
            today=None, out=print):
    work = os.path.abspath(work or os.path.join(out_dir, "genres-moods-enrich"))
    if os.path.exists(os.path.join(work, "prepare.json")):
        raise StageError(f"{work} already holds a prepared enrichment — merge it, or name another --work")
    if batch < 1:
        raise StageError("--batch must be at least 1")
    if mode == "since":
        try:
            datetime.date.fromisoformat(since or "")
        except ValueError:
            raise StageError(f"--since wants YYYY-MM-DD, got {since!r}") from None
    vocab = vocabulary()
    ctx = Context(out_dir=out_dir, dataset_version="")
    classify = read_classify(ctx.require_all(artifacts.COMBINED), ctx.require(artifacts.WITHDRAWN))
    _, titles = read_curated(curated)
    keys = None
    if mode == "keys":
        with open(keys_file, encoding="utf-8") as fh:
            keys = [line.strip() for line in fh if line.strip()]
        bad = [k for k in keys if not re.fullmatch(r"(movie|tv):\d+", k)]
        if bad:
            raise StageError(f"{keys_file}: not mediaType:tmdbId keys: {bad[:5]}")
    chosen = select(mode, titles, classify, keys, since)
    wanted = set(chosen)
    # Streamed, keeping only the chosen rows: the dump is ~450 MB of article text.
    articles, orphans = {}, 0
    with open(ctx.require(artifacts.ARTICLES), encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            key = key_of(row)
            if key in wanted:
                articles[key] = row
            elif mode == "missing" and key not in classify and key not in titles:
                orphans += 1
    animated = read_animated(ctx.path(artifacts.ENRICHED))
    skipped, items, new_flags = {}, [], {}
    for key in chosen:
        info, article = classify.get(key), articles.get(key)
        reason = skip_reason(info, article)
        if reason is None:
            text = premise(article, info["keep"])
            reason = ("the article changed since the classify pass judged its sections" if text is None
                      else "empty premise" if not text.strip()
                      else "new, with no enrichment row to take `animated` from"
                      if key not in titles and key not in animated else None)
        if reason:
            skipped.setdefault(reason, []).append(key)
            continue
        if key not in titles:
            new_flags[key] = animated[key]
        items.append(item(key, info, article, text))

    width = max(3, len(str(-(-len(items) // batch))))
    batches = {}
    os.makedirs(os.path.join(work, "in"), exist_ok=True)
    os.makedirs(os.path.join(work, "out"), exist_ok=True)
    for i in range(0, len(items), batch):
        name = f"batch-{i // batch + 1:0{width}d}.json"
        with open(os.path.join(work, "in", name), "w", encoding="utf-8") as fh:
            json.dump(items[i:i + batch], fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        batches[name] = [it["key"] for it in items[i:i + batch]]
    with open(os.path.join(work, "SPEC.md"), "w", encoding="utf-8") as fh:
        fh.write(spec_text(vocab))
    with open(os.path.join(work, "validate.py"), "w", encoding="utf-8") as fh:
        fh.write(f"#!/usr/bin/env python3\n\"\"\"Check one answer batch: python3 validate.py [--web] "
                 f"out/batch-NNN.json\"\"\"\nimport sys\nsys.path.insert(0, {REPO!r})\n"
                 f"from pipeline import genres_moods_merge\nsys.exit(genres_moods_merge.validate_main(sys.argv[1:], "
                 f"{work!r}))\n")
    record = {"preparedOn": today or datetime.date.today().isoformat(), "outDir": os.path.abspath(out_dir),
              "selection": {"mode": mode, "keys": keys_file, "since": since},
              "taxonomyVersion": vocab["version"], "batches": batches, "animated": new_flags}
    with open(os.path.join(work, "prepare.json"), "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=1)
        fh.write("\n")

    out(f"prepared {len(items)} titles in {len(batches)} batches of up to {batch}: {work}")
    for reason, ks in sorted(skipped.items()):
        out(f"  skipped {len(ks)}: {reason}, e.g. {', '.join(ks[:4])}")
    if orphans:
        out(f"  skipped {orphans}: in articles.jsonl with no classify row")
    if batches:
        out(f"\nGive one agent per batch ({min(batches)[:-5]} … {max(batches)[:-5]}) this instruction, "
            f"with NNN filled in:\n")
        out(f"  Follow {work}/SPEC.md for batch NNN. Input: {work}/in/batch-NNN.json. Write "
            f"{work}/out/batch-NNN.json. Web search is not allowed. From {work}, run "
            f"`python3 validate.py out/batch-NNN.json` until it prints ok.\n")
        out(f"With web search allowed, instead:\n\n  Follow {work}/SPEC.md for batch NNN. Input: "
            f"{work}/in/batch-NNN.json. Write {work}/out/batch-NNN.json and "
            f"{work}/out/batch-NNN.sources.json. Web search is allowed. From {work}, run "
            f"`python3 validate.py --web out/batch-NNN.json` until it prints ok.")
    return record


def register(commands):
    """`den genres-moods`'s flags, on `den`'s own subcommand parser."""
    steps = commands.add_parser("genres-moods", help="a hand enrichment of genres & moods by labelling "
                                "agents").add_subparsers(dest="step", required=True)
    prep = steps.add_parser("prepare", help="pick titles and write the agents' batches; spends nothing")
    prep.add_argument("--out-dir", default="out")
    prep.add_argument("--work", help="the kit's directory (default: <out-dir>/genres-moods-enrich)")
    pick = prep.add_mutually_exclusive_group()
    pick.add_argument("--missing", action="store_true",
                      help="titles with no curated entry, or with neither subgenres nor moods (default)")
    pick.add_argument("--keys", metavar="FILE", help="the mediaType:tmdbId keys in FILE, one a line")
    pick.add_argument("--since", metavar="YYYY-MM-DD", help="titles released in that date's year or later")
    prep.add_argument("--batch", type=int, default=25, help="titles per batch (default 25)")
    mrg = steps.add_parser("merge", help="validate the agents' answers and write them into the curated file")
    mrg.add_argument("--model", required=True, help="the model the agents ran on, e.g. sonnet")
    mrg.add_argument("--web", action="store_true", help="the agents could search the web; require sources")
    mrg.add_argument("--work", default=os.path.join("out", "genres-moods-enrich"))
    mrg.add_argument("--accept-drop", action="store_true",
                     help="write the result even when it scores below the quality floors")
    for sub in (prep, mrg):
        sub.add_argument("--curated", default=CURATED,
                         help="the curated genres & moods (default: %(default)s)")


def run(args):
    if args.step == "prepare":
        mode = "keys" if args.keys else "since" if args.since else "missing"
        prepare(out_dir=args.out_dir, work=args.work, mode=mode, keys_file=args.keys, since=args.since,
                batch=args.batch, curated=args.curated)
    else:
        from pipeline import genres_moods_merge
        genres_moods_merge.merge(args.work, args.model, web=args.web, accept_drop=args.accept_drop,
                                 curated=args.curated)
    return 0
