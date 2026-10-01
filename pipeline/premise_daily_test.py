"""`pipeline/premise_daily.py`: premise tags through `lib/llm.py`, with OpenAI and Anthropic stubbed below
`urlopen` — a written and costed batch, a refused title answered by the fallback, a repaired space, a row
asked again alone, a resumed run that asks nothing, and what the merge records about who answered."""
import json
import os
import tempfile
import unittest
from unittest import mock

from store import vector_blob

from . import artifacts, premise_daily

TAGS = ["identity-swap", "hidden-heir", "family-secret", "reluctant-alliance",
        "betrayal-revenge", "class-barrier", "secret-parentage", "race-against-time"]
PLOTS = {"movie:7": "Two strangers exchange identities and uncover a family secret.",
         "movie:8": "A heir hides among servants while a rival plots against the family."}


class Reply:
    def __init__(self, value):
        self.value = value
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.value).encode()


class Wire:
    """OpenAI's Responses API and Anthropic's Messages API. `answers` maps a key to the tags OpenAI writes
    for it, or to None for a refusal; Anthropic answers every title with TAGS."""

    def __init__(self, answers):
        self.answers, self.sent = answers, []

    def __call__(self, request, timeout=None):
        body = json.loads(request.data)
        self.sent.append((request.full_url, body))
        if request.full_url.endswith("/v1/responses"):
            rows = json.loads(body["input"][-1]["content"].split("\n", 1)[1])
            if any(self.answers.get(row["key"]) is None for row in rows):
                return Reply({"output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}],
                              "usage": {"input_tokens": 50, "output_tokens": 5}})
            text = json.dumps({"rows": [{"key": row["key"], "tags": self.answers[row["key"]]} for row in rows]})
            return Reply({"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
                          "usage": {"input_tokens": 1000, "output_tokens": 200,
                                    "output_tokens_details": {"reasoning_tokens": 100}}})
        rows = json.loads(body["messages"][0]["content"].split("\n", 1)[1])
        return Reply({"content": [{"type": "tool_use", "name": "answer",
                                   "input": {"rows": [{"key": row["key"], "tags": TAGS} for row in rows]}}],
                      "stop_reason": "tool_use", "usage": {"input_tokens": 1000, "output_tokens": 200}})


