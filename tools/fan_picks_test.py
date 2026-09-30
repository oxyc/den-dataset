"""`tools/fan_picks.py`: the prompt, what an answer parses to, how a written name finds a store title, what is
dropped, what the store is handed, and the spend check — offline, with no model and no network."""
import gzip
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fan_picks as fp  # noqa: E402


def row(key, en, year, orig=None, aliases=(), **facts):
    titles = {"en": en, **({"orig": orig} if orig else {}), **({"aliases": list(aliases)} if aliases else {})}
    date = {"released" if key.startswith("movie:") else "started": {"date": f"{year}-01-01"}} if year else {}
    return {"key": key, "facts": {"titles": titles, **date, **facts}}


ROWS = {r["key"]: r for r in (
    row("movie:1", "Amélie", 2001, orig="Le Fabuleux Destin d'Amélie Poulain"),
    row("movie:2", "Seven Samurai", 1954),
    row("movie:3", "Ocean's Eleven", 2001),
    row("movie:4", "Ocean's Eleven", 1960),
    row("movie:5", "Dreams", 1990, aliases=["Akira Kurosawa's Dreams"]),
    row("movie:6", "Crash", 2004),
    row("movie:7", "Crash", 2004),
    row("movie:8", "Heat", 1995, otherVersions=[{"key": "movie:9", "kind": "remake"}]),
    row("movie:9", "L.A. Takedown", 1989, otherVersions=[{"key": "movie:8", "kind": "remake"}]),
    row("movie:10", "Alien", 1979, followedBy=["Q2"]),
    row("movie:11", "Aliens", 1986),
    row("movie:12", "Toy Story", 1995),
    row("movie:13", "Toy Story 2", 1999),
    row("tv:1", "The Office", 2005),
    row("tv:2", "The Office", 2001),
    row("tv:3", "Gamma (TV series)", 2010),
)}


class Prompt(unittest.TestCase):
    def test_the_prompt_names_title_year_kind_and_lead_and_asks_for_json(self):
        text = fp.prompt({"key": "tv:1438", "title": "The Wire", "year": 2002, "lead": "A crime drama"})
        self.assertTrue(text.startswith("A friend loved The Wire (2002, series). A crime drama. Name up to 20"))
        self.assertIn('"known": bool', text)
        no_year = fp.prompt({"key": "movie:1", "title": "X", "year": None, "lead": ""})
        self.assertTrue(no_year.startswith("A friend loved X (film). Name up to 20"))

    def test_the_request_asks_for_json_at_low_thinking(self):
        config = fp.request_body({"key": "movie:1", "title": "X", "year": 1, "lead": ""})["generationConfig"]
        self.assertEqual((config["responseMimeType"], config["thinkingConfig"]),
                         ("application/json", {"thinkingLevel": "low"}))

    def test_a_long_lead_is_cut_at_a_sentence(self):
        lead = fp.lead_of(("A sentence. " * 200) + "\n\n== Plot ==\nnot this")
        self.assertLessEqual(len(lead), fp.LEAD_CHARS)
        self.assertTrue(lead.endswith("sentence."))
        self.assertEqual(fp.lead_of("Short lead.\n\n== Plot ==\nx"), "Short lead.")


class Parse(unittest.TestCase):
    def test_an_answer_keeps_titled_picks_and_drops_the_rest(self):
        known, picks = fp.parse('{"known": true, "picks": [{"title": " Heat ", "year": 1995, "type": "film"},'
                                ' {"year": 2000}, {"title": "X", "year": "1999", "type": "tv"}]}')
        self.assertTrue(known)
        self.assertEqual(picks, [{"title": "Heat", "year": 1995, "type": "film"},
                                 {"title": "X", "year": None, "type": None}])

    def test_no_picks_list_is_an_error(self):
        with self.assertRaises(ValueError):
            fp.parse('{"known": false}')

    def test_a_record_prices_a_batch_answer_at_half(self):
        response = {"candidates": [{"content": {"parts": [{"text": '{"known":true,"picks":[]}'}]}}],
                    "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 1000,
                                      "thoughtsTokenCount": 1000}, "modelVersion": "m"}
        online, _ = fp.record(response, "movie:1", 0, "online")
        batch, _ = fp.record(response, "movie:1", 0, "batch", "batches/x")
        self.assertAlmostEqual(online["costUSD"], 1000 * fp.PRICE_IN + 2000 * fp.PRICE_OUT)
        self.assertAlmostEqual(batch["costUSD"], online["costUSD"] / 2)
        self.assertEqual(batch["batch"], "batches/x")
        _, error = fp.record({"candidates": [{"content": {"parts": [{"text": "{not json"}]},
                                              "finishReason": "MAX_TOKENS"}]}, "movie:1", 0, "online")
        self.assertIn("MAX_TOKENS", error["error"])


