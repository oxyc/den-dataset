"""`lib/llm.py` and `lib/llm_providers.py`, offline: what each provider is sent and how its answer, usage and
refusal are read; prices by date; and the orchestration — a refusal inside a multi-title call, a schema
failure, the fallback, a Batch job that is not ready, and one that expired."""
import datetime
import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

from . import llm
from . import llm_providers as providers

ITEMS = [{"key": f"movie:{n}"} for n in range(1, 6)]


class Task:
    """Rows of `{"key", "n"}` for a list of titles, as a JSON object `{"rows": [...]}`."""
    name, version = "test", "test-v1"
    schema = {"type": "object", "properties": {"rows": {"type": "array"}}, "required": ["rows"]}

    def key(self, item):
        return item["key"]

    def request(self, items):
        return llm.request(" ".join(self.key(i) for i in items), 100, schema=self.schema)

    def parse(self, items, answer):
        value = answer["value"] if answer["value"] is not None else json.loads(answer["text"])
        return {row["key"]: row["n"] for row in value["rows"] if row["key"] in {self.key(i) for i in items}}


class Fake:
    """A provider whose answers are a function of the prompt. `refuse` is the keys it will not answer about."""

    def __init__(self, refuse=(), garble=(), batch_state="done"):
        self.refuse, self.garble, self.batch_state = set(refuse), set(garble), batch_state
        self.prompts = []

    def online(self, cfg, request):
        self.prompts.append((cfg["model"], request["prompt"]))
        keys = request["prompt"].split()
        if cfg["model"] == "primary" and self.refuse & set(keys):
            return providers.answer("", refused=True, why="SAFETY")
        if cfg["model"] == "primary" and self.garble & set(keys) and len(keys) > 1:
            return providers.answer("{not json", usage=self.usage())
        rows = [{"key": key, "n": f"{cfg['model']}:{key}"} for key in keys]
        return providers.answer(json.dumps({"rows": rows}), usage=self.usage())

    @staticmethod
    def usage():
        return {"inputTokens": 1000, "cachedTokens": 0, "outputTokens": 100, "reasoningTokens": 0}

    def submit(self, cfg, requests, label, source):
        self.submitted = requests
        return {"name": "batches/1"}

    def poll(self, cfg, job, customs):
        if self.batch_state != "done":
            return self.batch_state, {}
        out = {}
        for custom in customs:
            keys = self.submitted[custom]["prompt"].split()
            out[custom] = providers.answer(json.dumps({"rows": [{"key": k, "n": f"batch:{k}"} for k in keys]}),
                                           usage=self.usage())
        return "done", out


def configured(fake, per_call=5, fallback=True):
    patches = (mock.patch.dict(providers.PROVIDERS, {"fake": fake}),
               mock.patch.dict(llm.PRICES, {"primary": [("2026-01-01", 1e-6, 2e-6, 1e-6)],
                                            "backup": [("2026-01-01", 3e-6, 6e-6, 3e-6)]}))
    for patch in patches:
        patch.start()
    cfg = {**llm.STEP_DEFAULTS, "step": "test", "provider": "fake", "model": "primary", "titlesPerCall": per_call,
           "fallback": ({**llm.STEP_DEFAULTS, "step": "test", "provider": "fake", "model": "backup"}
                        if fallback else None)}
    return cfg, patches


