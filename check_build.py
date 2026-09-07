import argparse
import sys

import chat_tools
from chart_specs import chart_requirements, resolve_chart_type
from config import APP_NAME
from qlik_engine import QlikEngine, QlikEngineError

OK = "  ok   "
STOP = " STOPS "


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Replay the checks create_chart runs before it builds anything, "
                    "and report which one stops it. Reads only - writes nothing."
    )
    parser.add_argument("--app", default=APP_NAME,
                        help=f"App to test against (default: {APP_NAME!r})")
    parser.add_argument("--type", default="table",
                        help="Chart type the assistant was asked for (default: table)")
    parser.add_argument("--dimensions", default="",
                        help="Comma-separated field names; blank picks the first two")
    parser.add_argument("--measures", default="",
                        help="Comma-separated expressions, e.g. \"Sum([Amount])\"")
    parser.add_argument("--title", default="Diagnostic table")
    return parser.parse_args(argv)


def split(text):
    return [part.strip() for part in (text or "").split(",") if part.strip()]


def main(argv=None):
    args = parse_args(argv)

    try:
        with QlikEngine() as engine:
            engine.open_app(args.app)
            print(f"App {args.app!r} -> {engine.app_id!r} ({engine.mode} mode)\n")

            try:
                fields = [f["name"] for f in engine.get_fields()]
            except QlikEngineError as e:
                print(f"{STOP} get_fields() failed: {e}")
                return 1

            print(f"{len(fields)} field(s) in the data model:")
            print(f"   {', '.join(fields[:40])}"
                  f"{' ...' if len(fields) > 40 else ''}")
            if not fields:
                print(f"\n{STOP} The app has no fields. Nothing can be charted "
                      f"until the load script has run.")
                return 1

            dimensions = split(args.dimensions) or fields[:2]
            measures = split(args.measures)
            resolved = resolve_chart_type(args.type)

            print(f"\nAsking for : {args.type!r} -> resolves to {resolved!r}")
            print(f"dimensions : {dimensions}")
            print(f"measures   : {measures or '(none)'}")

            if resolved:
                (dmin, dmax), (mmin, mmax) = chart_requirements(resolved)
                print(f"{resolved} accepts {dmin}-{dmax} dimension(s), "
                      f"{mmin}-{mmax} measure(s)")

            print("\nStep 1 - _chart_problem (fields exist, expressions valid)")
            problem = chat_tools._chart_problem(
                engine, args.type, args.title, dimensions, measures)
            if problem:
                print(f"{STOP} {problem}")
                print("\nThis is what the assistant reports instead of building.")
                return 0
            print(f"{OK} passed")

            print("\nStep 2 - _chart_preview (does the data come back?)")
            preview, problem = chat_tools._chart_preview(
                engine, args.type, dimensions, measures)
            if problem:
                print(f"{STOP} {problem}")
                print("\nThis is what the assistant reports instead of building.")
                return 0
            if preview:
                rows = preview if isinstance(preview, list) else [preview]
                print(f"{OK} preview returned {len(rows)} row(s)")
            else:
                print(f"{OK} no preview needed for this shape")

            print("\nStep 3 - what create_chart would do next")
            attempts = [resolved or args.type]
            attempts += [t for t in chat_tools.fallback_types(
                attempts[0], dimensions, measures) if t not in attempts]
            print(f"   build {attempts[0]!r}, and if it will not draw fall back "
                  f"through {attempts[1:] or '(nothing)'}")
            print(f"\n{OK} Nothing blocks creation. If no chart appears in Qlik "
                  f"after this, the failure is in the build itself - run the same "
                  f"request through the assistant and send me its reply.")

    except QlikEngineError as e:
        print(f"Failed: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
