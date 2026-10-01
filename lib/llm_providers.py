"""How each model API is spoken: the request body, the answer, the usage, refusals and Batch jobs.

`lib/llm.py` is what a step calls; this is the five dialects under it (oxyc/den-dataset#183). Each provider
turns one request — `{"prompt", "system", "schema", "maxOutputTokens", "json"}` — into its own body, and its
answer back into one shape: `{"text", "value", "usage", "modelVersion", "refused", "why", "raw"}`, where
`usage` is `{"inputTokens", "cachedTokens", "outputTokens", "reasoningTokens"}` and `value` is the parsed
answer when the provider enforced a schema.

**A refusal is an answer, not an error.** Gemini's `promptFeedback.blockReason`, a safety `finishReason` or an
empty candidate; OpenAI's `refusal` content or a `content_filter` stop; Anthropic's `stop_reason: refusal`.
Transport failures, 429s and 5xx are retried here and never read as a refusal.

**Keys** are read from the environment when a request is sent, go only into a header, and are never logged:
an error carries the provider's status and the start of its body, never the request.

The two CLI providers bill to the owner's monthly plans. They are for local backfills and bake-offs, so they
answer online only and refuse to run in GitHub Actions.
"""
import datetime
import email.utils
import json
import math
import os
import random
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

#: Attempts per request, and the longest wait a server's `Retry-After` is obeyed for.
ATTEMPTS = 6
MAX_RETRY_DELAY = 120.0
RETRYABLE = (408, 429, 500, 502, 503, 504, 529)


class Unavailable(RuntimeError):
    """No answer: a transport failure or an HTTP error, after the retries a transient one gets."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def key(name):
    value = os.environ.get(name)
    if not value:
        raise Unavailable(f"{name} is not set")
    return value


def retry_delay(error, attempt, now=None):
    """Provider-directed delay when present, else bounded exponential backoff with small jitter."""
    value = error.headers.get("Retry-After") if error.headers else None
    if value:
        try:
            seconds = float(value)
        except ValueError:
            try:
                retry_at = email.utils.parsedate_to_datetime(value)
                current = now or datetime.datetime.now(datetime.timezone.utc)
                seconds = (retry_at - current).total_seconds()
            except (TypeError, ValueError, OverflowError):
                seconds = None
        if seconds is not None and math.isfinite(seconds):
            return min(MAX_RETRY_DELAY, max(0.0, seconds))
    return min(MAX_RETRY_DELAY, 2 ** attempt + random.uniform(0.0, 1.0))


def http(method, url, body=None, headers=None, raw=False, timeout=300):
    """One request. Returns `(headers, body)`, the body parsed as JSON unless `raw`; raises HTTPError."""
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read()
        return response.headers, (payload if raw else json.loads(payload or b"{}"))


#: How many times a generation request is sent at most when its response never arrives. A send whose answer
#: was lost may still be billed — the provider may have done the work — so it is tried once more, not six
#: times, and `sends()` lets the caller count every such send as spent.
GENERATION_SENDS = 2
_local = threading.local()


def sends():
    """How many times this thread's last `send` sent its request: its unanswered sends, plus one."""
    return getattr(_local, "sends", 1)


def send(method, url, body=None, headers=None, raw=False, timeout=300, attempts=ATTEMPTS, generation=False):
    """`http`, retried on 408/429/5xx and transport failures. Raises `Unavailable` with no key in it.

    `generation` marks a request the provider bills when it runs: after a lost response it is sent at most
    `GENERATION_SENDS` times in all."""
    _local.sends = 1
    for attempt in range(attempts):
        last = attempt == attempts - 1
        try:
            return http(method, url, body, headers, raw, timeout)
        except urllib.error.HTTPError as error:
            if error.code in RETRYABLE and not last:
                time.sleep(retry_delay(error, attempt))
                continue
            raise Unavailable(f"HTTP {error.code} {error.read()[:300]!r}", error.code) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            if generation:
                _local.sends += 1
            if not last and not (generation and _local.sends > GENERATION_SENDS):
                time.sleep(min(MAX_RETRY_DELAY, 2 ** attempt + random.uniform(0.0, 1.0)))
                continue
            raise Unavailable(f"{type(error).__name__}: {error}") from None


