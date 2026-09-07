import argparse
import sys
from collections import Counter

from chart_defaults import CHART_DEFAULTS
from chart_specs import hypercube_owner
from config import APP_NAME
from qlik_engine import QlikEngine, QlikEngineError


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Walk every sheet in an app and report, for each object on it, "
                    "what the engine computes and which bundle version it carries."
    )
    parser.add_argument("--app", default=APP_NAME,
                        help=f"App to inspect (default: {APP_NAME!r})")
    parser.add_argument("--full", action="store_true",
                        help="Print the whole property tree of every object")
    return parser.parse_args(argv)


def data_verdict(engine, handle, properties):
    try:
        layout = engine.send("GetLayout", handle=handle)["result"]["qLayout"]
    except (QlikEngineError, KeyError) as e:
        return "?", f"no layout ({e})"

    _owner, path = hypercube_owner(properties)
    branch = layout if path == "qHyperCubeDef" else layout.get(path.split(".", 1)[0], {})
    cube = (branch or {}).get("qHyperCube")
    if cube is None:
        return "-", f"no qHyperCube at {path!r}"

    if cube.get("qError"):
        return "no", f"engine error {cube['qError']}"

    rows = (cube.get("qSize") or {}).get("qcy")
    cols = (cube.get("qSize") or {}).get("qcx")
    if not rows:
        return "no", f"computes 0 rows (qcx={cols})"

    for key in engine.DATA_PAGE_KEYS:
        pages = cube.get(key) or []
        if pages and (pages[0].get("qMatrix") or pages[0].get("qData")
                      or pages[0].get("qNodes")):
            return "yes", f"{rows} rows x {cols} cols in {key}"

    present = [k for k in engine.DATA_PAGE_KEYS if cube.get(k)]
    return "no", (f"{rows} rows x {cols} cols but empty pages "
                  f"(present: {present or 'none'})")


def version_of(properties):
    return str(properties.get("version")
               or (properties.get("qHyperCubeDef") or {}).get("version")
               or "-")


def main(argv=None):
    args = parse_args(argv)

    try:
        with QlikEngine() as engine:
            engine.open_app(args.app)
            print(f"Connected in {engine.mode} mode.")
            print(f"Asked for app {args.app!r} -> opened id {engine.app_id!r}\n")

            infos = engine.send("GetAllInfos",
                                handle=engine.app_handle)["result"]["qInfos"]
            census = Counter(i["qType"] for i in infos)
            print(f"GetAllInfos returned {len(infos)} object(s) at app level:")
            for q_type, count in census.most_common():
                mark = "  <- a chart type we know" if q_type in CHART_DEFAULTS else ""
                print(f"   {count:>4}  {q_type}{mark}")

            sheets = engine.list_sheets()
            print(f"\n{len(sheets)} sheet(s):")
            if not sheets:
                print("   (none - nothing has been built in this app)")

            mismatched = Counter()
            inspected = 0

            for sheet in sheets:
                try:
                    sheet_handle = engine._object_handle(sheet["qId"])
                    sheet_props = engine.send(
                        "GetProperties", handle=sheet_handle)["result"]["qProp"]
                except QlikEngineError as e:
                    print(f"\n   {sheet['title']!r} - could not read: {e}")
                    continue

                cells = sheet_props.get("cells") or []
                print(f"\n   {sheet['title'] or '(untitled)'!r}  "
                      f"({sheet['qId']})  {len(cells)} cell(s)")

                if not cells:
                    continue

                print(f"      {'object':<24} {'type':<20} {'ver':<9} {'we write':<9} "
                      f"{'data':<5} note")

                for cell in cells:
                    object_id = cell.get("name")
                    if not object_id:
                        continue
                    try:
                        handle = engine._object_handle(object_id)
                        props = engine.send(
                            "GetProperties", handle=handle)["result"]["qProp"]
                    except QlikEngineError as e:
                        print(f"      {object_id:<24} (could not read: {e})")
                        continue

                    inspected += 1
                    q_type = props.get("qInfo", {}).get("qType") or cell.get("type") or "?"
                    installed = version_of(props)
                    ours = str(CHART_DEFAULTS[q_type]["version"]) \
                        if q_type in CHART_DEFAULTS else "-"
                    draws, note = data_verdict(engine, handle, props)

                    flag = ""
                    if ours != "-" and installed not in ("-", ours):
                        flag = "   <-- VERSION MISMATCH"
                        mismatched[q_type] += 1

                    print(f"      {object_id:<24} {q_type:<20} {installed:<9} "
                          f"{ours:<9} {draws:<5} {note}{flag}")

                    if args.full:
                        import json
                        print(json.dumps(props, indent=2)[:4000])

            print(f"\n{inspected} object(s) inspected across {len(sheets)} sheet(s).")

            if mismatched:
                print("\nVersion mismatches - chart_defaults.py was harvested from a "
                      "different Qlik build than this server runs:")
                for q_type, count in mismatched.most_common():
                    print(f"   {q_type:<20} {count} object(s)  "
                          f"we write {CHART_DEFAULTS[q_type]['version']}")
                print("\nRegenerate it from THIS server's client bundles:")
                print(r"   set QLIK_CLIENT=<path to ...\Client\qmfe\@nebula.js>")
                print("   node tools/extract_chart_specs.js > tools/chart_defaults.json")
                print("   python tools/generate_chart_specs.py")
            elif inspected:
                print("No version mismatches. If a chart still draws nothing, re-run "
                      "with --full and send the property tree of the broken one.")

    except QlikEngineError as e:
        print(f"Failed: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