class PremiseGeneration(unittest.TestCase):
    def phase(self, keys=("movie:7", "movie:8")):
        root = self.enterContext(tempfile.TemporaryDirectory())
        os.makedirs(os.path.join(root, "in"))
        rows = [{"key": key, "mediaType": "movie", "tmdbId": int(key.split(":")[1]), "plot": PLOTS[key]}
                for key in keys]
        with open(os.path.join(root, "in", "batch-0000.json"), "w", encoding="utf-8") as handle:
            json.dump(rows, handle)
        with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8") as handle:
            json.dump({"titles": len(rows), "batches": 1, "estimatedInputTokens": 100,
                       "estimatedOutputTokens": 100}, handle)
        return root

    def run_phase(self, phase, answers):
        wire = Wire(answers)
        with mock.patch("urllib.request.urlopen", wire), mock.patch("time.sleep"), \
                mock.patch.dict(os.environ, {"OPENAI_API_KEY": "o", "ANTHROPIC_API_KEY": "a"}):
            result = premise_daily.generate(phase, 1.0)
        with open(os.path.join(phase, "out", "batch-0000.json"), encoding="utf-8") as handle:
            return result, {row["key"]: row for row in json.load(handle)}, wire

    def test_a_batch_is_tagged_in_one_call_costed_and_says_who_answered(self):
        result, out, wire = self.run_phase(self.phase(), {"movie:7": TAGS, "movie:8": TAGS})
        self.assertEqual([url for url, _ in wire.sent], ["https://api.openai.com/v1/responses"])
        body = wire.sent[0][1]
        self.assertEqual((body["model"], body["text"]["format"]["type"]), ("gpt-5.6-luna", "json_schema"))
        self.assertEqual(out["movie:7"]["tags"], TAGS)
        self.assertEqual((out["movie:7"]["by"]["provider"], out["movie:7"]["by"]["model"]),
                         ("openai", "gpt-5.6-luna"))
        self.assertEqual((result["generated"], result["byModel"]), (2, {"gpt-5.6-luna": 2}))
        self.assertAlmostEqual(result["costUSD"], 1000 * 0.20e-6 + 200 * 1.20e-6)

    def test_a_refused_title_is_answered_by_the_fallback_and_its_batch_mate_by_luna(self):
        result, out, wire = self.run_phase(self.phase(), {"movie:7": TAGS, "movie:8": None})
        self.assertEqual(out["movie:7"]["by"]["model"], "gpt-5.6-luna")
        self.assertEqual(out["movie:8"]["by"]["model"], "claude-haiku-4-5-20251001")
        self.assertEqual(result["byModel"], {"claude-haiku-4-5-20251001": 1, "gpt-5.6-luna": 1})
        # The two-title call, each alone, movie:8 once more, then Haiku.
        self.assertEqual([url.rsplit("/", 1)[1] for url, _ in wire.sent],
                         ["responses", "responses", "responses", "responses", "messages"])

    def test_a_space_in_a_tag_is_repaired_and_a_short_row_is_asked_again_alone(self):
        spaced = ["identity swap"] + TAGS[1:]
        short = TAGS[:6] + ["comedy", "character-arc"]
        answers = {"movie:7": spaced, "movie:8": short}
        _result, out, wire = self.run_phase(self.phase(), answers)
        self.assertEqual(out["movie:7"]["tags"][0], "identity-swap")
        self.assertNotIn("movie:8", out, "a row luna keeps answering short is left for a later run")
        self.assertEqual(len(wire.sent), 2)
        self.assertEqual((_result["short"], _result["refused"]), (["movie:8"], []))

    def test_an_answer_missing_a_row_keeps_the_rows_it_has_and_asks_only_the_missing_one(self):
        class Short(Wire):
            def __call__(self, request, timeout=None):
                reply = super().__call__(request, timeout)
                if request.full_url.endswith("/v1/responses"):
                    body = reply.value
                    text = json.loads(body["output"][0]["content"][0]["text"])
                    if len(text["rows"]) > 1:
                        text["rows"] = text["rows"][:1]
                        body["output"][0]["content"][0]["text"] = json.dumps(text)
                return reply
        wire = Short({"movie:7": TAGS, "movie:8": TAGS})
        with mock.patch("urllib.request.urlopen", wire), \
                mock.patch.dict(os.environ, {"OPENAI_API_KEY": "o", "ANTHROPIC_API_KEY": "a"}):
            result = premise_daily.generate(self.phase(), 1.0)
        self.assertEqual(result["generated"], 2)
        asked = [json.loads(body["input"][-1]["content"].split("\n", 1)[1]) for _, body in wire.sent]
        self.assertEqual([[row["key"] for row in rows] for rows in asked], [["movie:7", "movie:8"], ["movie:8"]])

    def test_a_resumed_run_asks_nothing_it_already_has(self):
        phase = self.phase()
        self.run_phase(phase, {"movie:7": TAGS, "movie:8": TAGS})
        result, out, wire = self.run_phase(phase, {})
        self.assertEqual((wire.sent, result["resumed"], result["generated"]), ([], 2, 0))
        self.assertEqual(sorted(out), ["movie:7", "movie:8"])

    def test_a_title_left_short_is_recorded_and_asked_again_three_then_six_months_on(self):
        """#200: a title no model would tag is not asked every run, and not given up on either."""
        kept, short = {}, TAGS[:6] + ["comedy", "character-arc"]

        def run(today, answers):
            wire = Wire(answers)
            phase = self.phase()
            with mock.patch("urllib.request.urlopen", wire), mock.patch("time.sleep"), \
                    mock.patch.dict(os.environ, {"OPENAI_API_KEY": "o", "ANTHROPIC_API_KEY": "a"}):
                return premise_daily.generate(phase, 1.0, kept=kept, today=today), wire
        result, _ = run("2026-10-01", {"movie:7": TAGS, "movie:8": short})
        self.assertEqual((result["short"], kept["movie:8"]["untaggable"], kept["movie:8"]["asks"]),
                         (["movie:8"], "short", 1))
        result, wire = run("2026-12-30", {"movie:8": short})
        self.assertEqual((result["notRetriedYet"], wire.sent), (["movie:8"], []), "not before 91 days")
        result, wire = run("2026-12-31", {"movie:8": short})
        self.assertEqual((result["short"], kept["movie:8"]["asks"], kept["movie:8"]["first"]),
                         (["movie:8"], 2, "2026-10-01"))
        _, wire = run("2027-03-31", {"movie:8": short})
        self.assertEqual(wire.sent, [], "not before 182 days")
        run("2027-04-01", {"movie:8": short})
        result, wire = run("2028-01-01", {"movie:8": short})
        self.assertEqual((kept["movie:8"]["asks"], wire.sent), (3, []), "then only on a new model or evidence")

    def test_the_projection_must_fit_the_cap_before_any_call(self):
        with self.assertRaisesRegex(RuntimeError, "daily cap"):
            premise_daily.generate(self.phase(), 1e-9)


