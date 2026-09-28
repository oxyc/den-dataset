import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("more_like_cascade", os.path.join(HERE, "more_like_cascade.py"))
cascade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cascade)


def row(n):
    return [f"movie:{i}" for i in range(1, n + 1)]


class FinalistsTest(unittest.TestCase):
    def test_a_tail_title_is_promoted_only_when_recognised_and_clear_of_the_weakest_top_ten(self):
        keys = row(20)
        scores = {key: 0.6 for key in keys}
        scores["movie:3"] = 0.5
        scores["movie:12"] = 0.66      # clear of 0.5 but not recognised
        scores["movie:13"] = 0.70      # recognised and 0.20 above the weakest top-ten score
        scores["movie:14"] = 0.64      # neither
        finalists, fixed, promoted = cascade.finalists_for({"row": keys}, scores, lambda key: True)
        self.assertEqual(promoted, ["movie:13"])
        self.assertEqual(finalists, keys[:10] + ["movie:13"])
        self.assertEqual(fixed, [])
        scores["movie:3"] = 0.56       # the margin is 0.14 now
        self.assertEqual(cascade.finalists_for({"row": keys}, scores, lambda key: True)[2], [])

    def test_promotions_are_capped_highest_first_then_by_atlas_rank(self):
        keys = row(30)
        scores = {key: 0.1 for key in keys[:10]}
        scores.update({key: 0.9 for key in keys[10:]})
        scores["movie:25"] = 0.95
        promoted = cascade.finalists_for({"row": keys}, scores, lambda key: True)[2]
        self.assertEqual(promoted, ["movie:25", "movie:11", "movie:12", "movie:13", "movie:14"])

    def test_no_screen_or_no_evidence_promotes_nothing_and_an_unevidenced_top_title_is_fixed(self):
        keys = row(15)
        finalists, fixed, promoted = cascade.finalists_for({"row": keys}, None, lambda key: key != "movie:4")
        self.assertEqual((promoted, fixed), ([], ["movie:4"]))
        self.assertNotIn("movie:4", finalists)
        scores = {key: 0.1 for key in keys}
        scores["movie:12"] = 0.99
        promoted = cascade.finalists_for({"row": keys}, scores, lambda key: key != "movie:12")[2]
        self.assertEqual(promoted, [], "a candidate without evidence cannot be promoted")


class RerankTest(unittest.TestCase):
    def test_finalists_lead_by_noul_a_fixed_title_keeps_its_slot_and_the_rest_keep_atlas_order(self):
        keys = row(14)
        finalists = [key for key in keys[:10] if key != "movie:2"] + ["movie:13"]
        scores = {key: 0.1 for key in finalists}
        scores["movie:13"] = 0.9
        scores["movie:9"] = 0.5
        scores["movie:1"] = 0.5
        got = cascade.reranked(keys, finalists, ["movie:2"], scores)
        self.assertEqual(got[:4], ["movie:13", "movie:2", "movie:1", "movie:9"])
        self.assertEqual(got[11:], ["movie:11", "movie:12", "movie:14"])
        self.assertEqual(sorted(got), sorted(keys))


class HelpersTest(unittest.TestCase):
    def test_short_works_keep_everything_and_long_ones_share_the_rest(self):
        self.assertEqual(cascade.shares({"a": 10, "b": 100, "c": 100}, 110), {"a": 10, "b": 50, "c": 50})
        self.assertEqual(cascade.shares({"a": 10, "b": 20}, 100), {"a": 10, "b": 20})

    def test_blinding_is_stable_and_independent_of_input_order(self):
        keys = row(12)
        self.assertEqual(cascade.blinded("movie:99", keys, 3), cascade.blinded("movie:99", keys[::-1], 3))
        self.assertNotEqual(list(cascade.blinded("movie:99", keys, 3).values()), keys)

    def test_ndcg_matches_den_index_eval(self):
        grades = {"movie:1": "good", "movie:2": "ok", "movie:3": "bad"}
        best = cascade.ndcg_scores(["movie:1", "movie:2", "movie:3"], grades)
        self.assertAlmostEqual(best["ndcg"], 1.0)
        unjudged_first = cascade.ndcg_scores(["movie:99", "movie:1"], {"movie:1": "good"})
        self.assertAlmostEqual(unjudged_first["ndcg"], 1 / 3 ** 0 / __import__("math").log2(3))
        self.assertAlmostEqual(unjudged_first["condensed"], 1.0)
        worst = cascade.ndcg_scores(["movie:3", "movie:2", "movie:1"], grades)
        self.assertEqual((worst["bad"], worst["judged"]), (1, 3))
        self.assertAlmostEqual(worst["precision"], 0.2)

    def test_popularity_slices(self):
        self.assertEqual(cascade.popularity_slice(0.05), "popular")
        self.assertEqual(cascade.popularity_slice(0.25), "mid")
        self.assertEqual(cascade.popularity_slice(0.26), "long tail")
        self.assertEqual(cascade.popularity_slice(None), "unranked")


if __name__ == "__main__":
    unittest.main()
