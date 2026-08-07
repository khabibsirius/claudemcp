"""Wrapper around a local Ollama model, specialised for getting JSON back.

Small local models are much worse than hosted ones at "reply with only
JSON", so this leans on three things in order: Ollama's own constrained JSON
decoding, tolerant parsing of what comes back, and a retry that shows the
model its own broken output.
"""

import json
import logging
import re

import ollama

from config import OLLAMA_HOST, OLLAMA_MODEL, OLLAMA_NUM_CTX, OLLAMA_TIMEOUT

log = logging.getLogger(__name__)


class OllamaError(Exception):
    """Ollama is unreachable, the model is missing, or it won't produce JSON."""


# A whole reply wrapped in a ``` / ```json fence.
_FENCE_RE = re.compile(r"\A\s*```(?:json)?\s*\n(?P<body>.*?)\n?\s*```\s*\Z", re.DOTALL)


def strip_code_fences(text):
    """Unwrap a fenced code block, if the entire reply is one.

    Anchored to the whole string on purpose. A line-by-line regex also
    matches backticks inside JSON string values and quietly corrupts
    otherwise-valid output.
    """
    text = (text or "").strip()
    match = _FENCE_RE.match(text)
    return match.group("body").strip() if match else text


def extract_json_object(text):
    """Pull the first complete top-level {...} out of a noisy reply.

    Last resort for models that insist on prefacing JSON with "Sure! Here's
    your dashboard:". Tracks string literals and escapes so a brace inside a
    field name doesn't end the object early. Returns None if there isn't a
    balanced object.
    """

    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]

    return None


def parse_json_reply(raw):
    """Best-effort parse of a model reply into a dict. Raises ValueError."""

    cleaned = strip_code_fences(raw)

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as first_error:
        candidate = extract_json_object(cleaned)
        if candidate is None:
            raise first_error
        parsed = json.loads(candidate)

    if not isinstance(parsed, dict):
        raise ValueError(
            f"expected a JSON object, got {type(parsed).__name__}"
        )

    return parsed


# Ollama names hosted models both ways - "kimi-k3:cloud" and
# "gpt-oss:120b-cloud" - so both separators have to be matched. Missing one
# meant a hosted model was reported as local, and because hosted models list
# a size of 0 GB it sorted first and became the automatic pick: data would
# have gone off the machine by default.
_CLOUD_HINTS = (":cloud", "-cloud")

# Tiny models accept tools and then invent the arguments.
_TOO_SMALL_HINTS = (":0.5b", ":1b", ":1.5b", "-1b")


def _model_names(client):
    try:
        listing = client.list()
    except Exception as e:
        raise OllamaError(f"Could not reach Ollama: {e}. Is `ollama serve` running?") from e

    models = listing.get("models", []) if isinstance(listing, dict) else listing.models
    names = []
    for model in models:
        name = (
            model.get("model") or model.get("name")
            if isinstance(model, dict) else getattr(model, "model", None)
        )
        if name:
            names.append(name)
    return names


def model_capabilities(client, model):
    """What a model can do, per Ollama, without loading it into memory."""
    try:
        info = client.show(model)
    except Exception as e:
        log.debug("Could not describe %s: %s", model, e)
        return []
    capabilities = (
        info.get("capabilities") if isinstance(info, dict)
        else getattr(info, "capabilities", None)
    )
    return list(capabilities or [])


def supports_tools(client, model):
    """Whether a model can call tools.

    Read from the model's declared capabilities rather than by attempting a
    call: probing means loading each candidate into memory, which for a
    26B model is tens of seconds and gigabytes.
    """
    return "tools" in model_capabilities(client, model)


def tool_capable_models(client):
    """Installed models that can call tools, best candidates first."""
    def rank(name):
        lowered = name.lower()
        return (
            any(h in lowered for h in _CLOUD_HINTS),
            any(h in lowered for h in _TOO_SMALL_HINTS),
            name,
        )

    return sorted(
        (name for name in _model_names(client) if supports_tools(client, name)),
        key=rank,
    )


def describe_models(client):
    """Every installed model, with size and whether it can drive the assistant.

    Sorted usable-first, then smallest-first: on a machine also running Qlik,
    a model that fits alongside it matters more than one that scores better
    but won't load.
    """
    try:
        listing = client.list()
    except Exception as e:
        raise OllamaError(f"Could not reach Ollama: {e}. Is `ollama serve` running?") from e

    raw = listing.get("models", []) if isinstance(listing, dict) else listing.models

    models = []
    for entry in raw:
        if isinstance(entry, dict):
            name = entry.get("model") or entry.get("name")
            size = entry.get("size") or 0
        else:
            name = getattr(entry, "model", None)
            size = getattr(entry, "size", 0) or 0
        if not name:
            continue

        lowered = name.lower()
        cloud = any(hint in lowered for hint in _CLOUD_HINTS)
        tiny = any(hint in lowered for hint in _TOO_SMALL_HINTS)
        tools = "tools" in model_capabilities(client, name)

        if not tools:
            reason = "cannot call tools"
        elif cloud:
            # Not blocked: some cloud models are free with an account and are
            # far faster than a large local model on a busy machine. But this
            # is the one choice that sends the data off the machine, so it
            # says so and never wins the automatic pick.
            reason = "cloud - runs on Ollama's servers, your data leaves this machine"
        elif tiny:
            reason = "very small - tends to invent tool arguments"
        else:
            reason = ""

        models.append({
            "name": name,
            "size_gb": round(size / 1e9, 1),
            "tools": tools,
            "cloud": cloud,
            "usable": tools,
            "recommended": tools and not cloud and not tiny,
            "reason": reason,
        })

    models.sort(key=lambda m: (not m["recommended"], not m["usable"], m["size_gb"]))
    return models