class Matching(unittest.TestCase):
    names = fp.Names(ROWS)

    def resolve(self, title, year, kind="film"):
        return self.names.resolve({"title": title, "year": year, "type": kind})

    def test_a_name_with_its_original_in_parentheses_matches_either(self):
        self.assertEqual(self.resolve("Amélie (Le Fabuleux Destin d'Amélie Poulain)", 2001), ("matched", "movie:1"))
        self.assertEqual(self.resolve("Le fabuleux destin d'Amelie Poulain", 2001), ("matched", "movie:1"))

    def test_digits_and_words_are_one_name(self):
        self.assertEqual(self.resolve("7 Samurai", 1954), ("matched", "movie:2"))
        self.assertEqual(self.resolve("Ocean's 11", 2001), ("matched", "movie:3"))

    def test_the_year_tells_a_remake_apart_within_two(self):
        self.assertEqual(self.resolve("Ocean's Eleven", 1960), ("matched", "movie:4"))
        self.assertEqual(self.resolve("Ocean's Eleven", 2003), ("matched", "movie:3"))
        self.assertEqual(self.resolve("Ocean's Eleven", 1980), ("unmatched", None))

    def test_the_nearer_year_wins_over_a_remake_two_years_off(self):
        names = fp.Names({r["key"]: r for r in (row("movie:1", "Twin", 2000), row("movie:2", "Twin", 2002))})
        self.assertEqual(names.resolve({"title": "Twin", "year": 2000, "type": "film"}), ("matched", "movie:1"))
        self.assertEqual(names.resolve({"title": "Twin", "year": 2001, "type": "film"}), ("ambiguous", None))

    def test_a_title_the_store_has_no_year_for_matches_whatever_the_picks_year(self):
        names = fp.Names({r["key"]: r for r in (row("tv:9", "Camping", None), row("movie:9", "Solo", 1990))})
        self.assertEqual(names.resolve({"title": "Camping", "year": 2016, "type": "series"}), ("matched", "tv:9"))
        # The only dated "Solo" is another work 20 years off: not taken.
        self.assertEqual(names.resolve({"title": "Solo", "year": 2010, "type": "film"}), ("unmatched", None))

    def test_a_possessive_is_tried_only_after_the_name_as_written(self):
        self.assertEqual(self.resolve("Kurosawa's Dreams", 1990), ("matched", "movie:5"))

    def test_two_titles_answering_one_name_are_dropped_not_guessed(self):
        self.assertEqual(self.resolve("Crash", 2004), ("ambiguous", None))

    def test_the_type_must_agree(self):
        self.assertEqual(self.resolve("The Office", 2005, "series"), ("matched", "tv:1"))
        self.assertEqual(self.resolve("The Office", 2005, "film"), ("unmatched", None))
        self.assertEqual(self.resolve("Gamma", 2010, "series"), ("matched", "tv:3"))

    def test_no_year_matches_only_a_name_one_title_carries(self):
        self.assertEqual(self.resolve("Seven Samurai", None), ("matched", "movie:2"))
        self.assertEqual(self.resolve("The Office", None, "series"), ("ambiguous", None))


class Dropped(unittest.TestCase):
    def test_the_seed_its_franchise_versions_sequels_and_repeats_are_dropped(self):
        franchises = {"franchises": {"f": {"members": [{"key": "movie:12"}, {"key": "movie:13"}]}},
                      "titles": {"movie:12": {"primary": "f"}, "movie:13": {"primary": "f"}}}
        owned = fp.related(ROWS, franchises, {"Q2": ["movie:11"]})
        self.assertEqual(owned["movie:12"], {"movie:13"})
        self.assertEqual(owned["movie:8"], {"movie:9"})
        self.assertEqual((owned["movie:10"], owned["movie:11"]), ({"movie:11"}, {"movie:10"}))
        names = fp.Names(ROWS)
        answer = {"picks": [{"title": "Toy Story", "year": 1995, "type": "film"},
                            {"title": "Toy Story 2", "year": 1999, "type": "film"},
                            {"title": "Heat", "year": 1995, "type": "film"},
                            {"title": "Heat", "year": 1995, "type": "film"},
                            {"title": "Nothing Like It", "year": 1995, "type": "film"}]}
        statuses = [p["status"] for p in fp.match_answer(answer, "movie:12", names, owned["movie:12"])]
        self.assertEqual(statuses, ["seed", "franchise or version", "matched", "duplicate", "unmatched"])


