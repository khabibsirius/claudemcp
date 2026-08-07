"""Turn the extracted Qlik chart definitions into a Python module.

    node tools/extract_chart_specs.js > tools/chart_defaults.json
    python tools/generate_chart_specs.py

Writes chart_defaults.py at the repo root: for every chart type Qlik ships,
its default property tree and how many dimensions and measures it accepts.

Both inputs come from the installed Qlik client, not from documentation:

  chart_defaults.json  each bundle's `qae.properties` and `qae.data.targets`
  chart_types.json     bundle name -> the qType the engine expects

The second is not guessable. sn-funnel-chart is "qlik-funnel-chart-ext",
sn-table is "sn-table" rather than "table", and getting it wrong produces an
object the client cannot render.
"""

import json
import pathlib
import pprint

HERE = pathlib.Path(__file__).parent
ROOT = HERE.parent

# Types that already have hand-verified property trees in chart_specs.py,
# confirmed rendering in a real app. Those stay authoritative - the point of
# this file is the ones we could not build before.
ALREADY_VERIFIED = {"kpi", "barchart", "linechart", "piechart", "table"}

# Objects that hold no data of their own. They are legitimate sheet contents
# but they are not charts, so they are kept out of the chart list.
NON_DATA = {
    "sn-action-button", "sn-layout-container", "sn-nav-menu", "sn-shape",
    "sn-slider", "sn-tabbed-container", "sn-animator", "sn-text",
}


def unwrap(properties):
    """Some bundles nest everything under `initial`; use what's inside."""
    if set(properties) == {"initial"} and isinstance(properties["initial"], dict):
        return properties["initial"]
    return properties


def counts(targets):
    """(min, max) dimensions and measures, from the bundle's own targets.

    This is the part no amount of reading documentation would have given us:
    a scatter plot needs exactly one dimension and two to three measures, so
    building one with a single measure produces something that cannot draw.
    """
    dimensions = (0, 0)
    measures = (0, 0)

    for target in targets or []:
        dim = target.get("dimensions") or {}
        mea = target.get("measures") or {}
        if dim:
            dimensions = (dim.get("min", 0), dim.get("max", 0))
        if mea:
            measures = (mea.get("min", 0), mea.get("max", 0))

    return dimensions, measures


def main():
    extracted = json.loads((HERE / "chart_defaults.json").read_text(encoding="utf-8"))
    types = json.loads((HERE / "chart_types.json").read_text(encoding="utf-8"))

    specs = {}
    skipped = []

    for bundle, entry in sorted(extracted["extracted"].items()):
        if bundle in NON_DATA:
            continue

        qtype = types.get(bundle)
        if not qtype:
            # No registry entry means no way to know what the engine calls
            # it, and a guessed qType renders as an unknown object.
            skipped.append(f"{bundle} (no engine type in the client registry)")
            continue

        properties = unwrap(entry["properties"])
        dimensions, measures = counts(entry["targets"])

        if measures == (0, 0) and dimensions == (0, 0):
            skipped.append(f"{bundle} (accepts no dimensions or measures)")
            continue

        specs[qtype] = {
            "bundle": bundle,
            "version": entry["version"],
            "dimensions": dimensions,
            "measures": measures,
            "properties": properties,
        }

    out = ROOT / "chart_defaults.py"
    with out.open("w", encoding="utf-8") as f:
        f.write('"""Default properties for every Qlik chart type. GENERATED - do not edit.\n\n')
        f.write("Produced by tools/generate_chart_specs.py from the nebula.js bundles\n")
        f.write("shipped with Qlik Sense itself, so these are the exact property trees\n")
        f.write("the Qlik client writes when you add a chart by hand. Guessing them from\n")
        f.write("documentation produced charts that rendered blank without erroring.\n\n")
        f.write("Each entry carries the property tree plus (min, max) dimensions and\n")
        f.write("measures the chart accepts - a scatter plot needs two measures, and\n")
        f.write("nothing short of the bundle would have told us that.\n\n")
        f.write("To refresh:\n")
        f.write("    node tools/extract_chart_specs.js > tools/chart_defaults.json\n")
        f.write("    python tools/generate_chart_specs.py\n")
        f.write('"""\n\n')
        f.write("CHART_DEFAULTS = ")
        f.write(pprint.pformat(specs, indent=1, width=88, sort_dicts=True))
        f.write("\n")

    print(f"wrote {out.relative_to(ROOT)} with {len(specs)} chart type(s)")
    for qtype in sorted(specs):
        spec = specs[qtype]
        d, m = spec["dimensions"], spec["measures"]
        new = "" if qtype in ALREADY_VERIFIED else "  NEW"
        print(f"  {qtype:<24} dims {d[0]}-{d[1]}  measures {m[0]}-{m[1]}{new}")
    for line in skipped:
        print(f"  skipped: {line}")


if __name__ == "__main__":
    main()
