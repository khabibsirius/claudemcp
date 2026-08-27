import json
import logging
import threading

import httpx

from config import (
    LLM_MAX_CONNECTIONS,
    LLM_MAX_KEEPALIVE,
    LLM_PROVIDER,
    OLLAMA_HOST,
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    OPENAI_EXTRA_BODY,
    OPENAI_EXTRA_HEADERS,
    OPENAI_MODEL,
    OPENAI_ORGANISATION,
    OPENAI_CONNECT_TIMEOUT,
    OPENAI_TIMEOUT,
    OPENAI_VERIFY_SSL,
)
from ollama_client import OllamaError

log = logging.getLogger(__name__)

OLLAMA = "ollama"
OPENAI = "openai"

THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"


def strip_thinking(text):
    if not text or THINK_OPEN not in text:
        return text

    out = []
    rest = text
    while THINK_OPEN in rest:
        before, _, after = rest.partition(THINK_OPEN)
        out.append(before)
        if THINK_CLOSE not in after:
            rest = ""
            break
        _, _, rest = after.partition(THINK_CLOSE)
    out.append(rest)
    return "".join(out).lstrip()


class ThinkFilter:
    def __init__(self):
        self._buffer = ""
        self._inside = False

    def feed(self, text):
        self._buffer += text
        out = []

        while True:
            if self._inside:
                index = self._buffer.find(THINK_CLOSE)
                if index == -1:
                    self._buffer = self._buffer[-(len(THINK_CLOSE) - 1):]
                    break
                self._buffer = self._buffer[index + len(THINK_CLOSE):]
                self._inside = False
                continue

            index = self._buffer.find(THINK_OPEN)
            if index == -1:
                safe = len(self._buffer) - (len(THINK_OPEN) - 1)
                if safe > 0:
                    out.append(self._buffer[:safe])
                    self._buffer = self._buffer[safe:]
                break

            out.append(self._buffer[:index])
            self._buffer = self._buffer[index + len(THINK_OPEN):]
            self._inside = True

        return "".join(out)

    def flush(self):
        if self._inside:
            self._buffer = ""
            return ""
        tail, self._buffer = self._buffer, ""
        return tail


