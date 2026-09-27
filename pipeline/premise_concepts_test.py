import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from contextlib import redirect_stdout
import io

from pipeline import canonicalize_premise_tags as cli
from pipeline import premise_concepts as pc


class PremiseConceptsTest(unittest.TestCase):
    def test_committed_artifact_is_complete_and_matches_its_source(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        tags = os.path.join(repo, "data", "premise-tags-v2.json")
        artifact_path = os.path.join(repo, "data", "premise-concepts-v1.json")
        with open(artifact_path, encoding="utf-8") as fh:
            artifact = json.load(fh)
        forms = pc.surface_forms(tags)
        counts = {item["form"]: item["count"] for item in forms}

        self.assertEqual(artifact["schema"], pc.SCHEMA)
        self.assertEqual(artifact["sourceSha256"], pc.source_digest(tags))
        self.assertEqual(artifact["surfaceForms"], len(forms))
        self.assertEqual(set(artifact["map"]), set(counts))
        self.assertTrue(set(artifact["map"].values()) <= set(counts))

        concept_occurrences = {}
        for surface, count in counts.items():
            concept = artifact["map"][surface]
            concept_occurrences[concept] = concept_occurrences.get(concept, 0) + count
        self.assertEqual(artifact["conceptOccurrences"], concept_occurrences)
        self.assertEqual(artifact["concepts"], len(concept_occurrences))

        with open(tags, encoding="utf-8") as fh:
            title_tags = json.load(fh)["tags"]
        concept_titles = {}
        for values in title_tags.values():
            for concept in {artifact["map"][surface] for surface in values}:
                concept_titles[concept] = concept_titles.get(concept, 0) + 1
        self.assertEqual(artifact["conceptTitles"], concept_titles)

    def test_surface_forms_count_and_validate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "tags.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"tags": {"movie:1": ["love-triangle", "heist"],
                                     "tv:2": ["love-triangle"]}}, fh)
            self.assertEqual(pc.surface_forms(path), [{"form": "heist", "count": 1},
                                                      {"form": "love-triangle", "count": 2}])
            self.assertEqual(pc.surface_forms(path, 2), [{"form": "love-triangle", "count": 2}])

    def test_candidates_are_exactly_scored_and_only_point_upstream(self):
        forms = [{"form": "family-love-triangle", "count": 1},
                 {"form": "love-triangle", "count": 5},
                 {"form": "unrelated", "count": 3}]
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "vectors.i8")
            pc.append_vectors(path, [[10, 10, 10, 10], [11, 11, 11, 11], [-10, 10, -10, 10]], 4)
            rows = list(pc.candidate_rows(forms, path, 4, min_count=2, source_min_count=1, threshold=.95,
                                          tables=16, bits=2, probe=3, options=2))
        row = next(row for row in rows if row["form"] == "family-love-triangle")
        self.assertEqual(row["candidates"][0]["concept"], "love-triangle")
        self.assertFalse(any(row["form"] == "love-triangle" and
                             any(c["concept"] == "family-love-triangle" for c in row["candidates"])
                             for row in rows), "a frequent concept cannot point down to its singleton")

    def test_candidates_skip_the_singleton_tail_by_default(self):
        forms = [{"form": "family-love-triangle", "count": 1},
                 {"form": "love-triangle", "count": 5}]
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "vectors.i8")
            pc.append_vectors(path, [[10, 10], [11, 11]], 2)
            rows = list(pc.candidate_rows(forms, path, 2, threshold=.95,
                                          tables=2, bits=1, probe=2, options=1))
        self.assertEqual(rows, [])

    def test_choice_is_bounded_and_other_keeps_the_surface(self):
        candidates = [{"concept": "love-triangle", "count": 5, "cosine": .9}]
        question = pc.choice_question(candidates)["canonical"]
        self.assertEqual(question["criteria"], {"c0": "love-triangle",
                                                 "other": "None of these is the same underlying premise concept."})
        self.assertEqual(pc.validate_choice({"choice": "c0"}, candidates), "love-triangle")
        self.assertIsNone(pc.validate_choice({"choice": "other"}, candidates))

    def test_materialise_collapses_chains_and_counts_occurrences(self):
        forms = [{"form": "a", "count": 2}, {"form": "b", "count": 3}, {"form": "c", "count": 1}]
        artifact = pc.materialise(forms, {"c": "b", "b": "a"}, "sha", "jev")
        self.assertEqual(artifact["map"], {"a": "a", "b": "a", "c": "a"})
        self.assertEqual(artifact["conceptOccurrences"], {"a": 6})

    def test_materialise_refuses_a_cycle(self):
        forms = [{"form": "a", "count": 1}, {"form": "b", "count": 1}]
        with self.assertRaisesRegex(ValueError, "cycle"):
            pc.materialise(forms, {"a": "b", "b": "a"}, "sha", "jev")

    def test_materialize_refuses_a_concept_that_does_not_match_the_recorded_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            tags = os.path.join(directory, "tags.json")
            candidates = os.path.join(directory, "candidates.jsonl")
            answers = os.path.join(directory, "answers.jsonl")
            verification = os.path.join(directory, "verification.jsonl")
            row = {"form": "family-love-triangle", "count": 2,
                   "candidates": [{"concept": "love-triangle", "count": 5, "cosine": .9}]}
            with open(tags, "w", encoding="utf-8") as fh:
                json.dump({"tags": {"movie:1": ["family-love-triangle", "love-triangle"],
                                    "movie:2": ["family-love-triangle", "love-triangle"],
                                    "movie:3": ["love-triangle"]}}, fh)
            with open(candidates, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
            with open(answers, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"form": row["form"], "choice": "other",
                                     "concept": "love-triangle", "model": "jev",
                                     "candidatesSha256": pc.candidates_digest(row["candidates"])}) + "\n")
            open(verification, "w", encoding="utf-8").close()
            args = SimpleNamespace(tags=tags, candidates=candidates, answers=answers,
                                   verification=verification, threshold=.95,
                                   out=os.path.join(directory, "out.json"), model="jev")
            with self.assertRaisesRegex(SystemExit, "does not match"):
                cli.materialize(args)

    def test_verification_keeps_only_exact_pairs(self):
        question = pc.verification_question()["relation"]
        self.assertEqual(set(question["criteria"]), {"exact", "surface-broader", "surface-narrower",
                                                     "roles-or-direction-differ", "related-not-same"})
        self.assertEqual(pc.validate_verification({"choice": "exact"}), "exact")
        with self.assertRaisesRegex(ValueError, "invalid verification"):
            pc.validate_verification({"choice": "yes"})

    def test_title_counts_deduplicate_collapsed_forms_within_a_title(self):
        with tempfile.TemporaryDirectory() as directory:
            tags = os.path.join(directory, "tags.json")
            with open(tags, "w", encoding="utf-8") as fh:
                json.dump({"tags": {"movie:1": ["horror-anthology", "anthology-horror"],
                                     "movie:2": ["horror-anthology"]}}, fh)
            counts = pc.concept_title_counts(tags, {"horror-anthology": "horror-anthology",
                                                    "anthology-horror": "horror-anthology"})
        self.assertEqual(counts, {"horror-anthology": 2})

    def test_adjudication_batches_independent_choices_into_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            candidates = os.path.join(directory, "candidates.jsonl")
            with open(candidates, "w", encoding="utf-8") as fh:
                for number in range(17):
                    fh.write(json.dumps({"form": f"form-{number}", "count": 2,
                                         "candidates": [{"concept": "common", "count": 20,
                                                         "cosine": .8}]}) + "\n")
            args = SimpleNamespace(candidates=candidates, out=os.path.join(directory, "answers.jsonl"),
                                   model="jev", workers=2, questions_per_call=16, spend=False)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(cli.adjudicate(args), 0)
            plan = json.loads(stdout.getvalue())
            self.assertEqual((plan["questionsPlanned"], plan["callsPlanned"]), (17, 2))


if __name__ == "__main__":
    unittest.main()
