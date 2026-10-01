"""`tools/fan_picks.py`: the prompt, what an answer parses to, how a written name finds a store title, what is
dropped, what the store is handed, and the spend check — offline, with no model and no network."""
import gzip
import io
import json
import os
import re
import sys
import tempfile
import types
import unittest
import urllib.error
from unittest import mock

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
        self.assertTrue(text.endswith('Return only JSON: {"k":bool,"p":[["Title",1999,"f"|"s"]]}'),
                        "the compact answer format (#187, format B)")
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

    def test_a_compact_answer_reads_as_the_keyed_one_does(self):
        compact = fp.parse('{"k":true,"p":[[" Heat ",1995,"f"],["The Wire",2002,"s"],[2000],["X","1999","tv"],'
                           '["Y",2001,"film"],["Z"],"loose",["W",2003,["s"]]]}')
        keyed = fp.parse('{"known": true, "picks": [{"title": "Heat", "year": 1995, "type": "film"}]}')
        self.assertEqual(compact, (True, [{"title": "Heat", "year": 1995, "type": "film"},
                                          {"title": "The Wire", "year": 2002, "type": "series"},
                                          {"title": "X", "year": None, "type": None},
                                          {"title": "Y", "year": 2001, "type": "film"},
                                          {"title": "Z", "year": None, "type": None},
                                          {"title": "W", "year": 2003, "type": None}]))
        self.assertEqual(keyed[1][0], compact[1][0])
        self.assertEqual(fp.parse('{"k":false,"p":[]}'), (False, []))

    def test_no_picks_list_is_an_error(self):
        with self.assertRaises(ValueError):
            fp.parse('{"known": false}')
        with self.assertRaises(ValueError):
            fp.parse('{"k": false, "p": "none"}')

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