class Ctx:
    """The two artifacts and the out-dir the correction step touches, in a temporary directory."""

    def __init__(self, root):
        self.out_dir = root

    def path(self, artifact):
        return os.path.join(self.out_dir, artifact.filename)


class CommittedCorrections(unittest.TestCase):
    """A run seeded from the bundle takes the committed rows that differ, and re-embeds only those (#184)."""

    DIMS = 4

    def setUp(self):
        self.root = self.enterContext(tempfile.TemporaryDirectory())
        self.ctx = Ctx(os.path.join(self.root, "out"))
        os.makedirs(os.path.join(self.root, "repo", "data"))
        os.makedirs(self.ctx.out_dir)
        self.enterContext(mock.patch.object(premise_daily, "REPO", os.path.join(self.root, "repo")))
        self.enterContext(mock.patch.object(premise_daily.embed_canary, "gate", lambda _url: None))
        self.embedded = []

        def embed(_url, texts):
            self.embedded.extend(texts)
            return [[len(text) % 100] * self.DIMS for text in texts]
        self.enterContext(mock.patch.object(premise_daily, "_embed", embed))

    def write_tags(self, committed, run):
        with open(os.path.join(self.root, "repo", "data", "premise-tags-v2.json"), "w", encoding="utf-8") as fh:
            json.dump({"count": len(committed), "tags": committed}, fh)
        with open(self.ctx.path(artifacts.PREMISE_TAGS), "w", encoding="utf-8") as fh:
            json.dump({"count": len(run), "dailyIncrements": [{"titles": 1}], "tags": run}, fh)

    def write_blob(self, keys):
        rows = b"".join(bytes([row + 1] * self.DIMS) for row in range(len(keys)))
        vector_blob.write(self.ctx.path(artifacts.PREMISE_VECTORS), keys, rows, self.DIMS)

    def blob(self):
        _count, _dims, keys, blob, base = vector_blob.read(self.ctx.path(artifacts.PREMISE_VECTORS))
        return keys, [blob[base + row * self.DIMS:base + (row + 1) * self.DIMS] for row in range(len(keys))]

    def test_only_shared_keys_whose_rows_differ_are_corrections(self):
        self.write_tags({"movie:1": ["a"], "movie:2": ["fixed-b"], "movie:3": ["committed-only"]},
                        {"movie:1": ["a"], "movie:2": ["wrong-b"], "tv:9": ["daily-title"]})
        self.assertEqual(premise_daily.committed_corrections(self.ctx), ["movie:2"])

    def test_no_run_copy_means_nothing_to_correct(self):
        self.write_tags({"movie:1": ["a"]}, {})
        os.remove(self.ctx.path(artifacts.PREMISE_TAGS))
        self.assertEqual(premise_daily.committed_corrections(self.ctx), [])

    def test_corrections_rewrite_those_rows_and_reembed_them_in_place(self):
        self.write_tags({"movie:1": ["a"], "movie:2": ["fixed-b"], "movie:3": ["fixed-c"]},
                        {"movie:1": ["a"], "movie:2": ["wrong-b"], "movie:3": ["wrong-c"], "tv:9": ["daily"]})
        self.write_blob(["movie:1", "movie:2", "movie:3", "tv:9"])
        before_keys, before = self.blob()
        keys = premise_daily.committed_corrections(self.ctx)
        result = premise_daily.apply_corrections(self.ctx, "http://embed.invalid", keys)

        self.assertEqual((result["corrected"], result["reembedded"], result["appended"]), (2, 2, 0))
        self.assertEqual(self.embedded, ["fixed-b", "fixed-c"])
        after_keys, after = self.blob()
        self.assertEqual(after_keys, before_keys, "re-embedding must not reorder or add rows")
        self.assertEqual([after[0], after[3]], [before[0], before[3]], "untouched rows keep their bytes")
        self.assertEqual(after[1], bytes([len("fixed-b")] * self.DIMS))
        with open(self.ctx.path(artifacts.PREMISE_TAGS), encoding="utf-8") as fh:
            run = json.load(fh)
        self.assertEqual(run["tags"], {"movie:1": ["a"], "movie:2": ["fixed-b"], "movie:3": ["fixed-c"],
                                       "tv:9": ["daily"]})
        self.assertEqual(run["dailyIncrements"], [{"titles": 1}], "the run copy's own record survives")
        self.assertEqual(premise_daily.committed_corrections(self.ctx), [], "a second pass finds nothing")

    def test_a_correction_never_embeds_an_unrelated_missing_key(self):
        # The live blob lacks three tagged titles; a correction-only run must leave them to the premise step.
        self.write_tags({"movie:2": ["fixed-b"]}, {"movie:1": ["a"], "movie:2": ["wrong-b"], "movie:5": ["no-row"]})
        self.write_blob(["movie:1", "movie:2"])
        result = premise_daily.apply_corrections(self.ctx, "http://embed.invalid", ["movie:2"])
        self.assertEqual((result["reembedded"], result["appended"]), (1, 0))
        self.assertEqual(self.embedded, ["fixed-b"])
        self.assertEqual(self.blob()[0], ["movie:1", "movie:2"])


