"""Smoke test: can we reach Qlik, open the app, and reach Ollama?

    python check_connection.py

Run this first when something isn't working. It checks each dependency
separately so you find out *which* one is broken, rather than watching the
whole pipeline fail at once. Uses no LLM tokens beyond a model listing.
"""

import sys

from config import APP_NAME, OLLAMA_MODEL
from config import summary as config_summary
from ollama_client import OllamaClient, OllamaError
from qlik_engine import QlikEngine, QlikEngineError

OK = "  ok   "
FAIL = " FAIL  "


def check_qlik():
    """Connect, list apps, open the configured one, read its fields."""

    try:
        engine = QlikEngine()
    except QlikEngineError as e:
        print(f"{FAIL} Qlik connection\n        {e}")
        return False

    with engine:
        print(f"{OK} Connected to the Qlik Engine ({engine.mode} mode)")

        try:
            apps = engine.list_apps()
            names = [a["name"] for a in apps if a["name"]]
            print(f"{OK} {len(apps)} app(s) visible: {', '.join(names[:10]) or '(none named)'}")
        except QlikEngineError as e:
            print(f"{FAIL} Listing apps\n        {e}")
            return False

        try:
            engine.open_app(APP_NAME)
            print(f"{OK} Opened app {APP_NAME!r}")
        except QlikEngineError as e:
            print(f"{FAIL} Opening app {APP_NAME!r}\n        {e}")
            return False

        try:
            fields = engine.get_fields()
        except QlikEngineError as e:
            print(f"{FAIL} Reading the data model\n        {e}")
            return False

        if not fields:
            print(f"{FAIL} App {APP_NAME!r} has no fields - is data loaded into it?")
            return False

        preview = ", ".join(f["name"] for f in fields[:8])
        print(f"{OK} {len(fields)} fields: {preview}{' ...' if len(fields) > 8 else ''}")

        try:
            sheets = engine.list_sheets()
            print(f"{OK} {len(sheets)} existing sheet(s)")
        except QlikEngineError as e:
            print(f"{FAIL} Listing sheets\n        {e}")
            return False

    return True


def check_ollama():
    """Only needed for the AI-design features, not the low-level MCP tools."""
    try:
        print(f"{OK} {OllamaClient(OLLAMA_MODEL).check()}")
        return True
    except OllamaError as e:
        print(f"{FAIL} Ollama\n        {e}")
        return False


def main():
    print("Configuration")
    print("-" * 60)
    print(config_summary())
    print()

    print("Checks")
    print("-" * 60)
    qlik_ok = check_qlik()
    ollama_ok = check_ollama()
    print()

    if qlik_ok and ollama_ok:
        print("All good - try `python main.py` or start the MCP server.")
        return 0
    if qlik_ok:
        print("Qlik works. The low-level MCP tools are usable; AI design is not.")
        return 1
    print("Fix the Qlik connection first - nothing else works without it.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
