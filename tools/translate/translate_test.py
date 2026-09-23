#!/usr/bin/env python3
"""The translator's stdlib half: what it translates, how it splits and rejoins, and how it resumes. The
model itself is stubbed; what it produces was measured by the pilot in oxyc/den-dataset#89."""
import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import translate  # noqa: E402
from pipeline import compose, embed  # noqa: E402


def write_batch(directory, number, rows):
    with open(os.path.join(directory, f"batch-{number}.json"), "w", encoding="utf-8") as handle:
        json.dump(rows, handle)


class Stub:
    """Upper-cases the plot, standing in for a model."""
    def __init__(self, lang, spec, device):
        self.name = f"{spec['model']}@{spec['revision']}"

    def __call__(self, plot):
        if "boom" in plot:
            raise RuntimeError("boom")
        return plot.upper(), 0

    def close(self):
        pass


class Rules(unittest.TestCase):
    def test_the_plot_cap_is_the_embed_stages(self):
        self.assertEqual(translate.PLOT_CAP, embed.SHIPPED_COMPOSITION["plotCap"])

    def test_every_model_is_pinned_to_a_commit(self):
        # Portuguese has no opus-mt-pt-en; the multi-source Romance model has English as its only target.
        multi_source = {"pt": "Helsinki-NLP/opus-mt-ROMANCE-en"}
        for lang, spec in translate.read_models().items():
            self.assertEqual(spec["model"], multi_source.get(lang, f"Helsinki-NLP/opus-mt-{lang}-en"))
            self.assertRegex(spec["revision"], r"^[0-9a-f]{40}$")

    def test_sentences(self):
        self.assertEqual(translate.sentences("彼は来た。彼女は去った！なぜ？", "ja"), ["彼は来た。", "彼女は去った！", "なぜ？"])
        self.assertEqual(translate.sentences("그는 왔다. 그녀는 갔다.", "ko"), ["그는 왔다.", "그녀는 갔다."])
        self.assertEqual(translate.sentences("Il part. Élise reste. vers 3 h.", "fr"),
                         ["Il part.", "Élise reste. vers 3 h."])
        self.assertEqual(translate.sentences("Er kommt. Über Nacht.", "de"), ["Er kommt.", "Über Nacht."])

    def test_paragraphs_survive(self):
        paragraphs = [translate.sentences(p, "de") for p in "A. B.\n\nC.".split("\n")]
        self.assertEqual(translate.rejoin(paragraphs, ["a.", "b.", "c."]), "a. b.\n\nc.")


class Extract(unittest.TestCase):
    def test_what_is_extracted_is_what_the_embed_stage_composes(self):
        with tempfile.TemporaryDirectory() as out:
            write_batch(out, 1, [{"tmdbId": 1, "mediaType": "movie", "overview": "Alt.", "hasWikiPlot": True,
                                  "plotLanguage": "de"}])
            write_batch(out, 2, [
                {"tmdbId": 1, "mediaType": "movie", "overview": " Neu. \n", "hasWikiPlot": True,
                 "plotLanguage": "de"},
                {"tmdbId": 2, "mediaType": "movie", "overview": "English.", "hasWikiPlot": True,
                 "plotLanguage": "en"},
                {"tmdbId": 3, "mediaType": "tv", "overview": "Pas de plot.", "hasWikiPlot": False,
                 "plotLanguage": "fr"},
                {"tmdbId": 4, "mediaType": "movie", "overview": "Old row, no language.", "hasWikiPlot": True}])
            rows = translate.extract(out)
            label = {"subgenres": [], "moods": []}
            [(_, _, doc)] = compose.documents(out, {"movie:1": label}, {}, set(), translate.PLOT_CAP, {})
        self.assertEqual(rows, [{"key": "movie:1", "lang": "de", "plot": "Neu.",
                                 "source_sha256": compose.plot_sha("Neu.")}])
        self.assertEqual(doc, "Plot: " + rows[0]["plot"])


class Resume(unittest.TestCase):
    PLOTS = [{"key": f"movie:{n}", "lang": lang, "source_sha256": f"s{n}", "plot": plot}
             for n, (lang, plot) in enumerate((("de", "eins"), ("de", "boom"), ("pt", "um"), ("fr", "un")))]
    MODELS = {"de": {"model": "m-de", "revision": "r"}, "fr": {"model": "m-fr", "revision": "r"}}

    def setUp(self):
        self.original = translate.Translator
        translate.Translator = Stub

    def tearDown(self):
        translate.Translator = self.original

    def test_a_rerun_translates_only_what_is_missing(self):
        with tempfile.TemporaryDirectory() as out:
            cache = os.path.join(out, "t.jsonl")
            first = translate.run(self.PLOTS, cache, self.MODELS, set(), 0, "cpu")
            self.assertEqual(first["untranslated"], {"pt": 1})
            self.assertEqual([f["key"] for f in first["failures"]], ["movie:1"])
            rows = translate.read_jsonl(cache)
            self.assertEqual([(r["key"], r["english"], r["translator"]) for r in rows],
                             [("movie:0", "EINS", "m-de@r"), ("movie:3", "UN", "m-fr@r")])
            with open(cache, "a", encoding="utf-8") as handle:
                handle.write('{"key": "movie:9", "so')  # a kill mid-line
            changed = [dict(self.PLOTS[0], source_sha256="s0b", plot="zwei")] + self.PLOTS[1:]
            second = translate.run(changed, cache, self.MODELS, set(), 0, "cpu")
            self.assertEqual(second["pending"], 2)  # the changed plot, and the one that failed
            self.assertEqual(compose.read_translations(cache)[("movie:0", "s0b")], "ZWEI")
            self.assertEqual(len(translate.read_jsonl(cache)), 3)

    def test_one_language_at_a_time(self):
        with tempfile.TemporaryDirectory() as out:
            cache = os.path.join(out, "t.jsonl")
            translate.run(self.PLOTS, cache, self.MODELS, {"fr"}, 0, "cpu")
            self.assertEqual([r["lang"] for r in translate.read_jsonl(cache)], ["fr"])


class Plan(unittest.TestCase):
    def test_shards_cover_the_pending_plots_under_the_budget(self):
        plots = [{"key": f"movie:{n}", "lang": "de" if n % 3 else "it", "source_sha256": f"s{n}",
                  "plot": "x" * (10 + n)} for n in range(30)]
        models = {"de": {}, "it": {}}
        with tempfile.TemporaryDirectory() as out:
            matrix = translate.plan(plots, {("movie:1", "s1")}, models, 120, out)
            shards = {m["shard"]: translate.read_jsonl(os.path.join(out, m["shard"] + ".jsonl")) for m in matrix}
        for name, rows in shards.items():
            self.assertLessEqual(sum(len(r["plot"]) for r in rows), 120, name)
            self.assertTrue(all(re.match(r["lang"] + "-", name) for r in rows))
        keys = sorted(r["key"] for rows in shards.values() for r in rows)
        self.assertEqual(keys, sorted(p["key"] for p in plots if p["key"] != "movie:1"))


if __name__ == "__main__":
    unittest.main()