class Generate(unittest.TestCase):
    def run_with(self, fake, **kwargs):
        cfg, patches = configured(fake, **kwargs)
        try:
            return llm.generate(cfg, Task(), ITEMS)
        finally:
            for patch in patches:
                patch.stop()

    def test_a_refused_title_gets_its_answer_from_the_fallback_and_the_rest_of_its_call_is_unaffected(self):
        fake = Fake(refuse={"movie:3"})
        rows, calls = self.run_with(fake)
        self.assertEqual({k: r["value"] for k, r in rows.items()},
                         {"movie:1": "primary:movie:1", "movie:2": "primary:movie:2", "movie:3": "backup:movie:3",
                          "movie:4": "primary:movie:4", "movie:5": "primary:movie:5"})
        self.assertEqual(rows["movie:3"]["by"], {"provider": "fake", "model": "backup", "mode": "online",
                                                 "version": "test-v1"})
        # The five-title call, each title alone, movie:3 once more, then the fallback.
        self.assertEqual([m for m, _ in fake.prompts], ["primary"] * 7 + ["backup"])
        self.assertEqual(len(calls), 8)

    def test_a_title_the_fallback_refuses_too_is_refused(self):
        fake = Fake(refuse={"movie:3"})
        rows, _ = self.run_with(fake, fallback=False)
        self.assertEqual(rows["movie:3"], {"refused": True, "by": {"provider": "fake", "model": "primary",
                                                                   "mode": "online", "version": "test-v1"}})
        self.assertEqual(rows["movie:1"]["value"], "primary:movie:1")

    def test_a_multi_title_answer_that_does_not_parse_is_asked_again_one_title_a_call(self):
        fake = Fake(garble={"movie:2"})
        rows, calls = self.run_with(fake)
        self.assertEqual(sorted(r["value"] for r in rows.values()),
                         [f"primary:movie:{n}" for n in range(1, 6)])
        self.assertEqual(len(calls), 6)

    def test_a_transport_failure_is_an_error_row_not_a_refusal(self):
        class Down(Fake):
            def online(self, cfg, request):
                raise providers.Unavailable("HTTP 503 b'busy'", 503)
        rows, calls = self.run_with(Down())
        self.assertEqual(rows["movie:1"], {"error": "HTTP 503 b'busy'", "unavailable": True})
        self.assertEqual(calls, [])

    def test_each_call_is_priced_and_a_cap_stops_before_the_call_that_could_cross_it(self):
        cfg, patches = configured(Fake(), per_call=1)
        try:
            _, calls = llm.generate(cfg, Task(), ITEMS[:1])
            self.assertAlmostEqual(calls[0]["costUSD"], 1000 * 1e-6 + 100 * 2e-6)
            with self.assertRaises(llm.OverBudget):
                llm.generate(cfg, Task(), ITEMS[:1], budget=llm.Budget(0.0001))
        finally:
            for patch in patches:
                patch.stop()

    def test_every_row_is_handed_to_the_recorder_as_it_is_final(self):
        seen = []
        cfg, patches = configured(Fake(refuse={"movie:5"}))
        try:
            llm.generate(cfg, Task(), ITEMS, record=lambda key, row: seen.append(key))
        finally:
            for patch in patches:
                patch.stop()
        self.assertEqual(sorted(seen), [item["key"] for item in ITEMS])


class Batch(unittest.TestCase):
    def test_a_job_that_is_not_ready_returns_nothing_and_a_finished_one_is_parsed_at_batch_price(self):
        fake = Fake()
        cfg, patches = configured(fake, per_call=2)
        try:
            with tempfile.TemporaryDirectory() as directory:
                job = llm.submit({**cfg, "mode": "batch"}, Task(), ITEMS, "test-1", directory)
            self.assertEqual(job["chunks"], {"c00000": ["movie:1", "movie:2"], "c00001": ["movie:3", "movie:4"],
                                             "c00002": ["movie:5"]})
            fake.batch_state = "running"
            self.assertEqual(llm.collect(cfg, job, Task(), ITEMS), ("running", {}, []))
            fake.batch_state = "done"
            state, rows, calls = llm.collect(cfg, job, Task(), ITEMS)
        finally:
            for patch in patches:
                patch.stop()
        self.assertEqual(state, "done")
        self.assertEqual(rows["movie:5"]["value"], "batch:movie:5")
        self.assertEqual(rows["movie:5"]["by"]["mode"], "batch")
        self.assertAlmostEqual(calls[0]["costUSD"], (1000 * 1e-6 + 100 * 2e-6) / 2)

    def test_an_expired_job_is_finished_online_and_never_resubmitted(self):
        fake = Fake(batch_state="expired")
        cfg, patches = configured(fake, per_call=5)
        try:
            with tempfile.TemporaryDirectory() as directory:
                job = llm.submit(cfg, Task(), ITEMS, "test-1", directory)
            fake.submitted = None
            state, rows, _ = llm.collect(cfg, job, Task(), ITEMS)
        finally:
            for patch in patches:
                patch.stop()
        self.assertEqual(state, "expired")
        self.assertEqual(sorted(r["value"] for r in rows.values()), [f"primary:movie:{n}" for n in range(1, 6)])
        self.assertIsNone(fake.submitted)


