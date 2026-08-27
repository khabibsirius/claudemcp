import argparse
import sys

from config import APP_NAME
from qlik_engine import QlikEngine, QlikEngineError


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="List the sheets in an app, or the apps on this Qlik instance.")
    parser.add_argument("--app", default=APP_NAME, help=f"App to inspect (default: {APP_NAME!r})")
    parser.add_argument("--apps", action="store_true", help="List apps instead of sheets")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    try:
        with QlikEngine() as engine:
            if args.apps:
                apps = engine.list_apps()
                print(f"{len(apps)} app(s):\n")
                for app in apps:
                    print(f"  {app['name']}")
                    print(f"      id: {app['id']}")
                return 0

            engine.open_app(args.app)
            sheets = engine.list_sheets()

            print(f"{len(sheets)} sheet(s) in {args.app!r}:\n")
            for sheet in sheets:
                charts = sheet["chart_count"]
                print(f"  {sheet['title'] or '(untitled)'}  -  {charts} chart(s)")
                print(f"      qId: {sheet['qId']}")
                if sheet["description"]:
                    print(f"      {sheet['description']}")

    except QlikEngineError as e:
        print(f"Failed: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