class Export(unittest.TestCase):
    def test_two_asks_lead_with_what_both_named(self):
        ask = lambda *keys: [{"status": "matched", "key": k} for k in keys]  # noqa: E731
        self.assertEqual(fp.merged([ask("a", "b", "c")]), ["a", "b", "c"])
        self.assertEqual(fp.merged([ask("a", "b", "c"), ask("c", "d", "a")]), ["a", "c", "b", "d"])

    def test_every_asked_title_is_exported_even_with_no_picks(self):
        with tempfile.TemporaryDirectory() as work:
            w = self.prepared(work)
            matched = {"movie:1": {"known": True, "asks": [[{"status": "matched", "key": "movie:2"},
                                                            {"status": "unmatched", "title": "Y"}]]},
                       "movie:2": {"known": False, "asks": [[{"status": "unmatched", "title": "Z"}]]}}
            fp.write_json(w.path("matched.json.gz"), matched, gz=True)
            with open(w.path("answers.jsonl"), "w") as fh:
                fh.write("{}\n")
            out = os.path.join(work, "fan-picks.json")
            result = fp.export(work, out)
            with open(out) as fh:
                exported = json.load(fh)
        self.assertEqual(exported["anchors"], {"movie:1": ["movie:2"], "movie:2": []})
        self.assertEqual((result["anchors"], result["withPicks"]), (2, 1))

    @staticmethod
    def prepared(work):
        corpus, articles, pool = (os.path.join(work, n) for n in ("c.jsonl.gz", "a.jsonl", "p.json.gz"))
        with gzip.open(corpus, "wt") as fh:
            for r in (row("movie:1", "One", 2000), row("movie:2", "Two", 2001)):
                fh.write(json.dumps(r) + "\n")
        with open(articles, "w") as fh:
            fh.write(json.dumps({"mediaType": "movie", "tmdbId": 1, "text": "Lead one.\n\n== Plot ==\nx"}) + "\n")
        with gzip.open(pool, "wt") as fh:
            json.dump({"popularity": {"movie:2": {"rank": 1, "typeSize": 2}, "movie:1": {"rank": 2, "typeSize": 2}}}, fh)
        fp.prepare(corpus, articles, pool, work, 1.0)
        return fp.Work(work)


class Spend(unittest.TestCase):
    def test_a_chunk_is_admitted_only_while_its_projection_fits_the_caps(self):
        with tempfile.TemporaryDirectory() as work:
            w = Export.prepared(work)
            self.assertEqual(w.order, ["movie:2", "movie:1"])
            items = [("movie:2", 0), ("movie:1", 0)]
            each = fp.PILOT_COST * fp.MARGIN
            admitted, _ = fp.admit(w, items, "online", run_cap=each * 1.5)
            self.assertEqual(admitted, items[:1])
            admitted, _ = fp.admit(w, items, "batch")
            self.assertEqual(admitted, items)
            with open(w.path("answers.jsonl"), "w") as fh:
                fh.write(json.dumps({"key": "movie:2", "costUSD": 0.9999, "usage": {}}) + "\n")
            self.assertEqual(fp.admit(w, items, "batch")[0], [])