def pick_tool_model(client, preferred):
    """Choose a model that can actually drive the assistant.

    Returns (model, note). Falls back to another installed model rather than
    refusing to start, because the configured model is usually the one chosen
    for dashboard design - which needs JSON, not tools - and a person should
    not have to know which models support what.
    """
    if preferred and supports_tools(client, preferred):
        return preferred, ""

    candidates = tool_capable_models(client)
    if not candidates:
        raise OllamaError(
            f"{preferred!r} cannot call tools, and no installed model can. "
            "Install one, for example: ollama pull qwen3"
        )

    chosen = candidates[0]
    return chosen, (
        f"{preferred!r} cannot call tools, so using {chosen!r} instead. "
        f"Set CHAT_MODEL in .env to choose. Also available: "
        f"{', '.join(candidates[1:4]) or 'none'}"
    )


class OllamaClient:

    def __init__(self, model=OLLAMA_MODEL, host=None, timeout=None, temperature=0.2):
        self.model = model
        self.host = host if host is not None else OLLAMA_HOST
        self.timeout = timeout if timeout is not None else OLLAMA_TIMEOUT
        # Design work wants consistency far more than flair, and a hot model
        # invents field names.
        self.temperature = temperature
        self._client = ollama.Client(host=self.host or None, timeout=self.timeout)

    # ------------------------------------------------------------------

    def _chat(self, messages, json_mode=False):
        try:
            response = self._client.chat(
                model=self.model,
                messages=messages,
                # Constrained decoding - the model can only emit valid JSON.
                # Far more reliable than asking nicely in the prompt.
                format="json" if json_mode else None,
                # num_ctx on every call: without it Ollama uses its own small
                # default and silently truncates the front of the prompt -
                # the field list this whole design step depends on.
                options={
                    "temperature": self.temperature,
                    "num_ctx": OLLAMA_NUM_CTX,
                },
            )
        except ollama.ResponseError as e:
            raise OllamaError(self._response_error_hint(e)) from e
        except (ConnectionError, TimeoutError, OSError) as e:
            raise OllamaError(
                f"Could not reach Ollama at {self.host or 'its default host'}: {e}. "
                "Is `ollama serve` running?"
            ) from e

        return response["message"]["content"]

    def _response_error_hint(self, error):
        text = str(error)
        lowered = text.lower()
        if "subscription" in lowered or "upgrade" in lowered:
            return (
                f"{self.model} is a cloud model that needs a paid Ollama "
                "subscription. Pick a local model, or one of the cloud models "
                "included with a free account."
            )
        if "not found" in lowered:
            return (
                f"Ollama doesn't have the model {self.model!r}. "
                f"Pull it first: ollama pull {self.model}"
            )
        return f"Ollama rejected the request: {text}"

    # ------------------------------------------------------------------

    def available_models(self):
        """Model names Ollama has pulled locally."""
        try:
            response = self._client.list()
        except Exception as e:
            raise OllamaError(
                f"Could not reach Ollama at {self.host or 'its default host'}: {e}. "
                "Is `ollama serve` running?"
            ) from e

        models = response.get("models", []) if isinstance(response, dict) else response.models
        names = []
        for model in models:
            name = model.get("model") or model.get("name") if isinstance(model, dict) else getattr(model, "model", None)
            if name:
                names.append(name)
        return names

    def check(self):
        """Verify Ollama is up and the configured model is pulled.

        Returns a status string; raises OllamaError if it can't be used.
        """
        names = self.available_models()
        if self.model in names:
            return f"Ollama is up, model {self.model!r} is available."

        # Ollama reports "phi4:14b"; users often configure "phi4".
        bare = [n.split(":", 1)[0] for n in names]
        if self.model.split(":", 1)[0] in bare:
            return (
                f"Ollama is up. Model {self.model!r} isn't an exact match for "
                f"anything pulled ({', '.join(names)}) but a same-family tag is."
            )

        raise OllamaError(
            f"Ollama is up but {self.model!r} isn't pulled. "
            f"Available: {', '.join(names) or '(none)'}. "
            f"Run: ollama pull {self.model}"
        )

    # ------------------------------------------------------------------

    def ask(self, prompt, system=None):
        """One free-text turn."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return self._chat(messages)

    def ask_json(self, prompt, system=None, retries=2):
        """Ask for a JSON object and return it as a dict.

        Retries keep the full conversation, so the correction turn actually
        has the original request and the model's own broken reply in front of
        it. Sending only "that wasn't valid JSON" - with no history, as a
        fresh chat call - gives the model nothing to correct.
        """

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        last_error = None
        last_raw = None

        for attempt in range(retries + 1):
            raw = self._chat(messages, json_mode=True)
            last_raw = raw

            try:
                return parse_json_reply(raw)
            except (json.JSONDecodeError, ValueError) as e:
                last_error = e
                log.warning(
                    "Model reply wasn't usable JSON (attempt %d/%d): %s",
                    attempt + 1, retries + 1, e,
                )
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content": (
                    f"That reply could not be parsed as JSON: {e}. "
                    "Reply again with the corrected JSON object only - no "
                    "prose, no markdown fences."
                )})

        raise OllamaError(
            f"{self.model} did not return valid JSON after {retries + 1} attempts. "
            f"Last error: {last_error}. Last reply: {last_raw!r}"
        )
