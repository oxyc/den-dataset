import gzip
import importlib.util
import json
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


card = _load("more_like_card")
gate_test = _load("more_like_gate_test")
gate = gate_test.gate


def _facets(choice, probability):
    return {"choice": choice, "probabilities": {choice: probability, "does-not-apply": 1 - probability},
            "confidence": probability}


class MoreLikeCardTest(unittest.TestCase):
    def setUp(self):
        gate_test.MoreLikeGateTest.setUp(self)
        self.story = self.work
        gate.prepare(self.ruler, self.articles, self.story, sample_size=4, minimum_population=6)
        keys = [f"movie:{tmdb}" for tmdb in range(1, 19)]
        self.tags = os.path.join(self.temp.name, "tags.json")
        self.labels = os.path.join(self.temp.name, "genres-moods.json")
        self.corpus = os.path.join(self.temp.name, "corpus.jsonl.gz")
        with open(self.tags, "w", encoding="utf-8") as fh:
            json.dump({"tags": {key: [f"tag-{key}"] for key in keys}}, fh)
        with open(self.labels, "w", encoding="utf-8") as fh:
            json.dump({"titles": {key: {"primaryGenre": "Drama", "animated": False,
                                        "subgenres": [{"label": "Heist", "confidence": 0.8}],
                                        "moods": [{"label": "Tense", "confidence": 0.7}]} for key in keys}}, fh)
        applicability = {"validity": {"choice": "correct-screen-work",
                                      "probabilities": {"correct-screen-work": 1.0}},
                         "narrative_applicability": {"choice": "bounded-fictional-narrative"}}
        with gzip.open(self.corpus, "wt", encoding="utf-8") as fh:
            for key in keys:
                fh.write(json.dumps({"key": key, "applicability": applicability, "facets": {
                    "era": _facets("contemporary", 0.9), "pacing": _facets("brisk", 0.55)}}) + "\n")
        self.card_work = os.path.join(self.temp.name, "card")

    def tearDown(self):
        gate_test.MoreLikeGateTest.tearDown(self)

    def prepare(self):
        return card.prepare(self.story, self.articles, self.tags, self.labels, self.corpus, self.card_work)

    def test_card_arm_keeps_story_pairs_blinding_and_questions(self):
        registration = self.prepare()
        self.assertEqual(registration, self.prepare(), "a second prepare must reproduce the preregistration")
        _, story_rows = gate.load_plan(self.story)
        _, card_rows = gate.load_plan(self.card_work)
        self.assertEqual([(r["pairId"], r["positiveLabel"]) for r in card_rows],
                         [(r["pairId"], r["positiveLabel"]) for r in story_rows])
        anchor = card_rows[0]["state"]["works"]["anchor"]["card"]
        # Only the gate-published facet reaches the card; the uncertain one is withheld as the store does.
        self.assertEqual(anchor["plotFacets"], {"era": "contemporary"})
        self.assertTrue(anchor["articleLead"].startswith("## Lead\nLead "))
        self.assertNotIn("Story", anchor["articleLead"])
        self.assertEqual(anchor["genresMoods"]["subgenres"], ["Heist"])

    def test_compare_pairs_card_with_story(self):
        self.prepare()
        story_answers = os.path.join(self.temp.name, "story.jsonl")
        card_answers = os.path.join(self.temp.name, "card.jsonl")
        gate.run(self.story, story_answers, spend=True, client=gate_test.FakeJev())
        gate.run(self.card_work, card_answers, spend=True, client=gate_test.FakeJev())
        report = card.compare(self.card_work, card_answers, self.story, story_answers)
        self.assertEqual(report["pairs"], 4)
        self.assertEqual(report["cardMinusStory"], 0.0)
        self.assertEqual(report["inputTokensPerCall"], {"card": 100, "story": 100})

    def test_refuses_an_article_the_story_arm_did_not_send(self):
        _, story_rows = gate.load_plan(self.story)
        sent = story_rows[0]["state"]["works"]["anchor"]["key"]
        rows = list(gate.read_jsonl(self.articles))
        for row in rows:
            if f"movie:{row['tmdbId']}" == sent:
                row["text"] = "A different revision."
        with open(self.articles, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        with self.assertRaisesRegex(ValueError, "differs from the one the story arm sent"):
            self.prepare()


if __name__ == "__main__":
    unittest.main()