class Retry(unittest.TestCase):
    def error(self, retry_after):
        return urllib.error.HTTPError("https://example.test", 429, "slow down",
                                      {"Retry-After": retry_after}, None)

    def test_retry_after_accepts_seconds_and_http_dates_and_is_bounded(self):
        now = fp.datetime.datetime(2026, 9, 30, 12, 0, tzinfo=fp.datetime.timezone.utc)
        self.assertEqual(fp.providers.retry_delay(self.error("17"), 0, now), 17)
        self.assertEqual(fp.providers.retry_delay(self.error("Wed, 30 Sep 2026 12:01:00 GMT"), 0, now), 60)
        self.assertEqual(fp.providers.retry_delay(self.error("999"), 0, now), fp.providers.MAX_RETRY_DELAY)

    def test_generate_online_sleeps_for_the_provider_delay_before_retrying(self):
        response = {"candidates": [{"content": {"parts": [{"text": '{"known":true,"picks":[]}'}]}}]}
        with mock.patch.object(fp.providers, "http", side_effect=[self.error("23"), ({}, response)]), \
             mock.patch.object(fp.providers.time, "sleep") as sleep, \
             mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
            answer, error, accepted = fp.generate_online({"key": "movie:1", "title": "One"}, "movie:1")
        sleep.assert_called_once_with(23)
        self.assertTrue(accepted)
        self.assertIsNone(error)
        self.assertEqual(answer["picks"], [])


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
                pass
            out = os.path.join(work, "fan-picks.json")
            result = fp.export(work, out)
            with open(out) as fh:
                exported = json.load(fh)
        self.assertEqual(exported["anchors"], {"movie:1": ["movie:2"], "movie:2": []})
        self.assertEqual((result["anchors"], result["withPicks"]), (2, 1))

    def test_provider_accepted_empty_is_asked_but_malformed_and_transport_errors_are_not(self):
        with tempfile.TemporaryDirectory() as work:
            w = self.prepared(work)
            fp.write_json(w.path("matched.json.gz"), {}, gz=True)
            open(w.path("answers.jsonl"), "w").close()
            with open(w.path("errors.jsonl"), "w") as fh:
                for error in ({"key": "movie:1", "usage": {}, "text": ""},
                              {"key": "movie:2", "usage": {}, "text": "not json"},
                              {"key": "movie:3", "error": "HTTP 503"}):
                    fh.write(json.dumps(error) + "\n")
            out = os.path.join(work, "fan-picks.json")
            fp.export(work, out)
            with open(out) as fh:
                exported = json.load(fh)
            errors_sha = fp.file_digest(w.path("errors.jsonl"))
        self.assertEqual(exported["anchors"], {"movie:1": []})
        self.assertEqual(exported["errorsSha256"], errors_sha)

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

    def test_a_provider_refusal_is_asked_empty_like_the_full_run(self):
        with tempfile.TemporaryDirectory() as directory:
            corpus, articles, franchises, existing = self.fixture(directory, {"movie:2": []})
            error = {"costUSD": 0.001, "error": "no picks list", "usage": {}, "text": ""}
            result = fp.daily_update(corpus, articles, franchises, existing, existing, ["movie:1"],
                                     workers=1, generate=lambda *_: (None, error, True), follows={})
            with open(existing, encoding="utf-8") as fh:
                out = json.load(fh)
        self.assertEqual(out["anchors"]["movie:1"], [])
        self.assertEqual((result["asked"], result["emptyAnswers"]), (1, 1))

    def test_malformed_nonempty_provider_output_stays_unasked(self):
        with tempfile.TemporaryDirectory() as directory:
            corpus, articles, franchises, existing = self.fixture(directory, {"movie:2": []})
            error = {"costUSD": 0.001, "error": "bad json", "usage": {}, "text": "{"}
            result = fp.daily_update(corpus, articles, franchises, existing, existing, ["movie:1"],
                                     workers=1, generate=lambda *_: (None, error, True), follows={})
            with open(existing, encoding="utf-8") as fh:
                out = json.load(fh)
        self.assertNotIn("movie:1", out["anchors"])
        self.assertEqual((result["emptyAnswers"], result["parseErrors"]), (0, 1))

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

    def test_a_plan_only_title_with_no_corpus_row_is_reported_and_not_asked(self):
        with tempfile.TemporaryDirectory() as directory:
            corpus, articles, franchises, existing = self.fixture(directory, {"movie:2": []})
            called = []
            result = fp.daily_update(
                corpus, articles, franchises, existing, existing, ["movie:1", "tv:290720", "movie:1"],
                workers=1,
                generate=lambda title, key, ask: (called.append(key) or {"picks": [], "costUSD": 0.001},
                                                   None, True), follows={})
        self.assertEqual(called, ["movie:1"])
        self.assertEqual(result["notInCorpus"], ["tv:290720"])
        self.assertEqual(result["asked"], 1)

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

    def test_successes_are_checkpointed_before_an_aggregate_failure_and_resumed(self):
        with tempfile.TemporaryDirectory() as directory:
            corpus, articles, franchises, existing = self.fixture(directory, {"movie:8": []})
            calls = []

            def first_generate(_title, key, _ask):
                calls.append(key)
                if key == "movie:2":
                    return None, {"error": "HTTP 503"}, False
                return {"picks": [], "costUSD": 0.001}, None, True

            with self.assertRaisesRegex(RuntimeError, "Gemini unreachable"):
                fp.daily_update(corpus, articles, franchises, existing, existing, ["movie:1", "movie:2"],
                                workers=1, generate=first_generate, follows={})
            checkpoint = fp.daily_checkpoint_path(existing)
            with open(checkpoint, encoding="utf-8") as fh:
                saved = json.load(fh)
            self.assertEqual(set(saved["responses"]), {"movie:1"})

            resumed_calls = []
            result = fp.daily_update(
                corpus, articles, franchises, existing, existing, ["movie:1", "movie:2"], workers=1,
                generate=lambda _title, key, _ask: (resumed_calls.append(key) or {"picks": [], "costUSD": 0.002},
                                                     None, True), follows={})
            with open(existing, encoding="utf-8") as fh:
                out = json.load(fh)
        self.assertEqual(calls, ["movie:1", "movie:2"])
        self.assertEqual(resumed_calls, ["movie:2"])
        self.assertEqual((result["generated"], result["resumed"]), (1, 1))
        self.assertEqual(out["anchors"], {"movie:1": [], "movie:2": [], "movie:8": []})
        self.assertFalse(os.path.exists(checkpoint))

    def test_a_refused_title_is_asked_once_more_and_then_by_the_fallback_model(self):
        asked = []

        def online(title, key, ask=0, cfg=fp.CFG, budget=None):
            asked.append(cfg["model"])
            if cfg["model"] == fp.MODEL:
                return None, {"key": key, "costUSD": 0.001, "usage": {}, "text": ""}, True
            return {"key": key, "picks": [{"title": "Seven Samurai", "year": 1954, "type": "film"}],
                    "costUSD": 0.01, "provider": cfg["provider"], "model": cfg["model"]}, None, True
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(fp, "generate_online", online):
            corpus, articles, franchises, existing = self.fixture(directory, {"movie:2": []})
            result = fp.daily_update(corpus, articles, franchises, existing, existing, ["movie:1"],
                                     workers=1, follows={})
            with open(existing, encoding="utf-8") as fh:
                out = json.load(fh)
        self.assertEqual(asked, [fp.MODEL, fp.MODEL, fp.CFG["fallback"]["model"]])
        self.assertEqual(out["anchors"]["movie:1"], ["movie:2"])
        self.assertAlmostEqual(result["costUSD"], 0.012)

    def test_the_cap_holds_every_call_fallback_included_and_what_it_leaves_waits(self):
        """#201 review: the projection covered one Gemini call a title, not its retry and the fallback."""
        seen = []

        def online(title, key, ask=0, cfg=fp.CFG, budget=None):
            seen.append((key, cfg["model"]))
            try:
                budget.admit(0.4)
            except fp.llm.OverBudget:
                return None, {"key": key, "error": fp.OVER_BUDGET, "costUSD": 0.0}, False
            budget.charge(0.4, held=0.4)
            return None, {"key": key, "costUSD": 0.4, "usage": {}, "text": ""}, True
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(fp, "generate_online", online):
            corpus, articles, franchises, existing = self.fixture(directory, {"movie:2": []})
            result = fp.daily_update(corpus, articles, franchises, existing, existing, ["movie:1", "movie:8"],
                                     workers=1, follows={}, max_spend=1.0)
            with open(existing, encoding="utf-8") as fh:
                out = json.load(fh)
        self.assertLessEqual(result["costUSD"], 1.0)
        self.assertTrue(result["overBudget"], "a title the cap left unasked waits")
        self.assertTrue(all(key not in out["anchors"] for key in result["overBudget"]))

    def test_a_title_the_fallback_refuses_too_stays_asked_empty(self):
        refused = lambda title, key, ask=0, cfg=fp.CFG, budget=None: (  # noqa: E731
            None, {"key": key, "costUSD": 0.001, "usage": {}, "text": ""}, True)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(fp, "generate_online", refused):
            corpus, articles, franchises, existing = self.fixture(directory, {"movie:2": []})
            result = fp.daily_update(corpus, articles, franchises, existing, existing, ["movie:1"],
                                     workers=1, follows={})
            with open(existing, encoding="utf-8") as fh:
                out = json.load(fh)
        self.assertEqual((out["anchors"]["movie:1"], result["emptyAnswers"]), ([], 1))

    def test_an_unreachable_fallback_leaves_the_refusal_standing_instead_of_refusing_the_day(self):
        def online(title, key, ask=0, cfg=fp.CFG, budget=None):
            if cfg["model"] == fp.MODEL:
                return None, {"key": key, "costUSD": 0.001, "usage": {}, "text": ""}, True
            return None, {"key": key, "error": "ANTHROPIC_API_KEY is not set", "costUSD": 0.0}, False
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(fp, "generate_online", online):
            corpus, articles, franchises, existing = self.fixture(directory, {"movie:2": []})
            result = fp.daily_update(corpus, articles, franchises, existing, existing, ["movie:1"],
                                     workers=1, follows={})
            with open(existing, encoding="utf-8") as fh:
                out = json.load(fh)
        self.assertEqual((out["anchors"]["movie:1"], result["emptyAnswers"]), ([], 1))

    def test_a_backfilled_answer_fills_a_title_with_no_picks_and_nothing_else(self):
        picks = {"picks": [{"title": "Heat", "year": 1995, "type": "film"}]}
        with tempfile.TemporaryDirectory() as directory:
            corpus, articles, franchises, existing = self.fixture(directory, {"movie:2": [], "movie:1": ["movie:2"]})
            result = fp.daily_update(corpus, articles, franchises, existing, existing, [], follows={},
                                     backfill={"movie:2": picks, "movie:1": picks, "movie:99": picks})
            with open(existing, encoding="utf-8") as fh:
                out = json.load(fh)
        self.assertEqual(out["anchors"], {"movie:1": ["movie:2"], "movie:2": ["movie:8"]})
        self.assertEqual((result["backfilled"], result["asked"], result["costUSD"]), (1, 0, 0))

    def test_a_reask_replaces_the_picks_only_when_it_knows_the_title_or_matches_more(self):
        heat = {"title": "Heat", "year": 1995, "type": "film"}
        samurai = {"title": "Seven Samurai", "year": 1954, "type": "film"}
        with tempfile.TemporaryDirectory() as directory:
            corpus, articles, franchises, existing = self.fixture(
                directory, {"movie:1": ["movie:8"], "movie:2": ["movie:8"], "movie:9": ["movie:2", "movie:8"]})
            result = fp.daily_update(corpus, articles, franchises, existing, existing, [], follows={}, reasks={
                "movie:1": {"known": True, "picks": [samurai]},             # known now: replaced
                "movie:2": {"known": False, "picks": [heat, {"title": "Amélie", "year": 2001, "type": "film"}]},
                "movie:9": {"known": False, "picks": [samurai]}})           # fewer, still unknown: kept
            with open(existing, encoding="utf-8") as fh:
                out = json.load(fh)["anchors"]
        self.assertEqual(out, {"movie:1": ["movie:2"], "movie:2": ["movie:8", "movie:1"],
                               "movie:9": ["movie:2", "movie:8"]})
        self.assertEqual((result["reasked"], result["reaskReplaced"]), (3, 2))

    def test_withdrawn_anchors_and_picks_are_removed_before_the_store_build(self):
        with tempfile.TemporaryDirectory() as directory:
            corpus, articles, franchises, existing = self.fixture(
                directory, {"movie:2": ["movie:8", "movie:99"], "movie:99": ["movie:2"]})
            result = fp.daily_update(corpus, articles, franchises, existing, existing, [], follows={})
            with open(existing, encoding="utf-8") as fh:
                out = json.load(fh)
        self.assertEqual(out["anchors"], {"movie:2": ["movie:8"]})
        self.assertEqual((result["asked"], result["anchors"], result["picks"]), (0, 1, 1))


