import argparse
import pprint
import re
import sys
from pathlib import Path

from chart_defaults import CHART_DEFAULTS
from config import APP_NAME
from qlik_engine import QlikEngine, QlikEngineError

OUT = Path(__file__).parent / "chart_overrides.py"

EQUIVALENT = {
    "table": "sn-table",
    "pivot-table": "sn-pivot-table",
    "pivottable": "sn-pivot-table",
}

OURS = re.compile(r"^[A-Z0-9-]{2,4}_[0-9a-f]{8}$")

STRIP_KEYS = ("qInfo", "qMetaDef", "qExtendsId", "qStateName", "descriptionExpression",
              "titleExpression", "qLayoutExclude", "hasSelections")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Read chart objects a person built in a Qlik app and write "
                    "chart_overrides.py, so this project writes the property "
                    "trees this particular server understands."
    )
    parser.add_argument("--app", default=APP_NAME)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be written and change nothing")
    parser.add_argument("--include-ours", action="store_true",
                        help="Also learn from objects this project created. Off "
                             "by default: one of those can exist on a sheet and "
                             "still be unrenderable, which is the whole problem "
                             "this tool exists to detect.")
    return parser.parse_args(argv)


def clean(properties):
    tree = {k: v for k, v in properties.items() if k not in STRIP_KEYS}

    tree["title"] = ""
    tree["subtitle"] = ""
    tree["footnote"] = ""

    cube = tree.get("qHyperCubeDef")
    if isinstance(cube, dict):
        cube = dict(cube)
        cube["qDimensions"] = []
        cube["qMeasures"] = []
        _blank_column_lists(cube)
        tree["qHyperCubeDef"] = cube

    _blank_column_lists(tree)

    return tree


PER_COLUMN = ("qColumnOrder", "columnOrder", "columnWidths",
              "qInterColumnSortOrder")


def _blank_column_lists(holder):
    for key in PER_COLUMN:
        if key in holder:
            holder[key] = []


def bounds(q_type):
    twin = EQUIVALENT.get(q_type)
    spec = CHART_DEFAULTS.get(twin) if twin else CHART_DEFAULTS.get(q_type)
    if spec:
        return tuple(spec["dimensions"]), tuple(spec["measures"])
    return (0, 1000), (0, 1000)


def main(argv=None):
    args = parse_args(argv)
    harvested = {}
    seen = set()
    ours = {}

    try:
        with QlikEngine() as engine:
            engine.open_app(args.app)
            print(f"App {args.app!r} -> {engine.app_id!r} ({engine.mode} mode)\n")

            for sheet in engine.list_sheets():
                try:
                    handle = engine._object_handle(sheet["qId"])
                    sheet_props = engine.send(
                        "GetProperties", handle=handle)["result"]["qProp"]
                except QlikEngineError as e:
                    print(f"   skipped sheet {sheet['qId']}: {e}")
                    continue

                for cell in sheet_props.get("cells") or []:
                    object_id = cell.get("name")
                    if not object_id:
                        continue
                    try:
                        object_handle = engine._object_handle(object_id)
                        props = engine.send(
                            "GetProperties",
                            handle=object_handle)["result"]["qProp"]
                    except QlikEngineError as e:
                        print(f"   skipped {object_id}: {e}")
                        continue

                    q_type = props.get("qInfo", {}).get("qType")
                    if not q_type or q_type == "sheet":
                        continue

                    if OURS.match(object_id) and not args.include_ours:
                        ours.setdefault(q_type, []).append(object_id)
                        continue

                    seen.add(q_type)

                    if q_type in harvested:
                        print(f"   {q_type:<20} already harvested, skipping "
                              f"{object_id}")
                        continue

                    dims, meas = bounds(q_type)
                    harvested[q_type] = {
                        "bundle": q_type,
                        "dimensions": dims,
                        "measures": meas,
                        "properties": clean(props),
                    }
                    print(f"   {q_type:<20} harvested from {object_id} "
                          f"(dims {dims[0]}-{dims[1]}, measures "
                          f"{meas[0]}-{meas[1]})")

            if ours:
                print()
                for q_type, ids in sorted(ours.items()):
                    print(f"   {q_type:<20} skipped {len(ids)} object(s) this "
                          f"project created ({', '.join(ids[:3])})")
                print("   Those prove nothing about what the client can draw - "
                      "an unrenderable object still sits on the sheet.")

            if not harvested:
                print("\nNothing harvested. Build one chart of each type you want "
                      "supported by hand in the Qlik client, on any sheet in this "
                      "app, then run this again.")
                return 1

            available = tuple(sorted(seen))
            body = (
                "CHART_OVERRIDES = "
                + pprint.pformat(harvested, indent=1, width=88, sort_dicts=True)
                + "\n\nAVAILABLE_TYPES = "
                + pprint.pformat(available, indent=1, width=88)
                + "\n"
            )

            print(f"\nTypes seen on this server: {', '.join(available)}")

            if args.dry_run:
                print(f"\n--dry-run, so {OUT.name} was not written. It would be:\n")
                print(body[:3000])
                return 0

            OUT.write_text(body, encoding="utf-8")
            print(f"\nWrote {OUT} ({len(harvested)} type(s)).")
            print("Restart the server for it to take effect.")

    except QlikEngineError as e:
        print(f"Failed: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
