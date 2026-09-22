"""The genres & moods stage against a fixture out-dir, with Jev stubbed.

No network: the provider is a stand-in that answers the questions the stage sends, so what is tested is
which titles are asked, what the state carries, that a second run buys nothing, what the rule derives, and
that the quality floors decide whether anything is written. The vocabulary, the definitions and the rule
are the committed ones, so a change to any of them reaches these tests.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import pipeline

from . import artifacts, genres_moods
from .contract import Context, StageError

sys.path.insert(0, os.path.join(pipeline.contract.REPO, "scripts"))
import genres_moods_merge as gmm  # noqa: E402
from article_sections import parse_sections  # noqa: E402

TEXT = "{t} is a film.\n\n== Plot ==\nA thief plans one last job.\n\n== Reception ==\nCritics were kind.\n"
#: What the stand-in answers: everything else is 0.05, under every threshold in the rule.
STRONG = {"Heist": 0.95, "Tense/Edge-of-seat": 0.9}


def entry(primary, subgenres=(), moods=(), source="july-relabel", animated=False):
    return {"animated": animated, "moods": [{"confidence": c, "label": l} for l, c in moods],
            "primaryGenre": primary, "primaryGenreSource": source, "source": source,
            "subgenres": [{"confidence": c, "label": l} for l, c in subgenres]}


class Jev:
    """The provider, answering from `STRONG` and a fixed primary genre. Records every state it was sent."""

    sent = []
    primary = "Crime"
    labels = STRONG
    #: The planner prices a run off the client class, so the stand-in carries the real rate.
    RATE_PER_INPUT_TOKEN = 0.042 / 1_000_000

    def __init__(self, model=None):
        self.model = model
        self.calls = self.input_tokens = self.output_tokens = 0
        self.spend = 0.0

    def ask_with_metadata(self, state, questions):
        type(self).sent.append(state)
        self.calls += 1
        mapping = genres_moods.questions()[1]
        answers = {}
        for qid, question in questions.items():
            if question["type"] == "noul":
                answers[qid] = {"type": "noul", "noul": type(self).labels.get(mapping[qid]["label"], 0.05)}
                continue
            criteria = list(question["criteria"])
            choice = type(self).primary if qid == "gm__primary_genre" else "none-fits"
            rest = (1 - 0.9) / (len(criteria) - 1)
            answers[qid] = {"type": "choice", "choice": choice, "confidence": 0.9,
                            "probabilities": {c: (0.9 if c == choice else rest) for c in criteria}}
        return answers, {"model": self.model, "usage": {"input_tokens": 7000, "output_tokens": 2000}}

    def summary(self):
        return "stub"


class Fixture(unittest.TestCase):
    """An out-dir with an article dump, one classify shard and one enrichment batch; a curated file of 30
    gated titles plus the ones under test; a golden set over them; floors recorded on the curated file.

    movie:1 is labelled, movie:2 is curated with neither subgenres nor moods, movie:3 is new (and animated
    per TMDB), movie:4 is about another work, movie:5 has no article, movie:6's article changed since
    classify, movie:7 is new with no enrichment row, tv:8 is new.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.out = os.path.join(self.dir, "out")
        os.makedirs(os.path.join(self.out, "enriched"))
        self.keys = ["movie:1", "movie:2", "movie:3", "movie:4", "movie:5", "movie:6", "movie:7", "tv:8"]
        self.write_classify()
        with open(os.path.join(self.out, "articles.jsonl"), "w", encoding="utf-8") as fh:
            for key in self.keys:
                if key == "movie:5":
                    continue
                media, tmdb_id = key.split(":")
                text = TEXT.format(t=f"T{tmdb_id}")
                if key == "movie:6":
                    text = text.replace("last job", "final job")
                fh.write(json.dumps({"mediaType": media, "tmdbId": int(tmdb_id), "title": f"T{tmdb_id}",
                                     "article": f"T {tmdb_id}", "language": "en", "year": 1990,
                                     "plotSections": ["Plot"], "text": text}) + "\n")
        with open(os.path.join(self.out, "enriched", "batch-1.json"), "w", encoding="utf-8") as fh:
            json.dump([{"mediaType": "movie", "tmdbId": 2, "genreIDs": []},
                       {"mediaType": "movie", "tmdbId": 3, "genreIDs": [16]},
                       {"mediaType": "movie", "tmdbId": 9, "genreIDs": []},
                       {"mediaType": "tv", "tmdbId": 8, "genreIDs": [35, 18]}], fh)

        # Two labels per family over 30 gated titles, so that both clear the eval's 10-positive support
        # floor: a label the golden set has fewer than ten of is dropped before scoring, and a wrong
        # prediction of one could not move the gate either way.
        gated = {f"movie:{100 + i}": ("Heist", "Tense/Edge-of-seat") if i < 15 else ("Prison", "Feel-good")
                 for i in range(30)}
        titles = {"movie:1": entry("Drama", [("Prison", 0.7)]), "movie:2": entry("Crime")}
        titles.update({k: entry("Crime", [(sub, 0.9)], [(mood, 0.9)]) for k, (sub, mood) in gated.items()})
        head = {"_": "fixture", "count": len(titles), "sources": {"july-relabel": "July."},
                "taxonomyVersion": "t02"}
        self.curated = os.path.join(self.dir, "curated.json")
        with open(self.curated, "w", encoding="utf-8") as fh:
            fh.write(gmm.dump_curated(head, titles))
        self.golden = os.path.join(self.dir, "golden.json")
        golden = {**gated, "movie:2": ("Heist", "Tense/Edge-of-seat")}
        with open(self.golden, "w", encoding="utf-8") as fh:
            json.dump({"taxonomyVersion": "t02", "titles": [
                {"mediaType": "movie", "tmdbId": int(k.split(":")[1]), "primaryGenre": "Crime",
                 "subgenres": [sub], "moods": [mood]} for k, (sub, mood) in golden.items()]}, fh)
        self.floors = os.path.join(self.dir, "floors.json")
        recorded = gmm.run_eval(self.curated, self.golden, self.floors, "--record")
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        for name, value in (("CURATED", self.curated), ("GOLDEN", self.golden), ("FLOORS", self.floors)):
            patched = mock.patch.object(genres_moods, name, value)
            patched.start()
            self.addCleanup(patched.stop)
        Jev.sent = []
        Jev.primary = "Crime"
        Jev.labels = STRONG

    def write_classify(self, invalid=("movie:4",)):
        with open(os.path.join(self.out, "combined-v1-r2.jsonl"), "w", encoding="utf-8") as fh:
            for key in self.keys:
                media, tmdb_id = key.split(":")
                sections = []
                for section in parse_sections(TEXT.format(t=f"T{tmdb_id}")):
                    role = {"Lead": "work-context", "Plot": "story-premise"}.get(
                        section["heading"], "irrelevant-production-reception-navigation")
                    sections.append({"id": section["id"], "heading": section["heading"],
                                     "textSha256": section["textSha256"],
                                     "role": {"probabilities": {role: 1.0}}})
                validity = "other-screen-work" if key in invalid else "correct-screen-work"
                fh.write(json.dumps({"mediaType": media, "tmdbId": int(tmdb_id), "title": f"T{tmdb_id}",
                                     "year": 1990, "sections": sections,
                                     "answers": {"validity": {"choice": validity}}}) + "\n")

    def context(self, **kw):
        return Context(out_dir=self.out, **kw)

    def run_stage(self, **kw):
        with mock.patch.object(genres_moods.rc, "TypeSafe", Jev):
            return genres_moods.run(self.context(**kw))

    def derived(self):
        with open(os.path.join(self.out, "genres-moods.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def shards(self):
        return sorted(os.path.basename(p) for p in
                      self.context().paths(artifacts.GENRES_MOODS_ANSWERS))


class Ask(Fixture):
    def test_it_asks_the_titles_the_curated_file_cannot_answer_and_says_why_it_skipped_the_rest(self):
        made = self.run_stage(spend=True)
        self.assertEqual(len(Jev.sent), 3, "one call per asked title")
        self.assertEqual(sorted(self.answers(), key=genres_moods.gm.sort_key),
                         ["movie:2", "movie:3", "tv:8"])
        self.assertIn("asked 3", made)

    def test_the_state_is_the_lead_and_the_premise_sections_only(self):
        self.run_stage(spend=True)
        for state in Jev.sent:
            sections = state["article"]["sections"]
            self.assertEqual([s["heading"] for s in sections.values()], ["Lead", "Plot"],
                             "Reception is not premise text")

    def test_a_second_run_buys_nothing(self):
        self.run_stage(spend=True)
        bought = len(Jev.sent)
        made = self.run_stage(spend=True)
        self.assertEqual(len(Jev.sent), bought, "the answered titles were asked again")
        self.assertIn("asked 0", made)

    def test_the_shard_is_named_by_the_article_dump_and_carries_its_manifest(self):
        self.run_stage(spend=True)
        shard, = self.shards()
        digest = genres_moods.rc.sha256_file(os.path.join(self.out, "articles.jsonl"))
        self.assertEqual(shard, f"genres-moods-v1-{digest[:12]}.jsonl")
        with open(os.path.join(self.out, shard + ".manifest.json"), encoding="utf-8") as fh:
            config = json.load(fh)["config"]
        self.assertEqual(config["requestedModel"], "jev-1.13.0")
        self.assertEqual(config["globalQuestionsSha256"], genres_moods.rc.sha256_text(
            genres_moods.rc.canonical(genres_moods.questions()[0])))
        self.assertIn("stateSectionIdsSha256", config, "which sections were sent is part of the provenance")

    def test_a_rebuilt_article_dump_starts_its_own_shard_and_asks_only_what_is_new(self):
        """The manifest hashes the dump, so a rebuilt one cannot resume the old shard. A new shard beside
        it can, and a title already answered is not in the new one's selection."""
        self.run_stage(spend=True)
        with open(os.path.join(self.out, "articles.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"mediaType": "movie", "tmdbId": 9, "title": "T9", "article": "T 9",
                                 "language": "en", "year": 1991, "plotSections": ["Plot"],
                                 "text": TEXT.format(t="T9")}) + "\n")
        self.keys = self.keys + ["movie:9"]
        self.write_classify()
        self.run_stage(spend=True)
        self.assertEqual(len(self.shards()), 2)
        self.assertEqual(sorted(self.answers())[-1], "tv:8")
        self.assertIn("movie:9", self.answers())
        self.assertEqual(len(Jev.sent), 4, "only the new title was bought a second time")

    def test_without_spend_nothing_is_asked_and_the_derive_still_runs(self):
        made = genres_moods.run(self.context())
        self.assertEqual(Jev.sent, [])
        self.assertIn("asked 0", made)
        self.assertTrue(os.path.exists(os.path.join(self.out, "genres-moods.json")))

    def test_plan_writes_nothing(self):
        made = self.run_stage(plan=True)
        self.assertEqual(Jev.sent, [])
        self.assertEqual(self.shards(), [])
        self.assertFalse(os.path.exists(os.path.join(self.out, "genres-moods.json")))
        self.assertIn("3 titles to ask", made)

    def answers(self):
        answers, _ = genres_moods.read_answers(self.context().paths(artifacts.GENRES_MOODS_ANSWERS))
        return set(answers)


class Derive(Fixture):
    def setUp(self):
        super().setUp()
        self.run_stage(spend=True)

    def test_a_new_title_is_derived_with_its_tmdb_animation_flag(self):
        titles = self.derived()["titles"]
        self.assertEqual(titles["movie:3"], {
            "animated": True, "primaryGenre": "Crime", "primaryGenreSource": "jev-v3", "source": "jev-v3",
            "subgenres": [{"confidence": 0.95, "label": "Heist"}],
            "moods": [{"confidence": 0.9, "label": "Tense/Edge-of-seat"}]})
        self.assertFalse(titles["tv:8"]["animated"])

    def test_a_curated_title_with_neither_subgenres_nor_moods_is_filled_and_keeps_its_primary_genre(self):
        entry = self.derived()["titles"]["movie:2"]
        self.assertEqual((entry["primaryGenre"], entry["primaryGenreSource"]), ("Crime", "july-relabel"))
        self.assertEqual([x["label"] for x in entry["subgenres"]], ["Heist"])
        self.assertEqual([x["label"] for x in entry["moods"]], ["Tense/Edge-of-seat"])
        self.assertEqual(entry["source"], "jev-v3")

    def test_a_curated_title_that_has_labels_is_never_asked_and_never_changed(self):
        with open(self.curated, encoding="utf-8") as fh:
            before = json.load(fh)["titles"]["movie:1"]
        self.assertEqual(self.derived()["titles"]["movie:1"], before)

    def test_a_hand_enrichment_of_an_answered_title_overrides_the_derived_labels(self):
        """The hand enrichment writes into the curated file, and the curated file wins: that is the whole
        way a person overrides the automated labeller (oxyc/den-dataset#56)."""
        head, titles = genres_moods.gm.read_curated(self.curated)
        titles["movie:3"] = entry("Documentary", [("Biopic", 0.8)], [("Cozy", 0.7)],
                                  source="enrichment-2026-09-22-sonnet", animated=True)
        with open(self.curated, "w", encoding="utf-8") as fh:
            fh.write(gmm.dump_curated(head, titles))
        made = genres_moods.run(self.context())
        self.assertEqual(self.derived()["titles"]["movie:3"], titles["movie:3"])
        self.assertIn("kept 1", made)

    def test_a_title_classify_says_is_about_another_work_gets_no_genres_or_moods(self):
        """Answered yesterday, judged a different work today: the answer stays on disk and is not derived."""
        self.write_classify(invalid=("movie:4", "movie:3"))
        made = genres_moods.run(self.context())
        self.assertNotIn("movie:3", self.derived()["titles"])
        self.assertIn("not about the requested work 1", made)

    def test_the_output_records_the_rule_and_the_shards_it_was_derived_from(self):
        head = self.derived()
        derivation = head["derivation"]
        self.assertEqual(derivation["rule"], "data/genres-moods-rule.json")
        self.assertEqual(derivation["ruleSha256"], genres_moods.rc.sha256_file(genres_moods.RULE))
        self.assertEqual([shard["rows"] for shard in derivation["answers"]], [3])
        self.assertEqual(head["count"], len(head["titles"]))
        self.assertIn("jev-v3", head["sources"])

    def test_it_is_rebuilt_from_the_answers_rather_than_read_back(self):
        """The file is an output, never an input: deleting it and running again reproduces it."""
        before = self.derived()
        os.unlink(os.path.join(self.out, "genres-moods.json"))
        genres_moods.run(self.context())
        self.assertEqual(self.derived(), before)


class Gate(Fixture):
    def test_a_result_below_the_quality_floors_is_refused_and_nothing_is_written(self):
        Jev.labels = {"Prison": 0.95, "Cozy": 0.95}
        with self.assertRaises(StageError) as refused:
            self.run_stage(spend=True)
        self.assertIn("below the quality floors", str(refused.exception))
        self.assertIn("subgenre microF1", str(refused.exception))
        self.assertFalse(os.path.exists(os.path.join(self.out, "genres-moods.json")))
        self.assertEqual(len(self.shards()), 1, "what was paid for is kept; only the derived file is refused")

    def test_the_answers_a_refused_derive_bought_are_derived_once_the_rule_fits(self):
        Jev.labels = {"Prison": 0.95, "Cozy": 0.95}
        with self.assertRaises(StageError):
            self.run_stage(spend=True)
        # Lowering a floor is the deliberate, committed step the ratchet asks for; here it stands for one.
        with open(self.floors, encoding="utf-8") as fh:
            floors = json.load(fh)
        floors["floors"]["subgenre"]["microF1"] = 0.0
        floors["floors"]["subgenre"]["macroF1"] = 0.0
        floors["floors"]["mood"]["microF1"] = 0.0
        floors["floors"]["mood"]["macroF1"] = 0.0
        with open(self.floors, "w", encoding="utf-8") as fh:
            json.dump(floors, fh)
        genres_moods.run(self.context())
        self.assertEqual([x["label"] for x in self.derived()["titles"]["movie:3"]["subgenres"]], ["Prison"])


class Rule(unittest.TestCase):
    """The derivation itself, on hand-written answers."""

    def answers(self, subgenres, moods, picks=("none-fits", "none-fits"), primary="Crime"):
        _, mapping, _ = genres_moods.questions()
        out = {"gm__primary_genre": {"choice": primary},
               "gm__pick__subgenre": {"choice": picks[0], "probabilities": {picks[0]: 0.9}},
               "gm__pick__mood": {"choice": picks[1], "probabilities": {picks[1]: 0.9}}}
        for qid, entry in mapping.items():
            table = subgenres if entry["family"] == "subgenre" else moods
            out[qid] = {"noul": table.get(entry["label"], 0.0)}
        return out, mapping

    def rule(self):
        return genres_moods.load_rule(genres_moods.RULE, genres_moods.gm.vocabulary())

    def test_each_label_clears_its_own_threshold_not_a_shared_one(self):
        """Police Procedural is kept at 0.6 and Historical/Period Drama dropped at 0.85: the per-label
        thresholds fitted on golden half A are what the file holds."""
        answers, mapping = self.answers({"Police Procedural": 0.6, "Historical/Period Drama": 0.85}, {})
        record = genres_moods.derive_record(answers, mapping, self.rule())
        self.assertEqual([x["label"] for x in record["subgenres"]], ["Police Procedural"])

    def test_it_keeps_the_three_strongest_and_the_confidence_is_the_probability(self):
        answers, mapping = self.answers(
            {"Heist": 0.95, "Prison": 0.99, "Neo-Noir": 0.9, "Crime Thriller": 0.85}, {})
        record = genres_moods.derive_record(answers, mapping, self.rule())
        self.assertEqual([(x["label"], x["confidence"]) for x in record["subgenres"]],
                         [("Prison", 0.99), ("Heist", 0.95), ("Neo-Noir", 0.9)])

    def test_the_most_defining_pick_leads_even_when_its_noul_did_not_clear(self):
        answers, mapping = self.answers({"Heist": 0.95}, {}, picks=("Biopic", "none-fits"))
        record = genres_moods.derive_record(answers, mapping, self.rule())
        self.assertEqual([x["label"] for x in record["subgenres"]], ["Biopic", "Heist"])

    def test_a_pick_below_the_rules_confidence_is_not_added(self):
        answers, mapping = self.answers({}, {}, picks=("Biopic", "none-fits"))
        answers["gm__pick__subgenre"]["probabilities"]["Biopic"] = 0.4
        record = genres_moods.derive_record(answers, mapping, self.rule())
        self.assertEqual(record["subgenres"], [])

    def test_a_rule_naming_an_unknown_label_or_another_type_is_refused(self):
        vocab = genres_moods.gm.vocabulary()
        with tempfile.TemporaryDirectory() as dir:
            for broken, expected in (({"subgenre": {"type": "flat", "t": {}, "default": 0.8, "cap": 3},
                                       "mood": {"type": "perlabel", "t": {}, "default": 0.8, "cap": 3}},
                                      "only per-label thresholds"),
                                     ({"subgenre": {"type": "perlabel", "t": {"Nope": 0.5}, "default": 0.8,
                                                    "cap": 3},
                                       "mood": {"type": "perlabel", "t": {}, "default": 0.8, "cap": 3}},
                                      "outside the taxonomy")):
                path = os.path.join(dir, "rule.json")
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(broken, fh)
                with self.assertRaises(StageError) as refused:
                    genres_moods.load_rule(path, vocab)
                self.assertIn(expected, str(refused.exception))


class Questions(unittest.TestCase):
    def test_they_are_the_set_the_rule_was_fitted_on(self):
        """The thresholds in `data/genres-moods-rule.json` were fitted on the answers to exactly these 78
        questions (oxyc/den-dataset#56). A reworded definition makes the rule someone else's."""
        qs, mapping, _ = genres_moods.questions()
        self.assertEqual(len(qs), 78)
        self.assertEqual(len(mapping), 75)
        self.assertEqual(genres_moods.rc.sha256_text(genres_moods.rc.canonical(qs)),
                         "9a6f1253c49cfe187646460ea1e0e6db9434621647f7cf37926cda0fdbbdd399")


if __name__ == "__main__":
    unittest.main()
