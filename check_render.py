import argparse
import json
import sys

from chart_specs import hypercube_owner, resolve_chart_type
from config import APP_NAME
from qlik_engine import QlikEngine, QlikEngineError

SCRATCH = "ZZ diagnostic - safe to delete"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Build one chart on a scratch sheet, report exactly what the "
                    "engine gives back, then remove it. Never saves the app."
    )
    parser.add_argument("--app", default=APP_NAME)
    parser.add_argument("--type", default="table")
    parser.add_argument("--dimensions", default="",
                        help="Comma-separated; blank picks the first two fields")
    parser.add_argument("--measures", default="")
    parser.add_argument("--keep", action="store_true",
                        help="Leave the chart in place AND save the app")
    return parser.parse_args(argv)


def split(text):
    return [p.strip() for p in (text or "").split(",") if p.strip()]


def main(argv=None):
    args = parse_args(argv)
    dimensions = split(args.dimensions)
    measures = split(args.measures)
    resolved = resolve_chart_type(args.type) or args.type

    object_id = None
    sheet_id = None

    try:
        with QlikEngine() as engine:
            engine.open_app(args.app)
            print(f"App {args.app!r} -> {engine.app_id!r} ({engine.mode} mode)")

            if not dimensions:
                dimensions = [f["name"] for f in engine.get_fields()][:2]
            print(f"Building {resolved!r} over dimensions={dimensions} "
                  f"measures={measures or '(none)'}\n")

            engine.create_sheet(SCRATCH)
            sheet_id = engine.sheet_id
            print(f"scratch sheet {sheet_id}")

            response = engine.create_chart(
                resolved, "Diagnostic",
                dimensions=dimensions, measure_expressions=measures,
            )
            object_id = (response.get("result", {})
                         .get("qReturn", {}).get("qGenericId"))
            print(f"created object {object_id!r}\n")

            if not object_id:
                print("The engine returned no object id. Raw response:")
                print(json.dumps(response, indent=2)[:1500])
                return 1

            handle = engine._object_handle(object_id)
            props = engine.send("GetProperties", handle=handle)["result"]["qProp"]
            layout = engine.send("GetLayout", handle=handle)["result"]["qLayout"]

            print(f"qType written   : {props.get('qInfo', {}).get('qType')!r}")
            print(f"version written : {props.get('version')!r}")

            _owner, path = hypercube_owner(props)
            branch = layout if path == "qHyperCubeDef" else layout.get(
                path.split(".", 1)[0], {})
            cube = (branch or {}).get("qHyperCube")

            if cube is None:
                print(f"\nNo qHyperCube at {path!r} in the layout. Layout keys: "
                      f"{sorted(layout)}")
            else:
                size = cube.get("qSize") or {}
                print(f"qSize           : {size.get('qcy')} rows x "
                      f"{size.get('qcx')} cols")
                print(f"qError          : {cube.get('qError')}")
                print(f"qMode           : {cube.get('qMode')!r}")
                for key in engine.DATA_PAGE_KEYS:
                    pages = cube.get(key)
                    if pages is None:
                        continue
                    first = pages[0] if pages else {}
                    rows = len(first.get("qMatrix") or first.get("qData")
                               or first.get("qNodes") or [])
                    print(f"{key:<16}: {len(pages)} page(s), "
                          f"{rows} row(s) in the first")

            ok, detail = engine.chart_renders(object_id)
            print(f"\nchart_renders() : {ok}  -  {detail}")
            if not ok:
                print("\nThis is why the chart is deleted again and never "
                      "reaches the app.")

            if args.keep:
                engine.save()
                print(f"\nKept and saved. Delete the sheet {SCRATCH!r} in Qlik "
                      f"when you are done.")
                return 0

            engine.delete_chart(object_id)
            engine.send("DestroyObject", handle=engine.app_handle,
                        params=[sheet_id])
            print("\nCleaned up. The app was never saved, so nothing persists.")

    except QlikEngineError as e:
        print(f"Failed: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
