import json
import sys

from config import APP_NAME
from qlik_engine import QlikEngine

# Optional: python dump_object_properties.py "Customer Segment"
# filters to objects whose qId or title contains this text (case-insensitive).
filter_text = sys.argv[1].lower() if len(sys.argv) > 1 else None

engine = QlikEngine()

try:
    print(f"Opening '{APP_NAME}'...")
    engine.open_app(APP_NAME)

    # GetAllInfos returns every object in the app (sheets, charts, etc.)
    response = engine.send("GetAllInfos", handle=engine.app_handle)
    infos = response["result"]["qInfos"]

    print(f"\n{len(infos)} objects found in the app:\n")

    interesting_types = {"sheet"}  # we'll filter to chart-ish objects below

    for info in infos:
        q_id = info["qId"]
        q_type = info["qType"]

        # Skip internal/system objects, focus on visualizations
        if q_type in ("sheet", "LoadModel", "loadModel", "MasterObject"):
            continue

        try:
            obj_response = engine.send(
                "GetObject", handle=engine.app_handle, params=[q_id]
            )
            obj_handle = obj_response["result"]["qReturn"]["qHandle"]

            props = engine.send("GetProperties", handle=obj_handle)
            qprop = props["result"]["qProp"]

            if filter_text:
                haystack = (q_id + " " + qprop.get("title", "")).lower()
                if filter_text not in haystack:
                    continue

            print(f"--- qId={q_id}  qType={q_type} ---")
            print(json.dumps(qprop, indent=2))
            print()

        except Exception as e:
            print(f"--- qId={q_id}  qType={q_type} ---")
            print(f"  (couldn't read properties: {e})\n")

finally:
    engine.close()