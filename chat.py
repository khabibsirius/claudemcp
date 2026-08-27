import argparse
import json
import logging
import sys

import ollama

import session
from chat_tools import execute, run_agent, system_prompt
from config import APP_NAME, CHAT_MODEL, CHAT_MAX_STEPS
from ollama_client import OllamaError
from qlik_engine import QlikEngine, QlikEngineError

log = logging.getLogger(__name__)

MAX_STEPS = CHAT_MAX_STEPS

BANNER = """Qlik assistant. Ask in plain language, for example:

  load every csv in my downloads folder and merge them
  what is total sales by region?
  clean the data - drop the columns that are always the same
  build me a sales dashboard

Ctrl-C or 'exit' to quit.
"""


def describe(name, arguments):
    if not arguments:
        return name
    interesting = {
        k: v for k, v in arguments.items()
        if k not in ("content",) and v not in ("", None, [], {})
    }
    if "content" in arguments:
        interesting["content"] = f"<{len(str(arguments['content']))} chars>"
    if "charts" in arguments and isinstance(arguments["charts"], list):
        interesting["charts"] = f"{len(arguments['charts'])} chart(s)"
    rendered = ", ".join(f"{k}={json.dumps(v)[:60]}" for k, v in interesting.items())
    return f"{name}({rendered})"


def confirm(question, action=None):
    try:
        return input(f"  {question} [y/N] ").strip().lower().startswith("y")
    except EOFError:
        return False


def run_tool(engine, name, arguments, assume_yes=False):
    return execute(
        engine, name, arguments,
        confirm=(lambda question, action: True) if assume_yes else confirm,
    )


def answer(client, model, engine, messages, assume_yes=False):
    return run_agent(
        client, model, engine, messages,
        on_call=lambda name, args: print(f"  -> {describe(name, args)}"),
        confirm=(lambda question, action: True) if assume_yes else confirm,
        max_steps=MAX_STEPS,
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Talk to your Qlik app in plain language.")
    parser.add_argument("--app", default=APP_NAME, help=f"App to open (default: {APP_NAME!r})")
    parser.add_argument("--model", default=CHAT_MODEL, help=f"Ollama model (default: {CHAT_MODEL!r})")
    parser.add_argument("--yes", action="store_true", help="Don't ask before reloading data")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        model, note = session.resolve_model(args.model)
    except OllamaError as e:
        print(e, file=sys.stderr)
        return 1
    if note:
        print(f"note: {note}\n", file=sys.stderr)

    client = session.client()

    try:
        engine = QlikEngine()
    except QlikEngineError as e:
        print(f"Can't reach Qlik: {e}", file=sys.stderr)
        return 1

    with engine:
        try:
            engine.open_app(args.app)
        except QlikEngineError as e:
            print(f"Can't open {args.app!r}: {e}", file=sys.stderr)
            return 1

        print(BANNER)
        print(f"Connected to {args.app!r} using {model}.\n")

        messages = [{"role": "system", "content": system_prompt()}]

        while True:
            try:
                user = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not user:
                continue
            if user.lower() in ("exit", "quit"):
                break

            messages.append({"role": "user", "content": user})

            try:
                reply = answer(client, model, engine, messages, assume_yes=args.yes)
            except (OllamaError, ollama.ResponseError) as e:
                print(f"  model error: {e}\n", file=sys.stderr)
                continue
            except KeyboardInterrupt:
                print("\n  (stopped)\n")
                continue

            print(f"\n{reply}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