def find(value, name):
    """The first value under `name` anywhere in a JSON tree: an operation's shape differs by API version."""
    if isinstance(value, dict):
        if name in value:
            return value[name]
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        return None
    for child in children:
        found = find(child, name)
        if found is not None:
            return found
    return None


def answer(text, value=None, usage=None, model_version=None, refused=False, why=None, raw=None):
    return {"text": text, "value": value,
            "usage": usage or {"inputTokens": 0, "cachedTokens": 0, "outputTokens": 0, "reasoningTokens": 0},
            "modelVersion": model_version, "refused": refused, "why": why, "raw": raw}


# --- Gemini ---------------------------------------------------------------------------------------------

class Gemini:
    API = "https://generativelanguage.googleapis.com"
    KEY = "GEMINI_API_KEY"
    #: Finish reasons that mean the model declined, not that it ran out or stopped.
    REFUSALS = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY"}

    def headers(self, extra=None):
        return {"x-goog-api-key": key(self.KEY), "Content-Type": "application/json", **(extra or {})}

    def body(self, cfg, request):
        body = {"contents": [{"role": "user", "parts": [{"text": request["prompt"]}]}]}
        if request.get("system"):
            body["systemInstruction"] = {"parts": [{"text": request["system"]}]}
        config = {}
        if request.get("json", True) or request.get("schema"):
            config["responseMimeType"] = "application/json"
        if request.get("schema"):
            config["responseSchema"] = request["schema"]
        config["maxOutputTokens"] = request["maxOutputTokens"]
        if cfg.get("thinking"):
            config["thinkingConfig"] = {"thinkingLevel": cfg["thinking"]}
        body["generationConfig"] = config
        return body

    @staticmethod
    def usage(response):
        usage = response.get("usageMetadata") or {}
        return {"inputTokens": usage.get("promptTokenCount") or 0,
                "cachedTokens": usage.get("cachedContentTokenCount") or 0,
                "outputTokens": usage.get("candidatesTokenCount") or 0,
                "reasoningTokens": usage.get("thoughtsTokenCount") or 0}

    def read(self, response, request):
        candidates = response.get("candidates") or []
        parts = ((candidates[0].get("content") or {}).get("parts") or []) if candidates else []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        finish = candidates[0].get("finishReason") if candidates else None
        block = (response.get("promptFeedback") or {}).get("blockReason")
        refused = bool(block) or finish in self.REFUSALS or (not text.strip() and finish != "MAX_TOKENS")
        return answer(text, None, self.usage(response), response.get("modelVersion"), refused,
                      block or finish, response)

    def online(self, cfg, request):
        url = f"{self.API}/v1beta/models/{cfg['model']}:generateContent"
        _, response = send("POST", url, self.body(cfg, request), self.headers({"Accept": "application/json"}),
                           timeout=90, generation=True)
        return self.read(response, request)

    def find(self, cfg, label):
        """The Batch job a lost create may still have made under `label`, or None."""
        _, listing = send("GET", f"{self.API}/v1beta/batches?pageSize=100", headers=self.headers())
        for entry in listing.get("operations") or listing.get("batches") or []:
            if isinstance(entry, dict) and find(entry, "displayName") == label and entry.get("name"):
                return {"name": entry["name"], "file": find(entry, "fileName")}
        return None

    def upload(self, path, display):
        """The Files API's resumable upload: start, then one upload-and-finalize. Returns `files/…`."""
        size = os.path.getsize(path)
        headers, _ = send("POST", f"{self.API}/upload/v1beta/files", {"file": {"display_name": display}},
                          self.headers({"X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
                                        "X-Goog-Upload-Header-Content-Length": str(size),
                                        "X-Goog-Upload-Header-Content-Type": "application/jsonl"}),
                          attempts=1)
        target = headers.get("X-Goog-Upload-URL") or headers.get("x-goog-upload-url")
        with open(path, "rb") as fh:
            _, done = send("POST", target, fh.read(), self.headers({"X-Goog-Upload-Offset": "0",
                                                                    "X-Goog-Upload-Command": "upload, finalize",
                                                                    "Content-Type": "application/jsonl"}),
                           attempts=1)
        return done["file"]["name"]

    def submit(self, cfg, requests, label, source):
        """One Batch API job over `requests` (`{custom id: request}`), its input kept at `source`."""
        with open(source, "w", encoding="utf-8") as fh:
            for custom, request in requests.items():
                fh.write(json.dumps({"key": custom, "request": self.body(cfg, request)}, ensure_ascii=False) + "\n")
        file_name = self.upload(source, label)
        _, job = send("POST", f"{self.API}/v1beta/models/{cfg['model']}:batchGenerateContent",
                      {"batch": {"displayName": label, "inputConfig": {"fileName": file_name}}},
                      self.headers(), attempts=1)
        return {"name": job["name"], "file": file_name}

    def poll(self, cfg, job, requests):
        """`(state, {custom id: answer or {"error"}})`; state is running, done, failed or expired."""
        _, status = send("GET", f"{self.API}/v1beta/{job['name']}", headers=self.headers())
        state = find(status, "state") or "unknown"
        if state in ("BATCH_STATE_FAILED", "BATCH_STATE_CANCELLED", "JOB_STATE_FAILED"):
            return "failed", {"error": json.dumps(find(status, "error"))[:500]}
        if state == "BATCH_STATE_EXPIRED":
            return "expired", {}
        if state not in ("BATCH_STATE_SUCCEEDED", "JOB_STATE_SUCCEEDED"):
            return "running", {}
        responses = find(status, "responsesFile")
        _, raw = send("GET", f"{self.API}/download/v1beta/{responses}:download?alt=media",
                      headers=self.headers(), raw=True, timeout=600)
        out = {}
        for line in raw.decode("utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                custom = item.get("key")
                if custom in requests and custom not in out:
                    out[custom] = (self.read(item["response"], {}) if "response" in item
                                   else {"error": json.dumps(item.get("error") or item)[:300]})
        return "done", out


# --- OpenAI ---------------------------------------------------------------------------------------------

class OpenAI:
    API = "https://api.openai.com"
    KEY = "OPENAI_API_KEY"

    def headers(self, extra=None):
        return {"Authorization": f"Bearer {key(self.KEY)}", "Content-Type": "application/json", **(extra or {})}

    def body(self, cfg, request):
        messages = []
        if request.get("system"):
            messages.append({"role": "developer", "content": request["system"]})
        messages.append({"role": "user", "content": request["prompt"]})
        body = {"model": cfg["model"], "input": messages, "max_output_tokens": request["maxOutputTokens"],
                "store": False}
        if cfg.get("thinking"):
            body["reasoning"] = {"effort": cfg["thinking"]}
        if request.get("schema"):
            body["text"] = {"format": {"type": "json_schema", "name": request.get("name") or "answer",
                                       "schema": request["schema"], "strict": True}}
        elif request.get("json", True):
            body["text"] = {"format": {"type": "json_object"}}
        return body

    @staticmethod
    def usage(response):
        usage = response.get("usage") or {}
        reasoning = (usage.get("output_tokens_details") or {}).get("reasoning_tokens") or 0
        return {"inputTokens": usage.get("input_tokens") or 0,
                "cachedTokens": (usage.get("input_tokens_details") or {}).get("cached_tokens") or 0,
                "outputTokens": (usage.get("output_tokens") or 0) - reasoning, "reasoningTokens": reasoning}

    def read(self, response, request):
        text, refusal = "", None
        for item in response.get("output") or []:
            for part in item.get("content") or [] if item.get("type") == "message" else []:
                if part.get("type") == "output_text":
                    text += part.get("text") or ""
                elif part.get("type") == "refusal":
                    refusal = part.get("refusal") or "refusal"
        reason = (response.get("incomplete_details") or {}).get("reason")
        refused = refusal is not None or reason == "content_filter"
        return answer(text, None, self.usage(response), response.get("model"), refused,
                      refusal or reason or response.get("status"), response)

    def online(self, cfg, request):
        _, response = send("POST", f"{self.API}/v1/responses", self.body(cfg, request), self.headers(),
                           generation=True)
        return self.read(response, request)

    def find(self, cfg, label):
        """The Batch job a lost create may still have made under `label`, or None."""
        _, listing = send("GET", f"{self.API}/v1/batches?limit=100", headers=self.headers())
        for entry in listing.get("data") or []:
            if (entry.get("metadata") or {}).get("label") == label[:500]:
                return {"name": entry["id"], "file": entry.get("input_file_id")}
        return None

    def submit(self, cfg, requests, label, source):
        with open(source, "w", encoding="utf-8") as fh:
            for custom, request in requests.items():
                fh.write(json.dumps({"custom_id": custom, "method": "POST", "url": "/v1/responses",
                                     "body": self.body(cfg, request)}, ensure_ascii=False) + "\n")
        boundary = uuid.uuid4().hex
        with open(source, "rb") as fh:
            payload = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\nbatch\r\n"
                       f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{label}.jsonl\"\r\n"
                       f"Content-Type: application/jsonl\r\n\r\n").encode() + fh.read() + f"\r\n--{boundary}--\r\n".encode()
        _, uploaded = send("POST", f"{self.API}/v1/files", payload,
                           self.headers({"Content-Type": f"multipart/form-data; boundary={boundary}"}), attempts=1)
        _, job = send("POST", f"{self.API}/v1/batches",
                      {"input_file_id": uploaded["id"], "endpoint": "/v1/responses", "completion_window": "24h",
                       "metadata": {"label": label[:500]}}, self.headers(), attempts=1)
        return {"name": job["id"], "file": uploaded["id"]}

    def poll(self, cfg, job, requests):
        _, status = send("GET", f"{self.API}/v1/batches/{job['name']}", headers=self.headers())
        state = status.get("status")
        if state == "failed":
            return "failed", {"error": json.dumps(status.get("errors"))[:500]}
        # `cancelling` still runs and bills some requests, so it is read only once it has stopped; a cancelled
        # or expired job's output file holds what it did finish.
        if state not in ("completed", "expired", "cancelled"):
            return "running", {}
        out = {}
        if status.get("output_file_id"):
            _, raw = send("GET", f"{self.API}/v1/files/{status['output_file_id']}/content",
                          headers=self.headers(), raw=True, timeout=600)
            for line in raw.decode("utf-8").splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                custom = item.get("custom_id")
                response = item.get("response") or {}
                if custom in requests and custom not in out:
                    out[custom] = (self.read(response["body"], {})
                                   if response.get("status_code") == 200
                                   else {"error": json.dumps(item.get("error") or response)[:300]})
        return ("done" if state == "completed" else "expired"), out


