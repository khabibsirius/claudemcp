import json
import sys

import llm
from config import OPENAI_BASE_URL, OPENAI_MODEL

OK = "  ok   "
FAIL = " FAIL  "

PROBE_TOOL = [{
    "type": "function",
    "function": {
        "name": "create_chart",
        "description": "Create a chart in the open Qlik app. Use this whenever the "
                       "user asks for a chart, table, or visualisation.",
        "parameters": {
            "type": "object",
            "properties": {
                "chart_type": {"type": "string"},
                "title": {"type": "string"},
                "dimensions": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["chart_type", "title"],
        },
    },
}]

ASK = [
    {"role": "system",
     "content": "You build charts in Qlik. When the user asks for one, call the "
                "create_chart tool. Never describe a chart in text instead."},
    {"role": "user",
     "content": "Draw me a table of Branch and Amount."},
]


def calls_in(message):
    calls = message.get("tool_calls") or []
    named = []
    for call in calls:
        function = call.get("function") or {}
        name = getattr(function, "name", None) or function.get("name")
        if name:
            named.append(name)
    return named


def main():
    print(f"endpoint : {OPENAI_BASE_URL}")
    print(f"model    : {OPENAI_MODEL}\n")

    client = llm.build_client()

    print("1. Does the endpoint list this model?")
    try:
        listed = [m.get("model") for m in client.list().get("models", [])]
        here = OPENAI_MODEL in listed
        print(f"{OK if here else FAIL} {len(listed)} model(s) offered; "
              f"{OPENAI_MODEL!r} {'is' if here else 'is NOT'} among them")
        if listed:
            print(f"        {', '.join(str(m) for m in listed[:12])}")
    except Exception as e:
        print(f"{FAIL} could not list models: {e}")

    print("\n2. Non-streaming: does it emit a tool call?")
    got_blocking = []
    try:
        response = client.chat(model=OPENAI_MODEL, messages=list(ASK),
                               tools=PROBE_TOOL, stream=False)
        message = response.get("message") or {}
        got_blocking = calls_in(message)
        text = (message.get("content") or "").strip()
        if got_blocking:
            print(f"{OK} called {got_blocking}")
        else:
            print(f"{FAIL} no tool call. It replied with text instead:")
            print(f"        {text[:300]!r}")
    except Exception as e:
        print(f"{FAIL} request failed: {e}")

    print("\n3. Streaming (what the web app actually uses):")
    got_stream = []
    text = []
    try:
        for chunk in client.chat(model=OPENAI_MODEL, messages=list(ASK),
                                 tools=PROBE_TOOL, stream=True):
            message = chunk.get("message") or {}
            got_stream += calls_in(message)
            piece = message.get("content")
            if piece:
                text.append(piece)
        if got_stream:
            print(f"{OK} called {got_stream}")
        else:
            print(f"{FAIL} no tool call. It streamed text instead:")
            print(f"        {''.join(text)[:300]!r}")
    except Exception as e:
        print(f"{FAIL} stream failed: {e}")

    print()
    if got_blocking and got_stream:
        print("This endpoint calls tools in both modes. The assistant should be "
              "able to build charts - the problem is further in.")
        return 0
    if got_blocking and not got_stream:
        print("Tool calls work when NOT streaming but are lost when streaming. "
              "The web app streams, so nothing ever reaches Qlik.")
        return 1
    print("This endpoint does not emit tool calls for this model. The assistant "
          "can only describe a table in the chat - it can never build one in "
          "Qlik. Ask whoever runs the endpoint whether tool/function calling is "
          "enabled for this model, or point OPENAI_MODEL at one that supports it.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