class Prices(unittest.TestCase):
    def test_flash_doubles_on_new_year_2027(self):
        self.assertEqual(llm.price("gemini-3.7-flash", datetime.date(2026, 12, 31))[:2], (0.75e-6, 3.75e-6))
        self.assertEqual(llm.price("gemini-3.7-flash", datetime.date(2027, 1, 1))[:2], (1.5e-6, 7.5e-6))

    def test_a_model_with_no_price_is_refused_before_it_is_asked(self):
        with self.assertRaisesRegex(ValueError, "no price"):
            llm.check({**llm.STEP_DEFAULTS, "step": "x", "provider": "openai", "model": "gpt-unpriced"})

    def test_thinking_bills_as_output_and_a_plan_bills_nothing(self):
        usage = {"inputTokens": 100, "cachedTokens": 0, "outputTokens": 10, "reasoningTokens": 90}
        cfg = {"provider": "openai", "model": "gpt-5.6-luna"}
        self.assertAlmostEqual(llm.cost(cfg, usage, "online", datetime.date(2026, 10, 1)), 100 * 0.2e-6 + 100 * 1.2e-6)
        self.assertEqual(llm.cost({"provider": "claude-cli", "model": "sonnet"}, usage, "online"), 0.0)


class Config(unittest.TestCase):
    def test_the_committed_config_is_valid_and_an_override_switches_a_model(self):
        for name in llm.load():
            llm.step(name)
        cfg = llm.step("fan_picks", {"provider": "openai", "model": "gpt-5.6-luna", "thinking": "low"})
        self.assertEqual((cfg["provider"], cfg["model"]), ("openai", "gpt-5.6-luna"))

    def test_a_cli_provider_has_no_batch_and_never_runs_in_actions(self):
        with self.assertRaisesRegex(ValueError, "no Batch"):
            llm.check({**llm.STEP_DEFAULTS, "step": "x", "provider": "codex-cli", "model": "m", "mode": "batch"})
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), self.assertRaises(providers.Unavailable):
            providers.PROVIDERS["claude-cli"].online({"provider": "claude-cli", "model": "m"},
                                                     llm.request("x", 10))