# --- Anthropic ------------------------------------------------------------------------------------------

class Anthropic:
    API = "https://api.anthropic.com"
    KEY = "ANTHROPIC_API_KEY"
    TOOL = "answer"

    def headers(self):
        return {"x-api-key": key(self.KEY), "anthropic-version": "2023-06-01", "content-type": "application/json"}

    def body(self, cfg, request):
        body = {"model": cfg["model"], "max_tokens": request["maxOutputTokens"]}
        if request.get("system"):
            body["system"] = request["system"]
        body["messages"] = [{"role": "user", "content": request["prompt"]}]
        if request.get("schema"):
            # The schema is enforced as the input of the one tool the model is made to call.
            body["tools"] = [{"name": self.TOOL, "description": "Return the answer.",
                              "input_schema": request["schema"]}]
            body["tool_choice"] = {"type": "tool", "name": self.TOOL}
        # Extended thinking is off unless asked for: it bills as output and cannot run with a forced tool.
        body["thinking"] = ({"type": "enabled", "budget_tokens": int(cfg["thinking"])}
                            if str(cfg.get("thinking") or "").isdigit() else {"type": "disabled"})
        return body

    @staticmethod
    def usage(response):
        usage = response.get("usage") or {}
        cached = usage.get("cache_read_input_tokens") or 0
        return {"inputTokens": (usage.get("input_tokens") or 0) + cached
                + (usage.get("cache_creation_input_tokens") or 0),
                "cachedTokens": cached, "outputTokens": usage.get("output_tokens") or 0, "reasoningTokens": 0}

    def read(self, response, request):
        text, value = "", None
        for block in response.get("content") or []:
            if block.get("type") == "text":
                text += block.get("text") or ""
            elif block.get("type") == "tool_use" and block.get("name") == self.TOOL:
                value = block.get("input")
        if value is not None:
            text = json.dumps(value, ensure_ascii=False)
        stop = response.get("stop_reason")
        return answer(text, value, self.usage(response), response.get("model"), stop == "refusal", stop, response)

    def online(self, cfg, request):
        _, response = send("POST", f"{self.API}/v1/messages", self.body(cfg, request), self.headers(),
                           generation=True)
        return self.read(response, request)

    def find(self, cfg, label):
        """None: a Message Batch carries no label to find it by. No step submits to Anthropic in Batch mode
        (it is the fallback, asked online); configuring one accepts that a create whose response is lost can
        be made twice."""
        return None

    def submit(self, cfg, requests, label, source):
        # A custom id is `[a-zA-Z0-9_-]{1,64}`, which a title key (`movie:1`) is not, so they are numbered.
        customs = {f"r{n}": custom for n, custom in enumerate(requests)}
        lines = [{"custom_id": short, "params": self.body(cfg, requests[custom])} for short, custom in customs.items()]
        with open(source, "w", encoding="utf-8") as fh:
            fh.writelines(json.dumps(line, ensure_ascii=False) + "\n" for line in lines)
        _, job = send("POST", f"{self.API}/v1/messages/batches", {"requests": lines}, self.headers(), attempts=1)
        return {"name": job["id"], "customIds": customs}

    def poll(self, cfg, job, requests):
        _, status = send("GET", f"{self.API}/v1/messages/batches/{job['name']}", headers=self.headers())
        if status.get("processing_status") != "ended":
            return "running", {}
        _, raw = send("GET", status["results_url"], headers=self.headers(), raw=True, timeout=600)
        out = {}
        for line in raw.decode("utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            custom = job["customIds"].get(item.get("custom_id"))
            result = item.get("result") or {}
            if custom in requests and custom not in out:
                out[custom] = (self.read(result["message"], {}) if result.get("type") == "succeeded"
                               else {"error": json.dumps(result)[:300]})
        return "done", out


# --- the owner's plans, through their CLIs --------------------------------------------------------------

class CLI:
    """A model run through a CLI on the owner's monthly plan: prompt in, text out, no schema enforcement."""

    def command(self, cfg, prompt, out):
        raise NotImplementedError

    def online(self, cfg, request):
        if os.environ.get("GITHUB_ACTIONS") == "true":
            raise Unavailable(f"{cfg['provider']} runs on the owner's plan and never in the daily job")
        prompt = (request["system"] + "\n\n" if request.get("system") else "") + request["prompt"]
        if request.get("schema"):
            prompt += "\n\nReturn only JSON matching this schema: " + json.dumps(request["schema"])
        with tempfile.TemporaryDirectory() as directory:
            out = os.path.join(directory, "answer.txt")
            # Run outside the repo, so the CLI does not read this repo's agent instructions into the prompt.
            done = subprocess.run(self.command(cfg, prompt, out), capture_output=True, text=True, timeout=900,
                                  cwd=directory)
            if done.returncode:
                raise Unavailable(f"{cfg['provider']} exited {done.returncode}: {done.stderr[-300:]}")
            return self.read(done.stdout, out, request)

    def submit(self, cfg, requests, label, source):
        raise Unavailable(f"{cfg['provider']} has no Batch mode")


class ClaudeCLI(CLI):
    def command(self, cfg, prompt, out):
        return ["claude", "-p", prompt, "--model", cfg["model"], "--output-format", "json"]

    def read(self, stdout, _out, request):
        result = json.loads(stdout)
        text = result.get("result") or ""
        return answer(text, None, Anthropic.usage(result), result.get("model"),
                      result.get("stop_reason") == "refusal" or not text.strip(), result.get("stop_reason"), result)


class CodexCLI(CLI):
    def command(self, cfg, prompt, out):
        return ["codex", "exec", "--model", cfg["model"], "--skip-git-repo-check", "--output-last-message", out,
                prompt]

    def read(self, _stdout, out, request):
        with open(out, encoding="utf-8") as fh:
            text = fh.read()
        return answer(text, None, None, None, not text.strip(), None, None)


PROVIDERS = {"gemini": Gemini(), "openai": OpenAI(), "anthropic": Anthropic(),
             "claude-cli": ClaudeCLI(), "codex-cli": CodexCLI()}