class Reask(unittest.TestCase):
    def test_a_title_the_model_did_not_know_is_asked_again_three_and_six_months_after_the_ask(self):
        self.assertEqual(fp.reask_dates("2026-09-25T10:00:00+00:00", False, "1999-05-01"),
                         ["2026-12-25", "2027-03-26"])

    def test_a_new_release_is_asked_again_three_and_six_months_after_it_came_out(self):
        self.assertEqual(fp.reask_dates("2026-09-25", True, "2026-08-01"), ["2026-10-31", "2027-01-30"])
        self.assertEqual(fp.reask_dates("2026-09-25", True, "2026-05-01"), ["2026-10-30"],
                         "a date before the first ask is not asked")
        self.assertEqual(fp.reask_dates("2026-09-25", True, "2026-12-01"), ["2027-03-02", "2027-06-01"],
                         "not released yet when asked")

    def test_a_known_older_title_is_not_asked_again(self):
        self.assertEqual(fp.reask_dates("2026-09-25", True, "2020-01-01"), [])
        self.assertEqual(fp.reask_dates("2026-09-25", True, None), [])

    def test_the_committed_seed_is_readable_and_every_entry_has_a_date(self):
        for key, entry in fp.load_reask().items():
            self.assertTrue(fp.reask_dates(entry["askedAt"], entry["known"], entry["released"]), key)


