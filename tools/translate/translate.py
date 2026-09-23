#!/usr/bin/env python3
"""English translations of the plots that are not in English, for the plot embedding (oxyc/den-dataset#89).

Untranslated, bge-m3 places a title with a German plot nearer other German plots than nearer its story. The
50-title pilot measured Helsinki-NLP's Opus-MT models level with hand translations at undoing that, so this
translates every such plot with them, into an append-only cache the embed stage reads (the
`plot_translations` artifact; `pipeline/compose.py` decides when a row applies).

Three steps, so the expensive one can run anywhere:

  extract  the plots to translate, from the enriched batches, as the embed stage composes them. Stdlib
           only. `source_sha256` is the hash of exactly the text after `Plot: `.
  plan     for CI: the pending plots split into one file per language and shard, each under a
           character budget, and the job matrix as JSON on stdout. Stdlib only.
  run      translate what the cache does not hold yet and append it. Needs `requirements.txt`.

A row is `{key, lang, source_sha256, translator, english}`. `run` skips every (key, source_sha256) the cache
holds, so a re-run translates only new or changed plots, and a killed run resumes where it stopped. A
language with no pinned model in `models.json` is left untranslated and listed in the summary.

The translation rules are the pilot's: each paragraph split into sentences (Japanese on 。！？), 16
sentences to a batch, beam search of 4, paragraph breaks kept.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from pipeline import compose  # noqa: E402
from pipeline.articles import key, ordered_batches  # noqa: E402

MODELS = os.path.join(HERE, "models.json")
#: The embed stage's plot cap, so the text hashed here is the text it composes. `pipeline/embed.py`'s
#: `SHIPPED_COMPOSITION`; `translate_test.py` holds the two equal.
PLOT_CAP = 3500
BATCH = 16
BEAMS = 4
#: Marian's window. A longer sentence is cut, and counted as `truncated` in the summary.
MAX_TOKENS = 512

#: Where a sentence ends: after . ! ? and before what starts one. Japanese has no spaces between
#: sentences; Korean has no capitals to start one.
SPLIT = {
    "ja": re.compile(r"(?<=[。！？])"),
    "ko": re.compile(r"(?<=[.!?])\s+"),
}
DEFAULT = re.compile(r'(?<=[.!?])\s+(?=[A-ZÀ-ÖØ-ÞŁŚŻŹĆА-ЯЁ0-9"«„(])')


def say(message):
    print(message, file=sys.stderr, flush=True)


def sentences(paragraph, lang):
    parts = (SPLIT.get(lang) or DEFAULT).split(paragraph)
    return [part.strip() for part in parts if part.strip()]


def read_models(path=MODELS):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def read_jsonl(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def extract(enriched_dir, cap=PLOT_CAP):
    """Every title whose plot is not in English, newest batch winning as in `compose.documents`."""
    seen, out = set(), []
    for name in reversed(ordered_batches(enriched_dir)):
        with open(os.path.join(enriched_dir, name), encoding="utf-8") as handle:
            batch = json.load(handle)
        for row in batch:
            title = key(row)
            if title in seen:
                continue
            seen.add(title)
            lang = row.get("plotLanguage") or "en"
            plot = compose.source_plot(row, cap)
            if lang == "en" or not plot:
                continue
            out.append({"key": title, "lang": lang, "source_sha256": compose.plot_sha(plot), "plot": plot})
    out.sort(key=lambda r: (r["lang"], r["key"]))
    return out


def repair(cache):
    """Drop a torn last line — a run killed mid-write — so the next append starts on a line of its own."""
    if not os.path.exists(cache):
        return
    with open(cache, "rb+") as handle:
        data = handle.read()
        if data and not data.endswith(b"\n"):
            cut = data.rfind(b"\n") + 1
            handle.truncate(cut)
            say(f"dropped a torn last line from {cache} ({len(data) - cut} bytes)")


def cached(cache):
    return set(compose.read_translations(cache)) if os.path.exists(cache) else set()


def pending(plots, done, models):
    """The plots to translate, and `{lang: titles}` for those no model covers."""
    todo, untranslated = [], {}
    for row in plots:
        if (row["key"], row["source_sha256"]) in done:
            continue
        if row["lang"] not in models:
            untranslated[row["lang"]] = untranslated.get(row["lang"], 0) + 1
            continue
        todo.append(row)
    return todo, untranslated


def plan(plots, done, models, max_chars, out_dir):
    """One file per (language, shard) of the pending plots, each shard under `max_chars` unless a single
    plot is larger; returns the matrix. Plots are dealt round-robin in key order, so shards are even."""
    todo, _ = pending(plots, done, models)
    by_lang = {}
    for row in todo:
        by_lang.setdefault(row["lang"], []).append(row)
    os.makedirs(out_dir, exist_ok=True)
    matrix = []
    for lang, rows in sorted(by_lang.items()):
        chars = sum(len(r["plot"]) for r in rows)
        n = max(1, -(-chars // max_chars))
        while n < len(rows) and any(sum(len(r["plot"]) for r in rows[i::n]) > max_chars for i in range(n)):
            n += 1
        for i in range(n):
            name = f"{lang}-{i}"
            with open(os.path.join(out_dir, name + ".jsonl"), "w", encoding="utf-8") as handle:
                handle.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows[i::n]))
            matrix.append({"lang": lang, "shard": name})
    return matrix


def rejoin(paragraphs, translated):
    """The translated sentences back into their paragraphs, in order; an empty paragraph stays empty."""
    out, i = [], 0
    for sents in paragraphs:
        out.append(" ".join(translated[i:i + len(sents)]))
        i += len(sents)
    return "\n".join(out)


class Translator:
    def __init__(self, lang, spec, device):
        import torch
        from transformers import MarianMTModel, MarianTokenizer
        from transformers.utils import logging as hf_logging
        # Otherwise every batch warns that the model's own max_length is overridden by max_new_tokens.
        hf_logging.set_verbosity_error()
        self.torch, self.device = torch, device
        self.name = f"{spec['model']}@{spec['revision']}"
        self.tok = MarianTokenizer.from_pretrained(spec["model"], revision=spec["revision"])
        self.model = MarianMTModel.from_pretrained(spec["model"], revision=spec["revision"]).to(device).eval()
        self.lang = lang

    def __call__(self, plot):
        """The plot in English, and how many of its sentences were longer than Marian's window."""
        paragraphs = [sentences(p, self.lang) for p in plot.split("\n")]
        flat = [s for p in paragraphs for s in p]
        truncated = sum(len(ids) > MAX_TOKENS for ids in self.tok(flat)["input_ids"]) if flat else 0
        # Similar lengths batched together pad less; each result goes back to its own place.
        order = sorted(range(len(flat)), key=lambda i: len(flat[i]))
        out = [""] * len(flat)
        for start in range(0, len(order), BATCH):
            idx = order[start:start + BATCH]
            enc = self.tok([flat[i] for i in idx], return_tensors="pt", padding=True, truncation=True,
                           max_length=MAX_TOKENS).to(self.device)
            with self.torch.no_grad():
                gen = self.model.generate(**enc, num_beams=BEAMS, max_new_tokens=MAX_TOKENS)
            for i, text in zip(idx, self.tok.batch_decode(gen, skip_special_tokens=True)):
                out[i] = text
        return rejoin(paragraphs, out), truncated

    def close(self):
        del self.model
        if self.device == "mps":
            self.torch.mps.empty_cache()


