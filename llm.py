"""The model behind the assistant, wherever it happens to live.

Everything above this module calls one object with three methods - `chat`,
`list` and `show` - and that object was the `ollama` package's client. This
adds a second implementation of the same three methods that speaks the
OpenAI `/v1/chat/completions` format, so the same assistant runs against
OpenAI, Azure OpenAI, OpenRouter, Together, Groq, or a vLLM or TGI endpoint
inside the bank's own network. They all speak that format; what differs is a
base URL and a key.

The shape it has to match is Ollama's, not OpenAI's, because Ollama's is what
`chat_tools.py` already reads:

    response["message"]["content"]
    response["message"]["tool_calls"] -> [{"function": {"name", "arguments"}}]

so the translation happens here rather than being sprayed through the agent
loop. Three differences carry all the work:

- **Tool call ids.** OpenAI pairs each call with an id and expects the
  result to quote it. Ollama has no ids and matches by position. Outgoing
  messages therefore have ids invented for them, and the tool results that
  follow are matched back to the call they answer.
- **Arguments are a string.** OpenAI sends the arguments as a JSON *string*;
  Ollama sends a dict. `_parse_arguments` in chat_tools already accepts
  either, so incoming calls are passed through as they arrive.
- **Streaming tool calls arrive in fragments.** OpenAI streams a tool call
  a few characters at a time across many chunks. The agent loop expects
  whole calls, the way Ollama sends them, so they are accumulated here and
  emitted once at the end.

Why this matters beyond running somewhere else: readiness.md records the
assistant getting its own arithmetic wrong (R1) and taking four minutes to
answer (O1). Both are consequences of a small quantised model on a shared
GPU, and neither is fixed by prompting.
"""

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

# Reasoning models think out loud before answering. Ollama keeps that apart
# from the reply on its own; an OpenAI-compatible endpoint does one of two
# things instead, and neither is handled by anything above this module:
#
#   - a `reasoning_content` field alongside `content` (DashScope, and vLLM
#     started with a reasoning parser), which is simply ignored here, or
#   - `<think>...</think>` inline in `content` (vLLM without one), which
#     would otherwise be shown to the user as part of the answer.
#
# Qwen3 does this by default, and the monologue is long: several hundred
# words of "the user is asking about deposits, let me consider..." in front
# of every reply, and worse, in front of the JSON that the dashboard designer
# has to parse.
THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"


def strip_thinking(text):
    """Remove any complete <think>...</think> blocks from a finished reply.

    An unterminated block - a reply cut off mid-thought - is dropped from the
    open tag onwards, because whatever follows it never arrived.
    """
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
    """Drop the monologue from a stream, where the tags arrive in pieces.

    A token boundary can fall anywhere, including the middle of `<think>`,
    so text is held back until it is known not to be the start of a tag.
    That costs a few characters of latency at the tail of each chunk and is
    the only way to avoid emitting half an opening tag to the browser.
    """

    def __init__(self):
        self._buffer = ""
        self._inside = False

    def feed(self, text):
        """The part of `text` that is really the answer, so far."""
        self._buffer += text
        out = []

        while True:
            if self._inside:
                index = self._buffer.find(THINK_CLOSE)
                if index == -1:
                    # Still thinking. Keep only enough to recognise a closing
                    # tag split across this chunk and the next.
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
        """Whatever is left once the stream ends."""
        if self._inside:
            # The reply stopped mid-thought; there is no answer in there.
            self._buffer = ""
            return ""
        tail, self._buffer = self._buffer, ""
        return tail