class OpenAICompatibleClient:
    def __init__(self, base_url=None, api_key=None, timeout=None,
                 organisation=None, extra_headers=None, verify=None,
                 default_model=None, connect_timeout=None):
        self.base_url = (base_url or OPENAI_BASE_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else OPENAI_API_KEY
        self.default_model = default_model or OPENAI_MODEL
        self.timeout = timeout or OPENAI_TIMEOUT
        self.connect_timeout = min(connect_timeout or OPENAI_CONNECT_TIMEOUT,
                                   self.timeout)

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if organisation or OPENAI_ORGANISATION:
            headers["OpenAI-Organization"] = organisation or OPENAI_ORGANISATION
        headers.update(extra_headers or OPENAI_EXTRA_HEADERS)

        verify = OPENAI_VERIFY_SSL if verify is None else verify
        if not verify:
            log.warning(
                "OPENAI_VERIFY_SSL is off - the model endpoint's certificate "
                "is not being verified."
            )

        self._http = httpx.Client(
            headers=headers, verify=verify,
            timeout=httpx.Timeout(self.timeout, connect=self.connect_timeout),
            limits=httpx.Limits(
                max_connections=LLM_MAX_CONNECTIONS,
                max_keepalive_connections=LLM_MAX_KEEPALIVE,
            ),
        )

    def close(self):
        self._http.close()

    @staticmethod
    def _to_openai_messages(messages):
        out = []
        pending = []

        for message in messages:
            role = message.get("role")

            if role == "tool":
                name = message.get("tool_name") or message.get("name") or ""
                call_id = None
                for index, (queued_id, queued_name) in enumerate(pending):
                    if queued_name == name:
                        call_id = queued_id
                        pending.pop(index)
                        break
                if call_id is None and pending:
                    call_id = pending.pop(0)[0]
                out.append({
                    "role": "tool",
                    "tool_call_id": call_id or f"call_{len(out)}",
                    "content": message.get("content") or "",
                })
                continue

            calls = message.get("tool_calls") or []
            if role == "assistant" and calls:
                converted = []
                for index, call in enumerate(calls):
                    function = call.get("function") or {}
                    name = function.get("name") or ""
                    arguments = function.get("arguments")
                    if not isinstance(arguments, str):
                        arguments = json.dumps(arguments or {}, default=str)
                    call_id = call.get("id") or f"call_{len(out)}_{index}"
                    pending.append((call_id, name))
                    converted.append({
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": arguments},
                    })
                out.append({
                    "role": "assistant",
                    "content": message.get("content") or None,
                    "tool_calls": converted,
                })
                continue

            out.append({"role": role, "content": message.get("content") or ""})

        return out

    @staticmethod
    def _from_openai_calls(calls):
        plain = []
        for call in calls or []:
            function = call.get("function") or {}
            plain.append({
                "id": call.get("id"),
                "function": {
                    "name": function.get("name") or "",
                    "arguments": function.get("arguments") or "{}",
                },
            })
        return plain

    @staticmethod
    def _payload_options(options):
        options = options or {}
        payload = {}
        if "temperature" in options:
            payload["temperature"] = options["temperature"]
        if "top_p" in options:
            payload["top_p"] = options["top_p"]
        if options.get("num_predict", 0) > 0:
            payload["max_tokens"] = options["num_predict"]
        if options.get("seed") is not None:
            payload["seed"] = options["seed"]
        return payload

    def chat(self, model=None, messages=None, tools=None, options=None,
             stream=False, **_ignored):
        body = {
            "model": model or self.default_model,
            "messages": self._to_openai_messages(messages or []),
            "stream": bool(stream),
            **self._payload_options(options),
        }
        if tools:
            body["tools"] = tools
        body.update(OPENAI_EXTRA_BODY)

        if stream:
            return self._stream(body)
        return self._once(body)

    def _once(self, body):
        data = self._request(body)
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}

        thinking = message.get("reasoning_content")
        if thinking:
            log.debug("Model reasoning (%d chars) discarded", len(thinking))

        return {
            "message": {
                "role": "assistant",
                "content": strip_thinking(message.get("content") or ""),
                "tool_calls": self._from_openai_calls(message.get("tool_calls")),
            },
            "done": True,
            "done_reason": choice.get("finish_reason"),
        }

    def _request(self, body):
        try:
            response = self._http.post(f"{self.base_url}/chat/completions",
                                       json=body)
        except httpx.HTTPError as e:
            raise OllamaError(self._unreachable(e)) from e

        if response.status_code >= 400:
            raise OllamaError(self._api_error(response))
        try:
            return response.json()
        except ValueError as e:
            raise OllamaError(
                f"{self.base_url} did not return JSON: {response.text[:200]}"
            ) from e

    def _stream(self, body):
        building = {}
        thinking = ThinkFilter()
        try:
            with self._http.stream(
                "POST", f"{self.base_url}/chat/completions", json=body
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    raise OllamaError(self._api_error(response))

                for line in response.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        data = json.loads(payload)
                    except ValueError:
                        log.debug("Skipping unparseable stream chunk: %r", payload[:120])
                        continue

                    delta = ((data.get("choices") or [{}])[0].get("delta")) or {}

                    text = delta.get("content")
                    if text:
                        visible = thinking.feed(text)
                        if visible:
                            yield {"message": {"role": "assistant",
                                               "content": visible,
                                               "tool_calls": []}, "done": False}

                    for fragment in delta.get("tool_calls") or []:
                        index = fragment.get("index", 0)
                        slot = building.setdefault(
                            index, {"id": None, "name": "", "arguments": ""})
                        if fragment.get("id"):
                            slot["id"] = fragment["id"]
                        function = fragment.get("function") or {}
                        if function.get("name"):
                            slot["name"] += function["name"]
                        if function.get("arguments"):
                            slot["arguments"] += function["arguments"]
        except httpx.HTTPError as e:
            raise OllamaError(self._unreachable(e)) from e

        calls = [
            {"id": slot["id"],
             "function": {"name": slot["name"], "arguments": slot["arguments"] or "{}"}}
            for _, slot in sorted(building.items())
            if slot["name"]
        ]
        yield {
            "message": {"role": "assistant", "content": thinking.flush(),
                        "tool_calls": calls},
            "done": True,
        }

    def list(self):
        try:
            response = self._http.get(f"{self.base_url}/models")
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as e:
            log.debug("Could not list models at %s: %s", self.base_url, e)
            if not self.default_model:
                raise OllamaError(self._unreachable(e)) from e
            return {"models": [{"model": self.default_model, "size": 0}]}

        models = [
            {"model": entry.get("id") or entry.get("name"), "size": 0}
            for entry in (data.get("data") or data.get("models") or [])
            if entry.get("id") or entry.get("name")
        ]
        if not models and self.default_model:
            models = [{"model": self.default_model, "size": 0}]
        return {"models": models}

    def show(self, model):
        return {"capabilities": ["completion", "tools"]}

    def _unreachable(self, error):
        return (
            f"Could not reach the model endpoint at {self.base_url}: {error}\n"
            "Check OPENAI_BASE_URL, that the host is reachable from this "
            "machine, and that OPENAI_API_KEY is set if the endpoint needs one."
        )

    def _api_error(self, response):
        detail = ""
        try:
            body = response.json()
            detail = (body.get("error") or {}).get("message") or json.dumps(body)[:300]
        except ValueError:
            detail = (response.text or "")[:300]

        if response.status_code == 401:
            return f"The model endpoint rejected the API key (401). {detail}"
        if response.status_code == 404:
            return (
                f"The model endpoint has no such model or path (404). {detail}\n"
                f"Check OPENAI_MODEL and that OPENAI_BASE_URL ends at the "
                f"version prefix, e.g. https://api.openai.com/v1"
            )
        if response.status_code == 429:
            return f"The model endpoint is rate limiting this account (429). {detail}"
        return f"The model endpoint returned {response.status_code}. {detail}"


def provider():
    name = (LLM_PROVIDER or OLLAMA).strip().lower()
    if name in ("openai", "openai-compatible", "openrouter", "azure", "vllm"):
        return OPENAI
    return OLLAMA


_shared_lock = threading.Lock()
_shared_client = None


def build_client():
    global _shared_client

    if provider() == OPENAI:
        if not OPENAI_BASE_URL:
            raise OllamaError(
                "LLM_PROVIDER is set to an OpenAI-compatible endpoint but "
                "OPENAI_BASE_URL is empty. Set it to the API root, e.g. "
                "https://api.openai.com/v1"
            )
        with _shared_lock:
            if _shared_client is None:
                _shared_client = OpenAICompatibleClient()
            return _shared_client

    import ollama

    return ollama.Client(host=OLLAMA_HOST or None)


def close_shared():
    global _shared_client

    with _shared_lock:
        if _shared_client is not None:
            try:
                _shared_client.close()
            except Exception:
                log.debug("Could not close the model client", exc_info=True)
            _shared_client = None


def default_model():
    if provider() == OPENAI:
        return OPENAI_MODEL
    from config import CHAT_MODEL

    return CHAT_MODEL