def device_for(choice):
    if choice != "auto":
        return choice
    import torch
    return "mps" if torch.backends.mps.is_available() else "cpu"


def run(plots, cache, models, langs, stop_after, device):
    """Translate what `cache` lacks, appending a row per title as it finishes. Returns the summary."""
    repair(cache)
    todo, untranslated = pending(plots, cached(cache), models)
    if langs:
        todo = [r for r in todo if r["lang"] in langs]
    os.makedirs(os.path.dirname(os.path.abspath(cache)), exist_ok=True)
    device = device_for(device) if todo else device
    started, stopped = time.time(), False
    summary = {"device": device, "pending": len(todo), "languages": {}, "untranslated": untranslated,
               "failures": []}
    by_lang = {}
    for row in todo:
        by_lang.setdefault(row["lang"], []).append(row)
    with open(cache, "a", encoding="utf-8") as out:
        for lang, rows in sorted(by_lang.items()):
            if stopped:
                break
            total_chars = sum(len(r["plot"]) for r in rows)
            say(f"{lang}: {len(rows)} plots, {total_chars} chars, model {models[lang]['model']}")
            translate = Translator(lang, models[lang], device)
            stats = summary["languages"][lang] = {"titles": 0, "chars": 0, "seconds": 0.0, "truncated": 0}
            for n, row in enumerate(rows, 1):
                if stop_after and time.time() - started > stop_after:
                    say(f"stopping after {stop_after}s as asked; re-run to continue")
                    stopped = True
                    break
                t0 = time.time()
                try:
                    english, truncated = translate(row["plot"])
                except Exception as failure:  # one bad plot must not end an hours-long run
                    say(f"FAILED {row['key']} ({lang}): {type(failure).__name__}: {failure}")
                    summary["failures"].append({"key": row["key"], "lang": lang, "error": str(failure)[:200]})
                    continue
                out.write(json.dumps({"key": row["key"], "lang": lang, "source_sha256": row["source_sha256"],
                                      "translator": translate.name, "english": english},
                                     ensure_ascii=False) + "\n")
                out.flush()
                stats["titles"] += 1
                stats["chars"] += len(row["plot"])
                stats["seconds"] += time.time() - t0
                stats["truncated"] += truncated
                if n % 25 == 0 or n == len(rows):
                    rate = stats["chars"] / max(stats["seconds"], 1e-9)
                    left = total_chars - stats["chars"]
                    say(f"{lang}: {n}/{len(rows)} plots, {rate:.0f} chars/s, ~{left / rate / 60:.0f} min left "
                        f"in {lang}")
            stats["charsPerSecond"] = round(stats["chars"] / max(stats["seconds"], 1e-9))
            stats["seconds"] = round(stats["seconds"], 1)
            translate.close()
    summary["stopped"] = stopped
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    ex = sub.add_parser("extract", help="write the plots to translate from the enriched batches")
    ex.add_argument("--enriched", required=True, help="the out-dir's enriched/ directory")
    ex.add_argument("--out", required=True)
    pl = sub.add_parser("plan", help="split the pending plots into shards; print the job matrix")
    pl.add_argument("--plots", required=True)
    pl.add_argument("--cache", required=True)
    pl.add_argument("--max-chars", type=int, required=True, help="source characters per shard")
    pl.add_argument("--out-dir", required=True)
    ru = sub.add_parser("run", help="translate what the cache lacks")
    ru.add_argument("--plots", required=True)
    ru.add_argument("--cache", required=True, help="the append-only translations JSONL")
    ru.add_argument("--lang", action="append", help="only this language (repeatable)")
    ru.add_argument("--stop-after", type=int, default=0, help="stop cleanly after this many seconds")
    ru.add_argument("--device", default="auto", help="auto (mps, else cpu), cpu or mps")
    args = parser.parse_args(argv)
    models = read_models()

    if args.command == "extract":
        rows = extract(args.enriched)
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        langs = {}
        for r in rows:
            langs[r["lang"]] = langs.get(r["lang"], 0) + 1
        say(f"{len(rows)} plots not in English, {sum(len(r['plot']) for r in rows)} chars: {langs}")
        return 0
    plots = read_jsonl(args.plots)
    if args.command == "plan":
        matrix = plan(plots, cached(args.cache), models, args.max_chars, args.out_dir)
        print(json.dumps(matrix))
        return 0
    summary = run(plots, args.cache, models, set(args.lang or ()), args.stop_after, args.device)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