class OpenAICompatibleClient:
    """An Ollama-shaped client over the OpenAI chat-completions API."""

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
            # An internal endpoint behind the bank's own CA. Worth saying out
            # loud: the server is no longer authenticated, so this is for a
            # network you already trust, not for the open internet.
            log.warning(
                "OPENAI_VERIFY_SSL is off - the model endpoint's certificate "
                "is not being verified."
            )

        # One pool, sized for the number of turns that can run at once. Left
        # at httpx's default the pool itself becomes the queue: turns wait on
        # a free connection rather than on the model, which looks like the
        # model being slow.
        self._http = httpx.Client(
            headers=headers, verify=verify,
            # connect short, read long - see OPENAI_CONNECT_TIMEOUT.
            timeout=httpx.Timeout(self.timeout, connect=self.connect_timeout),
            limits=httpx.Limits(
                max_connections=LLM_MAX_CONNECTIONS,
                max_keepalive_connections=LLM_MAX_KEEPALIVE,
            ),
        )

    def close(self):
        self._http.close()

    # ------------------------------------------------------------------
    # Translation
    # ------------------------------------------------------------------

    @staticmethod
    def _to_openai_messages(messages):
        """Ollama's message list as OpenAI wants it.

        Tool results are matched to the call they answer: OpenAI rejects a
        `tool` message whose `tool_call_id` names no preceding call, and
        Ollama's format carries only a name, so the pairing is rebuilt from
        the order the calls were made in.
        """
        out = []
        pending = []          # ids of calls not yet answered, in order

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
                    # Named something we never called - answer the oldest
                    # outstanding one rather than dropping the result, which
                    # would leave the model waiting for it forever.
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
                    # None rather than "": some endpoints reject an empty
                    # string alongside tool calls.
                    "content": message.get("content") or None,
                    "tool_calls": converted,
                })
                continue

            out.append({"role": role, "content": message.get("content") or ""})

        return out

    @staticmethod
    def _from_openai_calls(calls):
        """OpenAI tool calls in the shape the agent loop reads."""
        plain = []
        for call in calls or []:
            function = call.get("function") or {}
            plain.append({
                "id": call.get("id"),
                "function": {
                    "name": function.get("name") or "",
                    # Left as the string it arrived as: _parse_arguments
                    # accepts either, and re-parsing here would only move
                    # where a malformed argument list is discovered.
                    "arguments": function.get("arguments") or "{}",
                },
            })
        return plain

    @staticmethod
    def _payload_options(options):
        """Ollama's `options` as OpenAI parameters.

        `num_ctx` has no equivalent and is dropped on purpose: the context
        window is the endpoint's property, not the caller's, and a hosted
        model's is far larger than anything this would ask for.
        """
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

    # ------------------------------------------------------------------
    # The three methods everything above expects
    # ------------------------------------------------------------------

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
        # Whatever a particular endpoint wants that is not in the standard:
        # Qwen served by vLLM or DashScope takes
        # {"chat_template_kwargs": {"enable_thinking": false}}, which stops
        # the monologue being generated at all rather than stripping it after
        # the fact - cheaper and faster than paying for tokens nobody reads.
        body.update(OPENAI_EXTRA_BODY)

        if stream:
            return self._stream(body)
        return self._once(body)

    def _once(self, body):
        data = self._request(body)
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}

        # `reasoning_content` is the monologue kept in a field of its own by
        # endpoints that separate it. Useful when something goes wrong, never
        # part of the answer.
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
        """Yield Ollama-shaped chunks, with whole tool calls at the end.

        OpenAI streams a tool call in fragments - the name in one chunk, the
        arguments a few characters at a time across many more. The agent loop
        expects whole calls, so they are assembled here and emitted once the
        stream finishes.
        """
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
                        # Held back a few characters at a time, because a
                        # token boundary can fall inside "<think>" and half
                        # an opening tag must never reach the browser.
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
        """Models the endpoint offers, in the shape describe_models reads.

        An endpoint that does not implement /v1/models - several
        self-hosted ones serve exactly one model and skip it - still has to
        produce a usable answer, so the configured model is reported rather
        than an empty picker that looks like a fault.
        """
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
        """Capabilities, which this API has no way to report.

        Ollama can say whether a model supports tool calling without loading
        it, and `pick_tool_model` uses that to avoid choosing one that will
        return a 400 on the first tool call. A hosted endpoint offers no such
        introspection, so tool support is assumed - which is right for every
        model anyone would put behind this, and wrong loudly rather than
        silently if it isn't: the first call fails with the provider's own
        error rather than the model quietly ignoring the tools.
        """
        return {"capabilities": ["completion", "tools"]}

    # ------------------------------------------------------------------

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


# ----------------------------------------------------------------------


def provider():
    """Which client the configuration asks for."""
    name = (LLM_PROVIDER or OLLAMA).strip().lower()
    if name in ("openai", "openai-compatible", "openrouter", "azure", "vllm"):
        return OPENAI
    return OLLAMA


# One HTTP client for the whole process rather than one per person.
#
# Sessions are per user, and a client per session would mean a connection
# pool per user - hundreds of separate pools, each paying its own TLS
# handshake and none of them reusing a connection anybody else opened. The
# client itself holds no per-user state: the key and the model are the same
# for everyone, and which model to use is passed on every call.
_shared_lock = threading.Lock()
_shared_client = None


def build_client():
    """The client the rest of the app talks to.

    Imported lazily so a deployment using a hosted endpoint does not need the
    ollama package installed, and the reverse.
    """
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

    # Ollama's own client is cheap and local; there is nothing to share.
    return ollama.Client(host=OLLAMA_HOST or None)


def close_shared():
    """Close the shared pool on shutdown. Safe to call more than once."""
    global _shared_client

    with _shared_lock:
        if _shared_client is not None:
            try:
                _shared_client.close()
            except Exception:
                log.debug("Could not close the model client", exc_info=True)
            _shared_client = None


def default_model():
    """The model to start from, whichever provider is configured."""
    if provider() == OPENAI:
        return OPENAI_MODEL
    from config import CHAT_MODEL

    return CHAT_MODEL