class Frozen(fp.datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return fp.datetime.datetime(2026, 10, 1, 12, 0, tzinfo=fp.datetime.timezone.utc)


FROZEN = types.SimpleNamespace(datetime=Frozen, timezone=fp.datetime.timezone, date=fp.datetime.date,
                               timedelta=fp.datetime.timedelta)


def gemini(text, prompt=351, out=600, thoughts=40, finish="STOP"):
    return {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": finish}],
            "usageMetadata": {"promptTokenCount": prompt, "candidatesTokenCount": out,
                              "thoughtsTokenCount": thoughts, "totalTokenCount": prompt + out + thoughts},
            "modelVersion": "gemini-3.7-flash-001"}


class Wire:
    """Gemini over a stubbed `urlopen`: canned answers by the title in the prompt, a Files API and a Batch
    API, and a record of every request body sent. Below every layer the tool talks through, so the same
    scenario runs against any implementation of the asking."""

    ANSWERS = {
        "Amélie": [gemini('{"known": true, "picks": [{"title": "Seven Samurai", "year": 1954, "type": "film"},'
                       ' {"title": "Heat", "year": 1995, "type": "film"}, {"title": "Nope", "year": 1, '
                       '"type": "film"}]}')],
        "Seven Samurai": [503, gemini('{"known": false, "picks": [{"title": "Amélie", "year": 2001, '
                                      '"type": "film"}]}', thoughts=0)],
        "Heat": [gemini("", out=0)],
        "L.A. Takedown": [gemini("{not json", finish="MAX_TOKENS")],
    }

    def __init__(self):
        self.sent = []
        self.queues = {title: list(answers) for title, answers in self.ANSWERS.items()}
        self.uploads = {}
        self.jobs = {}

    def answer(self, body, batch=False):
        text = body["contents"][0]["parts"][0]["text"]
        title = re.match(r"A friend loved (.+?) \(", text).group(1)
        reply = self.queues[title].pop(0)
        while batch and isinstance(reply, int):
            reply = self.queues[title].pop(0)
        return reply

    def __call__(self, request, timeout=None):
        url, data = request.full_url, request.data
        self.sent.append((request.get_method(), url, data))
        headers = {}
        if url.endswith(":generateContent"):
            reply = self.answer(json.loads(data))
            if isinstance(reply, int):
                raise urllib.error.HTTPError(url, reply, "busy", {}, io.BytesIO(b"busy"))
        elif url.endswith("/upload/v1beta/files"):
            headers, reply = {"X-Goog-Upload-URL": "https://upload.test/1"}, {}
        elif url == "https://upload.test/1":
            self.uploads["files/1"] = data
            reply = {"file": {"name": "files/1"}}
        elif url.endswith(":batchGenerateContent"):
            name = f"batches/{len(self.jobs) + 1}"
            self.jobs[name] = json.loads(data)["batch"]["inputConfig"]["fileName"]
            reply = {"name": name}
        elif "/download/" in url:
            lines = [json.loads(line) for line in self.uploads["files/1"].decode().splitlines()]
            reply = "".join(json.dumps({"key": line["key"], "response": self.answer(line["request"], True)})
                            + "\n" for line in lines).encode()
        elif "/v1beta/batches/" in url:
            reply = {"metadata": {"state": "BATCH_STATE_SUCCEEDED", "output": {"responsesFile": "files/out"}}}
        else:
            raise AssertionError(url)
        return Reply(reply, headers)


class Reply:
    def __init__(self, value, headers):
        self.value, self.headers = value, headers

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.value if isinstance(self.value, bytes) else json.dumps(self.value).encode()


def digest_of(path):
    with open(path, "rb") as fh:
        return fp.hashlib.sha256(fh.read()).hexdigest()


class Port(unittest.TestCase):
    """What the tool sends and writes, pinned byte for byte (oxyc/den-dataset#183, step 2).

    The digests below were taken from `tools/fan_picks.py` before it asked through `lib/llm.py`, and the
    port reproduced them: the same request bodies on the wire and the same `fan-picks.json`, through the
    daily path, the full run online and the full run's Batch API. They now pin what it sends and writes, so
    a change to either is a deliberate re-pin (see below)."""

    def wired(self):
        wire = Wire()
        for patch in (mock.patch("urllib.request.urlopen", wire), mock.patch("time.sleep"),
                      mock.patch.object(fp, "datetime", FROZEN),
                      mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"})):
            patch.start()
            self.addCleanup(patch.stop)
        for name in ("lib.llm", "lib.llm_providers"):
            module = sys.modules.get(name)
            if module is not None and hasattr(module, "datetime"):
                patch = mock.patch.object(module, "datetime", FROZEN)
                patch.start()
                self.addCleanup(patch.stop)
        return wire

    def sent_digest(self, wire):
        return fp.hashlib.sha256(json.dumps(
            [(method, url, (data or b"").decode()) for method, url, data in wire.sent]).encode()).hexdigest()

    def test_the_daily_path(self):
        wire = self.wired()
        directory = self.enterContext(tempfile.TemporaryDirectory())
        corpus, articles, franchises, existing = Daily().fixture(directory, {"movie:99": []})
        result = fp.daily_update(corpus, articles, franchises, existing, existing,
                                 ["movie:1", "movie:2", "movie:8", "movie:9"], workers=1, follows={},
                                 generate=fp.generate_online)
        self.assertEqual((result["answered"], result["emptyAnswers"], result["parseErrors"]), (2, 1, 1))
        self.assertEqual((digest_of(existing), self.sent_digest(wire)), DAILY_DIGESTS)

    def full_run(self, online):
        wire = self.wired()
        directory = self.enterContext(tempfile.TemporaryDirectory())
        corpus, articles, franchises, _existing = Daily().fixture(directory)
        work, pool = os.path.join(directory, "work"), os.path.join(directory, "pool.json.gz")
        with gzip.open(corpus, "rb") as fh:
            rows = fh.read()
        # The manifest pins the input files' digests, so their gzip headers must not carry a time.
        for path, body in ((corpus, rows), (pool, b'{"popularity": {}}')):
            with open(path, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as fh:
                fh.write(body)
        follows = os.path.join(directory, "follows.json")
        with open(follows, "w") as fh:
            json.dump({}, fh)
        fp.prepare(corpus, articles, pool, work, 1.0)
        log = io.StringIO()
        fp.run(work, online=online, workers=1, label="all", log=log)
        if not online:
            fp.collect(fp.Work(work), log=log)
        fp.match(work, corpus, franchises, follows)
        out = os.path.join(work, "fan-picks.json")
        fp.export(work, out)
        return digest_of(out), self.sent_digest(wire)

    def test_the_full_run_online(self):
        self.assertEqual(self.full_run(online=True), ONLINE_DIGESTS)

    def test_the_full_run_through_the_batch_api(self):
        self.assertEqual(self.full_run(online=False), BATCH_DIGESTS)


#: `(fan-picks.json, every request on the wire)`. Taken from origin/main at 2af7781 before the port, and
#: re-taken when the prompt asked for the compact format (#187): that moved every request body and, through
#: the prompt digest the full run's manifest pins, its export — the daily path's fan-picks.json did not move.
DAILY_DIGESTS = ("b6db409b24ecf74d8ecf046abaf464743b1e86809c2de920c678ffac48718eb3",
                 "d3a528f27672870a98eac339b07658dad80695c430d4c8c9c61b3b487d00c0aa")
ONLINE_DIGESTS = ("e28463221509814b94d46a0fed99b209c5e79ca736d62163ed9eef3ef8e5bcb6",
                  "d3a528f27672870a98eac339b07658dad80695c430d4c8c9c61b3b487d00c0aa")
BATCH_DIGESTS = ("969ab80a6005f135417b30cee65d6b8e8abd01d061d11adc90e8b8a3bee10b06",
                 "a797283a97a8190fc030825e3fe331e00786225d33c7b24731fdf0db2fde44be")


if __name__ == "__main__":
    unittest.main()
