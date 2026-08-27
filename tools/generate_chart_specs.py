import json
import pathlib
import pprint

HERE = pathlib.Path(__file__).parent
ROOT = HERE.parent

ALREADY_VERIFIED = {"kpi", "barchart", "linechart", "piechart", "table"}

NON_DATA = {
    "sn-action-button", "sn-layout-container", "sn-nav-menu", "sn-shape",
    "sn-slider", "sn-tabbed-container", "sn-animator", "sn-text",
}


def unwrap(properties):
    if set(properties) == {"initial"} and isinstance(properties["initial"], dict):
        return unquote(properties["initial"])
    return unquote(properties)


def unquote(properties):
    if isinstance(properties, dict):
        return {key: unquote(value) for key, value in properties.items()}
    if isinstance(properties, list):
        return [unquote(value) for value in properties]
    if isinstance(properties, str) and len(properties) > 1:
        for quote in ('"', "'"):
            if properties.startswith(quote) and properties.endswith(quote):
                return properties[1:-1]
    return properties


def counts(targets):
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