class Daily(unittest.TestCase):
    def fixture(self, directory, anchors=None):
        corpus = os.path.join(directory, "corpus.jsonl.gz")
        with gzip.open(corpus, "wt", encoding="utf-8") as fh:
            for key in ("movie:1", "movie:2", "movie:8", "movie:9"):
                fh.write(json.dumps(ROWS[key]) + "\n")
        articles = os.path.join(directory, "articles.jsonl")
        with open(articles, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"mediaType": "movie", "tmdbId": 1,
                                 "text": "A whimsical romantic comedy.\n\n== Plot ==\nNo."}) + "\n")
        franchises = os.path.join(directory, "franchises.json")
        with open(franchises, "w", encoding="utf-8") as fh:
            json.dump({"franchises": {}, "titles": {}}, fh)
        existing = os.path.join(directory, "fan-picks.json")
        with open(existing, "w", encoding="utf-8") as fh:
            json.dump({"anchors": anchors or {"movie:2": ["movie:8"], "movie:99": ["movie:2"]}}, fh)
        return corpus, articles, franchises, existing

    def test_new_titles_use_the_full_prompt_match_and_merge_into_the_existing_input(self):
        with tempfile.TemporaryDirectory() as directory:
            corpus, articles, franchises, existing = self.fixture(directory)
            seen = []

            def generate(title, key, ask):
                seen.append((title, key, ask))
                return ({"picks": [{"title": "Seven Samurai", "year": 1954, "type": "film"}],
                         "costUSD": 0.003}, None, True)

            result = fp.daily_update(corpus, articles, franchises, existing, existing, ["movie:1"],
                                     workers=1, generate=generate, follows={})
            with open(existing, encoding="utf-8") as fh:
                out = json.load(fh)
        self.assertEqual(out["anchors"], {"movie:1": ["movie:2"], "movie:2": ["movie:8"]})
        self.assertEqual((result["asked"], result["answered"], result["costUSD"]), (1, 1, 0.003))
        self.assertEqual(seen[0][0]["lead"], "A whimsical romantic comedy.")
        self.assertEqual(fp.request_body(seen[0][0])["generationConfig"]["thinkingConfig"],
                         {"thinkingLevel": "low"})

    def test_a_provider_refusal_stays_unasked_like_the_full_run(self):
        with tempfile.TemporaryDirectory() as directory:
            corpus, articles, franchises, existing = self.fixture(directory, {"movie:2": []})
            error = {"costUSD": 0.001, "error": "no picks list", "usage": {}}
            result = fp.daily_update(corpus, articles, franchises, existing, existing, ["movie:1"],
                                     workers=1, generate=lambda *_: (None, error, True), follows={})
            with open(existing, encoding="utf-8") as fh:
                out = json.load(fh)
        self.assertNotIn("movie:1", out["anchors"])
        self.assertEqual((result["asked"], result["emptyAnswers"]), (1, 1))

    def test_a_valid_empty_answer_is_asked_like_the_full_run(self):
        with tempfile.TemporaryDirectory() as directory:
            corpus, articles, franchises, existing = self.fixture(directory, {"movie:2": []})
            result = fp.daily_update(
                corpus, articles, franchises, existing, existing, ["movie:1"], workers=1,
                generate=lambda *_: ({"picks": [], "costUSD": 0.001}, None, True), follows={})
            with open(existing, encoding="utf-8") as fh:
                out = json.load(fh)
        self.assertEqual(out["anchors"]["movie:1"], [])
        self.assertEqual((result["answered"], result["emptyAnswers"]), (1, 0))

    def test_the_whole_daily_set_must_fit_the_spend_cap_before_any_call(self):
        with tempfile.TemporaryDirectory() as directory:
            corpus, articles, franchises, existing = self.fixture(directory)
            called = []
            with self.assertRaisesRegex(RuntimeError, "per-run cap"):
                fp.daily_update(corpus, articles, franchises, existing, existing, ["movie:1"], workers=1,
                                generate=lambda *args: called.append(args), follows={}, max_spend=0.000001)
            self.assertEqual(called, [])

    def test_an_unreachable_provider_refuses_without_replacing_the_input(self):
        with tempfile.TemporaryDirectory() as directory:
            corpus, articles, franchises, existing = self.fixture(directory)
            with open(existing, encoding="utf-8") as fh:
                before = fh.read()
            with self.assertRaisesRegex(RuntimeError, "Gemini unreachable"):
                fp.daily_update(corpus, articles, franchises, existing, existing, ["movie:1"], workers=1,
                                generate=lambda *_: (None, {"error": "HTTP 503"}, False), follows={})
            with open(existing, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), before)

    def test_withdrawn_anchors_and_picks_are_removed_before_the_store_build(self):
        with tempfile.TemporaryDirectory() as directory:
            corpus, articles, franchises, existing = self.fixture(
                directory, {"movie:2": ["movie:8", "movie:99"], "movie:99": ["movie:2"]})
            result = fp.daily_update(corpus, articles, franchises, existing, existing, [], follows={})
            with open(existing, encoding="utf-8") as fh:
                out = json.load(fh)
        self.assertEqual(out["anchors"], {"movie:2": ["movie:8"]})
        self.assertEqual((result["asked"], result["anchors"], result["picks"]), (0, 1, 1))


if __name__ == "__main__":
    unittest.main()
