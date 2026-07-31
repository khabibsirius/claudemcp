import json
import os
import random
import string
import uuid
import websocket

from config import (
    QLIK_HOST, QLIK_PORT, QLIK_MODE,
    QLIK_CERT_DIR, QLIK_USER_DIRECTORY, QLIK_USER_ID
)


class QlikEngineError(Exception):
    """Raised when the Qlik Engine API returns an error response."""
    pass


class QlikEngine:

    # Qlik Sense sheets use a 24-column responsive grid by default.
    GRID_COLUMNS = 24
    GRID_ROW_HEIGHT = 4  # rows tall per chart, matches default Qlik object size

    def __init__(self, host=QLIK_HOST, port=QLIK_PORT, mode=QLIK_MODE,
                 cert_dir=QLIK_CERT_DIR, user_directory=QLIK_USER_DIRECTORY,
                 user_id=QLIK_USER_ID):

        mode = (mode or "desktop").lower()
        self.mode = mode

        if mode == "enterprise":
            if not cert_dir:
                raise QlikEngineError(
                    "QLIK_MODE=enterprise requires QLIK_CERT_DIR - a folder "
                    "containing client.pem, client_key.pem, and root.pem "
                    "exported from the QMC's Certificates section."
                )
            if not (user_directory and user_id):
                raise QlikEngineError(
                    "QLIK_MODE=enterprise requires QLIK_USER_DIRECTORY and "
                    "QLIK_USER_ID to identify which user the engine session "
                    "runs as."
                )

            self.ws = websocket.create_connection(
                f"wss://{host}:{port}/app/",
                header=[f"X-Qlik-User: UserDirectory={user_directory};UserId={user_id}"],
                sslopt={
                    "certfile": os.path.join(cert_dir, "client.pem"),
                    "keyfile": os.path.join(cert_dir, "client_key.pem"),
                    "ca_certs": os.path.join(cert_dir, "root.pem"),
                }
            )
        else:
            self.ws = websocket.create_connection(
                f"ws://{host}:{port}/app/"
            )

        self.request_id = 0

        self.app_handle = None
        self.sheet_handle = None
        self.sheet_id = None

        # Tracks where the next chart should be placed on the sheet grid.
        self._next_col = 0
        self._next_row = 0

    # ------------------------------------------------------------------
    # Low-level JSON-RPC plumbing
    # ------------------------------------------------------------------

    def send(self, method, handle=-1, params=None):

        self.request_id += 1

        request = {
            "jsonrpc": "2.0",
            "id": self.request_id,
            "handle": handle,
            "method": method,
            "params": params or []
        }

        self.ws.send(json.dumps(request))

        while True:

            response = json.loads(self.ws.recv())

            if "id" not in response:
                # Async notifications (e.g. OnConnected) - ignore.
                continue

            if response["id"] != self.request_id:
                continue

            if "error" in response:
                raise QlikEngineError(
                    f"{method} failed: {response['error'].get('message', response['error'])}"
                )

            return response

    # ------------------------------------------------------------------
    # App / document
    # ------------------------------------------------------------------

    def open_app(self, app_name):

        response = self.send(
            "OpenDoc",
            params=[app_name]
        )

        self.app_handle = response["result"]["qReturn"]["qHandle"]

        return self.app_handle

    # ------------------------------------------------------------------
    # Data model introspection - this is what feeds the AI
    # ------------------------------------------------------------------

    def get_fields(self):
        """Return field metadata (name, tags, source tables) for the open app."""

        response = self.send(
            "CreateSessionObject",
            handle=self.app_handle,
            params=[{
                "qInfo": {
                    "qType": "FieldList"
                },
                "qFieldListDef": {
                    "qShowSystem": False,
                    "qShowHidden": False,
                    "qShowSemantic": True,
                    "qShowSrcTables": True
                }
            }]
        )

        field_handle = response["result"]["qReturn"]["qHandle"]

        layout = self.send(
            "GetLayout",
            handle=field_handle
        )

        fields = []

        for item in layout["result"]["qLayout"]["qFieldList"]["qItems"]:

            fields.append({
                "name": item["qName"],
                "tags": item.get("qTags", []),
                "tables": item.get("qSrcTables", []),
                "is_numeric": "$numeric" in item.get("qTags", []),
                "is_key": "$key" in item.get("qTags", []),
            })

        self.destroy_session_object(field_handle)

        return fields

    def destroy_session_object(self, handle):
        """Best-effort cleanup of a temporary session object."""
        try:
            self.send("DestroySessionObject", handle=self.app_handle, params=[handle])
        except QlikEngineError:
            pass

    # ------------------------------------------------------------------
    # Sheet management
    # ------------------------------------------------------------------

    def create_sheet(self, title, description="Created by AI"):

        sheet_id = "SH_" + uuid.uuid4().hex[:8]

        response = self.send(
            "CreateObject",
            handle=self.app_handle,
            params=[{
                "qInfo": {
                    "qId": sheet_id,
                    "qType": "sheet"
                },
                "qMetaDef": {
                    "title": title,
                    "description": description
                },
                "rank": -1,
                "thumbnail": {
                    "qStaticContentUrlDef": {}
                },
                "columns": self.GRID_COLUMNS,
                "rows": 100,
                "cells": [],
                "qChildListDef": {
                    "qData": {
                        "title": "/title",
                        "description": "/description",
                        "meta": "/meta",
                        "order": "/order",
                        "id": "/qInfo/qId",
                        "type": "/qInfo/qType",
                        "hc": "/qHyperCubeDef"
                    }
                }
            }]
        )

        self.sheet_handle = response["result"]["qReturn"]["qHandle"]
        self.sheet_id = sheet_id
        self._next_col = 0
        self._next_row = 0

        return response

    def _place_on_grid(self, colspan=12, rowspan=4):
        """Very simple left-to-right, top-to-bottom grid packer for chart placement."""

        if self._next_col + colspan > self.GRID_COLUMNS:
            self._next_col = 0
            self._next_row += rowspan

        col, row = self._next_col, self._next_row
        self._next_col += colspan

        return col, row, colspan, rowspan

    def _add_object_to_sheet_layout(self, object_id, object_type, colspan=12, rowspan=4):
        """Registers a child object's position in the sheet's 'cells' grid so it
        actually shows up laid out on the sheet in the Qlik Sense client."""

        if self.sheet_handle is None:
            raise QlikEngineError("No sheet has been created.")

        col, row, colspan, rowspan = self._place_on_grid(colspan, rowspan)

        properties = self.send("GetProperties", handle=self.sheet_handle)
        sheet_props = properties["result"]["qProp"]

        sheet_props.setdefault("cells", []).append({
            "name": object_id,
            "type": object_type,
            "col": col,
            "row": row,
            "colspan": colspan,
            "rowspan": rowspan
        })

        self.send(
            "SetProperties",
            handle=self.sheet_handle,
            params=[sheet_props]
        )

    # ------------------------------------------------------------------
    # Visualization creation
    # ------------------------------------------------------------------

    @staticmethod
    def _gen_cid(length=5):
        """Generates a short component id in the style Qlik assigns to
        dimensions/measures (e.g. 'ayj', 'qfARm'). Just needs to be unique
        within the object, not globally."""
        return "".join(random.choices(string.ascii_letters, k=length))

    def create_chart(self, chart_type, title, dimension=None, measure=None,
                      measure_expression=None, colspan=12, rowspan=4):
        """Generic chart creator.

        chart_type: one of "barchart", "linechart", "piechart", "table", "kpi"
        dimension: field name to use as the dimension (omit for kpi)
        measure: friendly label for the measure
        measure_expression: full Qlik expression, e.g. "Sum([Sales])".
                             Defaults to Sum(measure) if not given.
        """

        if self.sheet_handle is None:
            raise QlikEngineError("No sheet has been created.")

        is_chart = chart_type in ("barchart", "linechart", "piechart")

        object_id = chart_type[:3].upper() + "_" + uuid.uuid4().hex[:8]

        expression = measure_expression or (f"Sum([{measure}])" if measure else None)

        hypercube_def = {
            "qDimensions": [],
            "qMeasures": [],
            "qInitialDataFetch": [
                {"qTop": 0, "qLeft": 0, "qHeight": 100, "qWidth": 2}
            ]
        }

        if dimension:
            dim_def = {"qDef": {"qFieldDefs": [dimension]}}
            if is_chart:
                dim_def["qDef"]["cId"] = self._gen_cid()
            hypercube_def["qDimensions"].append(dim_def)

        if expression:
            meas_def = {"qDef": {"qDef": f"={expression}", "qLabel": measure or expression}}
            if is_chart:
                meas_def["qDef"]["cId"] = self._gen_cid()
            hypercube_def["qMeasures"].append(meas_def)

        if is_chart:
            # A hand-built chart's qInterColumnSortOrder lists measure
            # column(s) first, then dimension column(s) - e.g. for 1 dim +
            # 1 measure: [1, 0]. Without this the chart renders blank even
            # though the underlying data is valid.
            n_dims = len(hypercube_def["qDimensions"])
            n_meas = len(hypercube_def["qMeasures"])
            hypercube_def["qInterColumnSortOrder"] = (
                list(range(n_dims, n_dims + n_meas)) + list(range(n_dims))
            )
            hypercube_def["qSuppressMissing"] = True

        properties = {
            "qInfo": {
                "qId": object_id,
                "qType": chart_type
            },
            "title": title,
            "subtitle": "",
            "footnote": "",
            "disableNavMenu": False,
            "showTitles": True,
            "showDetails": True,
            "showDetailsExpression": False,
            "showDisclaimer": True,
            "qHyperCubeDef": hypercube_def,
        }

        # These native chart types are rendered by nebula.js components
        # (sn-bar-chart, sn-line-chart, sn-pie-chart) that crash with
        # "Cannot read properties of undefined" if their full expected
        # property tree isn't present - a chart built by hand always has
        # every one of these keys; CreateChild does not fill them in.
        # This block is copied directly from a real, manually-built,
        # working bar chart's saved properties (confirmed via GetProperties)
        # rather than guessed.
        common_chart_extras = {
            "legend": {"show": True, "dock": "auto", "showTitle": True},
            "color": {
                "auto": True,
                "mode": "primary",
                "formatting": {"numFormatFromTemplate": True},
                "useBaseColors": "off",
                "paletteColor": {"index": 6},
                "useDimColVal": True,
                "useMeasureGradient": True,
                "persistent": False,
                "expressionIsColor": True,
                "expressionLabel": "",
                "measureScheme": "sg",
                "reverseScheme": False,
                "dimensionScheme": "12",
                "autoMinMax": True,
                "measureMin": 0,
                "measureMax": 10
            },
            "dataPoint": {
                "showLabels": False,
                "showSegmentLabels": False,
                "showTotalLabels": True
            },
            "tooltip": {
                "auto": True,
                "hideBasic": False,
                "chart": {"style": {"size": "medium"}},
                "data": {}
            },
            "version": "2.2.0",
            "components": [],
        }

        if chart_type in ("barchart", "linechart"):
            # Both are cartesian (axis-based) nebula charts and crashed
            # with the identical "reading 'show'" error - both need the
            # axis config objects.
            properties.update(common_chart_extras)
            properties.update({
                "visualization": chart_type,
                "script": "",
                "filter": None,
                "refLine": {"refLines": [], "dimRefLines": []},
                "plugins": [],
                "scrollbar": "miniChart",
                "scrollStartPos": 0,
                "gridLine": {"auto": True, "spacing": 2},
                "dimensionAxis": {
                    "continuousAuto": True,
                    "show": "all",
                    "label": "auto",
                    "dock": "near",
                    "axisDisplayMode": "auto",
                    "maxVisibleItems": 10
                },
                "preferContinuousAxis": True,
                "measureAxis": {
                    "show": "all",
                    "dock": "near",
                    "spacing": 1,
                    "autoMinMax": True,
                    "minMax": "min",
                    "min": 0,
                    "max": 10
                },
                "showMiniChartForContinuousAxis": True,
            })
            if chart_type == "barchart":
                properties.update({
                    "orientation": "vertical",
                    "barGrouping": {"grouping": "grouped"},
                })
            else:
                properties.update({
                    "lineType": "line",
                })
        elif chart_type == "piechart":
            # Reference: a manually-built pie chart's saved properties
            # (qId PsLnyCf), confirmed rendering correctly in the Qlik
            # Sense Desktop UI. Pie charts do NOT share bar/line's
            # "dataPoint" shape (showLabels/showSegmentLabels/
            # showTotalLabels) or use "slice" for the donut toggle - both
            # were guesses that the sn-pie-chart component silently
            # ignores rather than erroring on, which is why the chart
            # rendered as an empty box instead of failing loudly like
            # bar/line did. The real keys are "donut" (not "slice") and a
            # pie-specific "dataPoint" shape (auto/labelMode/labelValueMode),
            # plus a "components" entry that actually styles the slices.
            properties.update(common_chart_extras)
            properties.update({
                "visualization": "piechart",
                "script": "",
                "filter": None,
                "dimensionTitle": True,
                "donut": {"showAsDonut": False},
                "dataPoint": {
                    "auto": True,
                    "labelMode": "share",
                    "labelValueMode": "arc",
                },
                "components": [
                    {
                        "key": "slices",
                        "style": {
                            "strokeWidth": "none",
                            "strokeColor": {"index": -1, "color": "#FFFFFF"},
                            "cornerRadius": 0,
                            "innerRadius": 0.55,
                        },
                    }
                ],
            })

        response = self.send(
            "CreateChild",
            handle=self.sheet_handle,
            params=[properties]
        )

        self._add_object_to_sheet_layout(object_id, chart_type, colspan, rowspan)

        return response

    def create_bar_chart(self, dimension="Market", measure="Sales", title=None):
        title = title or f"{measure} by {dimension}"
        return self.create_chart("barchart", title, dimension=dimension, measure=measure)

    def create_line_chart(self, dimension, measure, title=None):
        title = title or f"{measure} over {dimension}"
        return self.create_chart("linechart", title, dimension=dimension, measure=measure)

    def create_pie_chart(self, dimension, measure, title=None):
        title = title or f"{measure} share by {dimension}"
        return self.create_chart("piechart", title, dimension=dimension, measure=measure)

    def create_kpi(self, measure, measure_expression=None, title=None):
        title = title or measure
        return self.create_chart(
            "kpi", title,
            measure=measure,
            measure_expression=measure_expression,
            colspan=6, rowspan=3
        )

    def create_table(self, dimension, measure, title=None):
        title = title or f"{measure} by {dimension}"
        return self.create_chart("table", title, dimension=dimension, measure=measure, colspan=24, rowspan=6)

    # ------------------------------------------------------------------
    # Persistence / teardown
    # ------------------------------------------------------------------

    def save(self):

        return self.send(
            "DoSave",
            handle=self.app_handle
        )

    def close(self):

        self.ws.close()