class UnlabelledTitles(unittest.TestCase):
    """The tag file can hold titles `labels-t02.json` has no record for; the premise labels are still rebuilt."""

    DIMS = 4

    def setUp(self):
        self.ctx = Ctx(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.object(premise_daily.embed_canary, "gate", lambda _url: None))
        self.embedded = []

        def embed(_url, texts):
            self.embedded.extend(texts)
            return [[len(text) % 100] * self.DIMS for text in texts]
        self.enterContext(mock.patch.object(premise_daily, "_embed", embed))

    def write(self, tags, blob_keys, labelled):
        with open(self.ctx.path(artifacts.PREMISE_TAGS), "w", encoding="utf-8") as fh:
            json.dump({"count": len(tags), "tags": tags}, fh)
        with open(self.ctx.path(artifacts.VECTOR_LABELS), "w", encoding="utf-8") as fh:
            json.dump({"taxonomyVersion": "t", "records": [
                {"mediaType": key.split(":")[0], "tmdbId": int(key.split(":")[1]), "tag": key} for key in labelled]}, fh)
        rows = b"".join(bytes([row + 1] * self.DIMS) for row in range(len(blob_keys)))
        vector_blob.write(self.ctx.path(artifacts.PREMISE_VECTORS), blob_keys, rows, self.DIMS)

    def blob(self):
        _count, _dims, keys, blob, base = vector_blob.read(self.ctx.path(artifacts.PREMISE_VECTORS))
        return keys, [blob[base + row * self.DIMS:base + (row + 1) * self.DIMS] for row in range(len(keys))]

    def premise_labels(self):
        with open(self.ctx.path(artifacts.PREMISE_LABELS), encoding="utf-8") as fh:
            return [record["tag"] for record in json.load(fh)["records"]]

    def test_a_tagged_title_without_a_record_is_not_appended(self):
        # The 2026-10-01 daily: three tagged corpus titles had no labels record, and the rebuild refused the day.
        tags = {"movie:1": ["a"], "movie:2": ["b"], "movie:5": ["new-title"], "movie:7": ["no-record"]}
        self.write(tags, ["movie:1", "movie:2"], ["movie:1", "movie:2", "movie:5"])
        result = premise_daily.extend_vectors(self.ctx, "http://embed.invalid")

        self.assertEqual((result["embedded"], result["unlabelled"], result["dropped"]), (1, ["movie:7"], []))
        self.assertEqual(self.embedded, ["new-title"])
        self.assertEqual(self.blob()[0], ["movie:1", "movie:2", "movie:5"])
        self.assertEqual(self.premise_labels(), ["movie:1", "movie:2", "movie:5"], "row i describes row i")
        with open(self.ctx.path(artifacts.PREMISE_TAGS), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["tags"], tags, "the unlabelled title keeps its tags")

    def test_a_row_whose_title_lost_its_record_leaves_with_its_key(self):
        self.write({"movie:1": ["a"], "movie:2": ["b"], "movie:3": ["c"], "movie:5": ["new"]},
                   ["movie:1", "movie:2", "movie:3"], ["movie:1", "movie:3", "movie:5"])
        _keys, before = self.blob()
        result = premise_daily.extend_vectors(self.ctx, "http://embed.invalid")

        self.assertEqual(result["dropped"], ["movie:2"])
        keys, after = self.blob()
        self.assertEqual(keys, ["movie:1", "movie:3", "movie:5"])
        self.assertEqual(after[:2], [before[0], before[2]], "the rows that stay keep their own vectors")
        self.assertEqual(self.premise_labels(), keys)

    def test_only_unlabelled_titles_missing_changes_nothing(self):
        self.write({"movie:1": ["a"], "movie:7": ["no-record"]}, ["movie:1"], ["movie:1"])
        result = premise_daily.extend_vectors(self.ctx, "http://embed.invalid")
        self.assertEqual((result["embedded"], result["unlabelled"]), (0, ["movie:7"]))
        self.assertEqual(self.embedded, [])
        self.assertFalse(os.path.exists(self.ctx.path(artifacts.PREMISE_LABELS)))


if __name__ == "__main__":
    unittest.main()
