import argparse
import logging
import sys

from config import APP_NAME, CHAT_MODEL
from dashboard_builder import run
from llm import ModelError
from qlik_engine import QlikEngineError


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Build an AI-designed dashboard end-to-end, from the command line.")
    parser.add_argument(
        "--app", default=APP_NAME,
        help=f"App to build in, by title, filename or id (default: {APP_NAME!r})",
    )
    parser.add_argument(
        "--model", default=CHAT_MODEL,
        help=f"model to design with (default: {CHAT_MODEL!r})",
    )
    parser.add_argument(
        "--instruction", default=None,
        help="Free-text steer, e.g. 'focus on sales by region and show a trend'",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Show debug logging, including skipped engine messages",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        stream=sys.stdout,
        format="%(message)s" if not args.verbose else "%(levelname)s %(name)s: %(message)s",
    )

    try:
        spec, built, skipped = run(
            app_name=args.app, model=args.model, instruction=args.instruction
        )
    except (QlikEngineError, ModelError, ValueError) as e:
        print(f"\nFailed: {e}", file=sys.stderr)
        return 1

    print(f"\n{spec['dashboard_title']}")
    for viz in built:
        print(f"  + {viz['type']:<10} {viz['title']}")
    for viz, reason in skipped:
        print(f"  ! skipped   {viz.get('title') or '(untitled)'} - {reason}")

    if not built:
        print("\nNothing could be built, so the app was left untouched.", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
