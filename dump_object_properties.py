"""Dump the full saved properties of every object in an app.

    python dump_object_properties.py
    python dump_object_properties.py --filter Sales
    python dump_object_properties.py --type piechart

This is how the property trees in chart_specs.py were derived: build a chart
by hand in the Qlik client until it renders correctly, dump it here, and copy
the real shape rather than guessing from the documentation. Use it whenever
you add a chart type or a chart renders blank for no obvious reason.
"""

import argparse
import json
import sys

from config import APP_NAME
from qlik_engine import QlikEngine, QlikEngineError

# Containers and internals rather than visualizations - never interesting here.
SKIPPED_TYPES = {"sheet", "LoadModel", "loadModel", "MasterObject", "AppPropsList"}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--app", default=APP_NAME, help=f"App to dump (default: {APP_NAME!r})")
    parser.add_argument(
        "--filter", dest="filter_text", default=None,
        help="Only objects whose qId or title contains this text (case-insensitive)",
    )
    parser.add_argument(
        "--type", dest="q_type", default=None,
        help="Only objects of this qType, e.g. piechart",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    needle = args.filter_text.lower() if args.filter_text else None

    try:
        with QlikEngine() as engine:
            engine.open_app(args.app)

            infos = engine.send("GetAllInfos", handle=engine.app_handle)["result"]["qInfos"]
            print(f"{len(infos)} object(s) in {args.app!r}\n")

            shown = 0
            for info in infos:
                q_id, q_type = info["qId"], info["qType"]

                if q_type in SKIPPED_TYPES:
                    continue
                if args.q_type and q_type != args.q_type:
                    continue

                try:
                    handle = engine.send(
                        "GetObject", handle=engine.app_handle, params=[q_id]
                    )["result"]["qReturn"]["qHandle"]
                    props = engine.send("GetProperties", handle=handle)["result"]["qProp"]
                except QlikEngineError as e:
                    print(f"--- qId={q_id}  qType={q_type} ---")
                    print(f"  (couldn't read properties: {e})\n")
                    continue

                if needle and needle not in f"{q_id} {props.get('title', '')}".lower():
                    continue

                print(f"--- qId={q_id}  qType={q_type} ---")
                print(json.dumps(props, indent=2))
                print()
                shown += 1

            if not shown:
                print("No objects matched.")

    except QlikEngineError as e:
        print(f"Failed: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