class Wire(unittest.TestCase):
    """Each provider's body and its reading of an answer, a refusal and the usage."""

    def test_gemini(self):
        gemini = providers.PROVIDERS["gemini"]
        body = gemini.body({"model": "m", "thinking": "low"},
                           llm.request("p", 50, system="s", schema={"type": "object"}))
        self.assertEqual(body["generationConfig"], {"responseMimeType": "application/json",
                                                    "responseSchema": {"type": "object"}, "maxOutputTokens": 50,
                                                    "thinkingConfig": {"thinkingLevel": "low"}})
        self.assertEqual(body["systemInstruction"], {"parts": [{"text": "s"}]})
        blocked = gemini.read({"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}}, {})
        self.assertEqual((blocked["refused"], blocked["why"]), (True, "PROHIBITED_CONTENT"))
        empty = gemini.read({"candidates": [{"content": {"parts": [{"text": " "}]}, "finishReason": "STOP"}]}, {})
        self.assertTrue(empty["refused"])
        cut = gemini.read({"candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}]}, {})
        self.assertFalse(cut["refused"])
        ok = gemini.read({"candidates": [{"content": {"parts": [{"text": "{}"}]}, "finishReason": "STOP"}],
                          "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 3,
                                            "thoughtsTokenCount": 2, "cachedContentTokenCount": 4}}, {})
        self.assertEqual(ok["usage"], {"inputTokens": 9, "cachedTokens": 4, "outputTokens": 3, "reasoningTokens": 2})

    def test_openai(self):
        openai = providers.PROVIDERS["openai"]
        body = openai.body({"model": "gpt-5.6-luna", "thinking": "low"},
                           llm.request("p", 50, system="s", schema={"type": "object"}, name="premise_tags"))
        self.assertEqual(body["text"]["format"], {"type": "json_schema", "name": "premise_tags",
                                                  "schema": {"type": "object"}, "strict": True})
        self.assertEqual((body["reasoning"], body["input"][0]["role"]), ({"effort": "low"}, "developer"))
        refused = openai.read({"output": [{"type": "message", "content": [{"type": "refusal",
                                                                            "refusal": "no"}]}]}, {})
        self.assertEqual((refused["refused"], refused["why"]), (True, "no"))
        ok = openai.read({"output": [{"type": "reasoning"}, {"type": "message", "content": [
            {"type": "output_text", "text": "{}"}]}], "usage": {
            "input_tokens": 10, "input_tokens_details": {"cached_tokens": 2}, "output_tokens": 30,
            "output_tokens_details": {"reasoning_tokens": 20}}}, {})
        self.assertEqual((ok["text"], ok["refused"]), ("{}", False))
        self.assertEqual(ok["usage"], {"inputTokens": 10, "cachedTokens": 2, "outputTokens": 10,
                                       "reasoningTokens": 20})

    def test_anthropic(self):
        anthropic = providers.PROVIDERS["anthropic"]
        body = anthropic.body({"model": "claude-haiku-4-5-20251001", "thinking": None},
                              llm.request("p", 50, schema={"type": "object"}))
        self.assertEqual(body["tool_choice"], {"type": "tool", "name": "answer"})
        self.assertEqual(body["thinking"], {"type": "disabled"})
        refused = anthropic.read({"content": [], "stop_reason": "refusal"}, {})
        self.assertTrue(refused["refused"])
        ok = anthropic.read({"content": [{"type": "tool_use", "name": "answer", "input": {"rows": []}}],
                             "stop_reason": "tool_use", "usage": {"input_tokens": 5, "output_tokens": 7}}, {})
        self.assertEqual((ok["value"], ok["refused"]), ({"rows": []}, False))

    def test_the_key_goes_only_into_a_header_and_never_into_an_error(self):
        sent = []

        def urlopen(request, timeout=None):
            sent.append(request)
            raise urllib.error.HTTPError(request.full_url, 400, "bad", {}, io.BytesIO(b"bad request"))
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-secret"}), \
                mock.patch("urllib.request.urlopen", urlopen):
            with self.assertRaises(providers.Unavailable) as caught:
                providers.PROVIDERS["openai"].online({"model": "gpt-5.6-luna"}, llm.request("p", 5))
        self.assertNotIn("sk-secret", str(caught.exception))
        self.assertNotIn("sk-secret", sent[0].full_url + sent[0].data.decode())
        self.assertEqual(sent[0].get_header("Authorization"), "Bearer sk-secret")

    def test_a_transient_status_is_retried_and_a_definitive_one_is_not(self):
        calls = []

        def urlopen(request, timeout=None):
            calls.append(1)
            raise urllib.error.HTTPError(request.full_url, 429 if len(calls) < 3 else 400, "x", {},
                                         io.BytesIO(b""))
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k"}), mock.patch("urllib.request.urlopen", urlopen), \
                mock.patch.object(providers.time, "sleep"):
            with self.assertRaisesRegex(providers.Unavailable, "HTTP 400"):
                providers.PROVIDERS["gemini"].online({"model": "m"}, llm.request("p", 5))
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
