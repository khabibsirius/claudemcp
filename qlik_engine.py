import json
import logging
import os
import random
import re
import ssl
import string
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

import websocket

from chart_specs import (
    CHART_TYPES,
    COLOURS,
    CROSS_TAB_TYPES,
    build_properties,
    chart_requirements,
    colour_block,
    default_size,
    hypercube_owner,
    resolve_chart_type,
    resolve_colour,
)
from config import (
    APP_NAME,
    CLIENT_CERT,
    CLIENT_KEY,
    ENTERPRISE,
    QLIK_CERT_DIR,
    QLIK_CONNECT_TIMEOUT,
    QLIK_HOST,
    QLIK_MODE,
    QLIK_PORT,
    QLIK_REQUEST_TIMEOUT,
    QLIK_SSL_VERIFY,
    QLIK_USER_DIRECTORY,
    QLIK_USER_ID,
    ROOT_CERT,
)

log = logging.getLogger(__name__)


QLIK_EPOCH = datetime(1899, 12, 30)

_URL_IN_CONNECTION_RE = re.compile(r"url=([^;\"]+)", re.IGNORECASE)


def redact_connection_string(connection_string, connection_type=""):
    if not connection_string:
        return connection_string

    if (connection_type or "").lower() == "folder":
        return connection_string

    match = _URL_IN_CONNECTION_RE.search(connection_string)
    if match:
        base = match.group(1).split("?", 1)[0]
        return f"{connection_type or 'custom'} connection to {base} (credentials redacted)"

    return f"{connection_type or 'custom'} connection (details redacted)"


CSV_FORMAT = {
    "qType": "CSV",
    "qLabel": "embedded labels",
    "qQuote": '"',
    "qComment": "",
    "qDelimiter": {"qName": "comma", "qScriptCode": "','", "qNumber": 44},
    "qCodePage": 65001,
    "qHeaderSize": 0,
    "qRecordSize": 0,
    "qTabSize": 0,
    "qIgnoreEOF": False,
    "qFixedWidthDelimiters": "",
}

_FILE_TYPES = {
    ".csv": "CSV", ".txt": "CSV", ".tab": "CSV",
    ".xlsx": "EXCEL_OOXML", ".xls": "EXCEL_BIFF",
    ".qvd": "QVD", ".json": "JSON", ".xml": "XML", ".parquet": "PARQUET",
}

_TAB_DELIMITER = {"qName": "tab", "qScriptCode": "'\\t'", "qNumber": 9}


def file_format_for(path):
    suffix = os.path.splitext(path or "")[1].lower()
    file_format = dict(CSV_FORMAT, qType=_FILE_TYPES.get(suffix, "CSV"))
    if suffix == ".tab":
        file_format["qDelimiter"] = dict(_TAB_DELIMITER)
    return file_format


class QlikEngineError(Exception):
    pass

class QlikConnectionError(QlikEngineError):
    pass

class QlikNotConnectedError(QlikEngineError):
    pass

class QlikNotFoundError(QlikEngineError):
    pass

def bare_field_name(name):
    if not name:
        return name
    name = name.strip()
    if name.startswith("="):
        return name
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1].strip()
    return name


class QlikEngine:

    GRID_COLUMNS = 24
    GRID_ROWS = 24

    def __init__(self, host=QLIK_HOST, port=QLIK_PORT, mode=QLIK_MODE,
                 cert_dir=QLIK_CERT_DIR, user_directory=QLIK_USER_DIRECTORY,
                 user_id=QLIK_USER_ID, ssl_verify=QLIK_SSL_VERIFY,
                 connect_timeout=QLIK_CONNECT_TIMEOUT,
                 request_timeout=QLIK_REQUEST_TIMEOUT,
                 autoconnect=True):

        self.host = host
        self.port = port
        self.mode = (mode or "desktop").lower()
        self.cert_dir = cert_dir
        self.user_directory = user_directory
        self.user_id = user_id
        self.ssl_verify = ssl_verify
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout

        self.ws = None
        self.request_id = 0

        self.app_handle = None
        self.app_id = None
        self.app_name = None
        self.sheet_handle = None
        self.sheet_id = None

        self._lock = threading.Lock()

        self._next_col = 0
        self._next_row = 0
        self._row_height = 0

        if autoconnect:
            self.connect()

    @property
    def connected(self):
        return self.ws is not None

    def _enterprise_sslopt(self):
        if not self.cert_dir:
            raise QlikConnectionError(
                "QLIK_MODE=enterprise requires QLIK_CERT_DIR - a folder "
                f"containing {CLIENT_CERT}, {CLIENT_KEY} and {ROOT_CERT}, "
                "exported from the QMC's Certificates section."
            )
        if not (self.user_directory and self.user_id):
            raise QlikConnectionError(
                "QLIK_MODE=enterprise requires QLIK_USER_DIRECTORY and "
                "QLIK_USER_ID to identify which user the engine session runs "
                "as. That user needs access to the app in the QMC."
            )

        paths = {
            "certfile": os.path.join(self.cert_dir, CLIENT_CERT),
            "keyfile": os.path.join(self.cert_dir, CLIENT_KEY),
            "ca_certs": os.path.join(self.cert_dir, ROOT_CERT),
        }
        missing = [os.path.basename(p) for p in paths.values() if not os.path.isfile(p)]
        if missing:
            raise QlikConnectionError(
                f"QLIK_CERT_DIR ({self.cert_dir}) is missing {', '.join(missing)}. "
                "Export the platform-independent .pem set from the QMC: "
                "Certificates > Export certificates."
            )

        if self.ssl_verify:
            paths["cert_reqs"] = ssl.CERT_REQUIRED
            paths["check_hostname"] = True
        else:
            log.warning(
                "QLIK_SSL_VERIFY is off - the engine's certificate is not "
                "being verified. Connect by the hostname on the certificate "
                "and turn this back on once it works."
            )
            paths["cert_reqs"] = ssl.CERT_NONE
            paths["check_hostname"] = False

        return paths

    def connect(self):
        if self.connected:
            return

        if self.mode == ENTERPRISE:
            url = f"wss://{self.host}:{self.port}/app/"
            kwargs = {
                "header": [
                    "X-Qlik-User: "
                    f"UserDirectory={self.user_directory}; UserId={self.user_id}"
                ],
                "sslopt": self._enterprise_sslopt(),
            }
        else:
            url = f"ws://{self.host}:{self.port}/app/"
            kwargs = {}

        try:
            self.ws = websocket.create_connection(
                url, timeout=self.connect_timeout, **kwargs
            )
        except (OSError, websocket.WebSocketException) as e:
            raise QlikConnectionError(self._connect_hint(url, e)) from e

        self.ws.settimeout(self.request_timeout)
        log.info("Connected to %s (%s mode)", url, self.mode)

    def _connect_hint(self, url, error):
        base = f"Could not connect to the Qlik Engine at {url}: {error}"
        if self.mode == ENTERPRISE:
            return (
                f"{base}\nCheck that port {self.port} is open to this machine, "
                "that the certificates in QLIK_CERT_DIR were exported for this "
                "server, and that the host matches the name on the certificate."
            )
        return (
            f"{base}\nCheck that Qlik Sense Desktop is running and the Hub is "
            f"open - it only listens on port {self.port} while it is."
        )

    def close(self):
        with self._lock:
            if self.ws is not None:
                try:
                    self.ws.close()
                except Exception as e:
                    log.debug("Ignoring error while closing the websocket: %s", e)
                self.ws = None
            self.app_handle = None
            self.app_id = None
            self.app_name = None
            self.sheet_handle = None
            self.sheet_id = None
            self._reset_grid()

    def _drop_socket(self, why):
        if self.ws is None:
            return
        log.warning("Dropping the Qlik connection: %s", why)
        self.ws = None
        self.app_handle = None
        self.app_id = None
        self.sheet_handle = None
        self.sheet_id = None
        self._reset_grid()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def send(self, method, handle=-1, params=None):
        if not self.connected:
            raise QlikNotConnectedError(
                "Not connected to the Qlik Engine. Call connect() first."
            )

        with self._lock:
            self.request_id += 1
            request_id = self.request_id

            request = {
                "jsonrpc": "2.0",
                "id": request_id,
                "handle": handle,
                "method": method,
                "params": params if params is not None else [],
            }

            try:
                self.ws.send(json.dumps(request))
            except (OSError, websocket.WebSocketException) as e:
                self._drop_socket(f"{method} could not be sent: {e}")
                raise QlikConnectionError(f"{method} could not be sent: {e}") from e

            return self._await_response(method, request_id)

    def _await_response(self, method, request_id):
        deadline = time.monotonic() + self.request_timeout

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._drop_socket(f"{method} timed out after "
                                  f"{self.request_timeout:g}s")
                raise QlikConnectionError(
                    f"{method} timed out after {self.request_timeout:g}s waiting "
                    "for the engine. Raise QLIK_REQUEST_TIMEOUT if this app is "
                    "just slow to respond."
                )

            try:
                self.ws.settimeout(remaining)
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except websocket.WebSocketConnectionClosedException as e:
                self._drop_socket(f"the engine closed the connection during {method}")
                raise QlikConnectionError(
                    f"The engine closed the connection during {method}: {e}"
                ) from e
            except (OSError, websocket.WebSocketException) as e:
                self._drop_socket(f"{method} failed to read a reply: {e}")
                raise QlikConnectionError(f"{method} failed to read a reply: {e}") from e

            try:
                response = json.loads(raw)
            except (TypeError, ValueError) as e:
                self._drop_socket(f"{method} got a non-JSON reply")
                raise QlikConnectionError(
                    f"{method} got a non-JSON reply from the engine: {e}"
                ) from e

            if response.get("id") != request_id:
                log.debug("Skipping engine message: %s", str(raw)[:200])
                continue

            if "error" in response:
                error = response["error"]
                message = error.get("message", error) if isinstance(error, dict) else error
                raise QlikEngineError(f"{method} failed: {message}")

            return response

    def whoami(self):
        """Ask the engine which user it thinks this connection is.

        The only way to tell a working impersonation header from one the
        server ignored: the answer comes from Qlik, not from our own config.
        """
        response = self.send("GetAuthenticatedUser")
        return str(response["result"].get("qReturn") or "").strip()

    def identity_matches(self):
        """(ok, reported) - does the engine agree with the identity we asked for?

        ok is None when the answer cannot be parsed, so callers can say
        "could not tell" instead of reporting a false pass.
        """
        reported = self.whoami()
        if self.mode != ENTERPRISE:
            return None, reported

        found = dict(
            (key.strip().lower(), value.strip())
            for key, _, value in (
                part.partition("=") for part in reported.split(";")
            )
            if value.strip()
        )
        directory, user_id = found.get("userdirectory"), found.get("userid")
        if not (directory and user_id):
            return None, reported

        return (directory.lower() == (self.user_directory or "").strip().lower()
                and user_id.lower() == (self.user_id or "").strip().lower()), reported

    def list_apps(self):
        response = self.send("GetDocList")
        return [
            {
                "id": doc.get("qDocId", ""),
                "name": doc.get("qTitle") or doc.get("qDocName", ""),
                "path": doc.get("qDocName", ""),
            }
            for doc in response["result"]["qDocList"]
        ]

    def resolve_app_id(self, app_name, apps=None):
        if apps is None:
            apps = self.list_apps()

        wanted = (app_name or "").strip()
        if not wanted:
            return None

        for app in apps:
            if app["id"] == wanted:
                return app["id"]

        for key in ("name", "path"):
            for app in apps:
                if app[key] == wanted:
                    return app["id"]

        lowered = wanted.lower()
        for key in ("name", "path"):
            for app in apps:
                candidate = app[key].lower()
                if candidate in (lowered, f"{lowered}.qvf"):
                    return app["id"]

        return None

    def open_app(self, app_name=APP_NAME):
        apps = []
        target = app_name
        try:
            apps = self.list_apps()
            resolved = self.resolve_app_id(app_name, apps=apps)
            if resolved:
                target = resolved
        except QlikEngineError as e:
            log.debug("Could not list apps (%s); opening %r as given", e, app_name)

        try:
            response = self.send("OpenDoc", params=[target])
        except QlikEngineError as e:
            if "already open" in str(e).lower():
                raise QlikEngineError(
                    f"Could not open {app_name!r}: Qlik Sense Desktop keeps one "
                    "app open at a time, and another one is open now. Close it "
                    "wherever it is open - the Qlik Sense window, including its "
                    "Data load editor tab - and try again."
                ) from e

            known = ", ".join(sorted(a["name"] for a in apps if a["name"])[:20])
            hint = f" Apps visible to this session: {known}." if known else ""
            raise QlikEngineError(f"Could not open app {app_name!r}: {e}.{hint}") from e

        self.app_handle = response["result"]["qReturn"]["qHandle"]
        self.app_id = target
        self.app_name = app_name

        self.sheet_handle = None
        self.sheet_id = None
        self._reset_grid()

        return self.app_handle

    def _require_app(self):
        if self.app_handle is None:
            raise QlikNotConnectedError("No app is open. Call open_app() first.")

    def _require_sheet(self):
        if self.sheet_handle is None:
            raise QlikNotConnectedError(
                "No sheet is active. Call create_sheet() before adding charts."
            )

    @contextmanager
    def _session_object(self, definition):
        self._require_app()

        response = self.send(
            "CreateSessionObject", handle=self.app_handle, params=[definition]
        )
        qreturn = response["result"]["qReturn"]
        handle = qreturn["qHandle"]
        generic_id = qreturn.get("qGenericId")

        try:
            yield handle
        finally:
            if generic_id:
                try:
                    self.send(
                        "DestroySessionObject",
                        handle=self.app_handle,
                        params=[generic_id],
                    )
                except QlikEngineError as e:
                    log.debug("Could not destroy session object %s: %s", generic_id, e)

    def get_fields(self):
        definition = {
            "qInfo": {"qType": "FieldList"},
            "qFieldListDef": {
                "qShowSystem": False,
                "qShowHidden": False,
                "qShowSemantic": True,
                "qShowSrcTables": True,
            },
        }

        with self._session_object(definition) as handle:
            layout = self.send("GetLayout", handle=handle)

        items = layout["result"]["qLayout"]["qFieldList"]["qItems"]

        return [
            {
                "name": item["qName"],
                "tags": item.get("qTags", []),
                "tables": item.get("qSrcTables", []),
                "cardinality": item.get("qCardinal"),
                "is_numeric": "$numeric" in item.get("qTags", []),
                "is_key": "$key" in item.get("qTags", []),
            }
            for item in items
        ]

    def evaluate(self, expression):
        self._require_app()

        expression = expression.strip()
        if expression.startswith("="):
            expression = expression[1:].strip()

        response = self.send("EvaluateEx", handle=self.app_handle, params=[expression])
        value = response["result"]["qValue"]

        return {
            "expression": expression,
            "text": value.get("qText"),
            "number": value.get("qNumber") if value.get("qIsNumeric") else None,
            "is_numeric": bool(value.get("qIsNumeric")),
        }

    def check_expression(self, expression):
        self._require_app()

        expression = (expression or "").strip()
        if expression.startswith("="):
            expression = expression[1:].strip()
        if not expression:
            return {"valid": False, "error": "empty expression", "bad_fields": []}

        response = self.send(
            "CheckExpression", handle=self.app_handle, params=[expression]
        )["result"]

        error = response.get("qErrorMsg") or ""

        bad_fields = []
        for entry in response.get("qBadFieldNames") or []:
            if isinstance(entry, dict) and "qFrom" in entry:
                start = entry.get("qFrom", 0)
                bad_fields.append(expression[start:start + entry.get("qCount", 0)])
            elif isinstance(entry, dict):
                bad_fields.append(entry.get("qName"))
            else:
                bad_fields.append(entry)
        bad_fields = [f for f in bad_fields if f]

        return {
            "valid": not error and not bad_fields,
            "error": error.replace("\n", " ").strip(),
            "bad_fields": bad_fields,
            "expression": expression,
        }

    def profile_field(self, field_name, sample_size=10):
        self._require_app()

        field_name = bare_field_name(field_name)
        definition = {
            "qInfo": {"qType": "ListObject"},
            "qListObjectDef": {
                "qDef": {"qFieldDefs": [field_name]},
                "qInitialDataFetch": [
                    {"qTop": 0, "qLeft": 0, "qHeight": max(1, int(sample_size)), "qWidth": 1}
                ],
            },
        }

        with self._session_object(definition) as handle:
            layout = self.send("GetLayout", handle=handle)

        list_object = layout["result"]["qLayout"]["qListObject"]
        info = list_object.get("qDimensionInfo", {})
        pages = list_object.get("qDataPages") or [{}]
        matrix = pages[0].get("qMatrix", [])

        samples = [row[0].get("qText") for row in matrix if row]

        profile = {
            "name": field_name,
            "cardinality": info.get("qCardinal"),
            "samples": samples,
            "tags": info.get("qTags", []),
        }

        if "$numeric" in (info.get("qTags") or []):
            profile["min"] = info.get("qMin")
            profile["max"] = info.get("qMax")

        return profile

    def profile_fields(self, field_names, sample_size=10):
        profiles = []
        for name in field_names:
            try:
                profiles.append(self.profile_field(name, sample_size=sample_size))
            except QlikEngineError as e:
                log.debug("Could not profile %r: %s", name, e)
                profiles.append({"name": name, "error": str(e)})
        return profiles

    def query(self, dimensions=None, measures=None, limit=50, sort_by_measure=True):
        self._require_app()

        dimensions = [bare_field_name(d) for d in (dimensions or []) if d]
        measures = [m for m in (measures or []) if m]

        if not dimensions and not measures:
            raise QlikEngineError("query needs at least one dimension or measure.")

        limit = max(1, min(int(limit), 1000))

        dimension_defs = [{"qDef": {"qFieldDefs": [d]}} for d in dimensions]
        measure_defs = []
        for measure in measures:
            qdef = measure if measure.startswith("=") else f"={measure}"
            definition = {"qDef": {"qDef": qdef, "qLabel": measure}}
            if sort_by_measure:
                definition["qSortBy"] = {"qSortByNumeric": -1}
            measure_defs.append(definition)

        width = len(dimension_defs) + len(measure_defs)

        if sort_by_measure and measure_defs:
            sort_order = [len(dimension_defs)] + [
                i for i in range(width) if i != len(dimension_defs)
            ]
        else:
            sort_order = list(range(width))

        session_def = {
            "qInfo": {"qType": "AdHocQuery"},
            "qHyperCubeDef": {
                "qDimensions": dimension_defs,
                "qMeasures": measure_defs,
                "qInterColumnSortOrder": sort_order,
                "qSuppressMissing": True,
                "qInitialDataFetch": [
                    {"qTop": 0, "qLeft": 0, "qHeight": limit, "qWidth": width}
                ],
            },
        }

        with self._session_object(session_def) as handle:
            layout = self.send("GetLayout", handle=handle)

        hypercube = layout["result"]["qLayout"]["qHyperCube"]
        columns = [
            info.get("qFallbackTitle", "")
            for info in hypercube.get("qDimensionInfo", [])
            + hypercube.get("qMeasureInfo", [])
        ]

        pages = hypercube.get("qDataPages") or [{}]
        rows = []
        for row in pages[0].get("qMatrix", []):
            record = {}
            for column, cell in zip(columns, row):
                number = cell.get("qNum")
                record[column] = (
                    number if isinstance(number, (int, float)) else cell.get("qText")
                )
            rows.append(record)

        return {
            "columns": columns,
            "rows": rows,
            "returned_rows": len(rows),
            "total_rows": hypercube.get("qSize", {}).get("qcy"),
        }

    def list_sheets(self):
        definition = {
            "qInfo": {"qType": "SheetList"},
            "qAppObjectListDef": {
                "qType": "sheet",
                "qData": {
                    "title": "/qMetaDef/title",
                    "description": "/qMetaDef/description",
                    "cells": "/cells",
                    "rank": "/rank",
                },
            },
        }

        with self._session_object(definition) as handle:
            layout = self.send("GetLayout", handle=handle)

        items = layout["result"]["qLayout"]["qAppObjectList"]["qItems"]

        return [
            {
                "qId": item["qInfo"]["qId"],
                "title": item.get("qData", {}).get("title", ""),
                "description": item.get("qData", {}).get("description", ""),
                "chart_count": len(item.get("qData", {}).get("cells") or []),
            }
            for item in items
        ]

    def create_sheet(self, title, description="Created by AI"):
        self._require_app()

        sheet_id = "SH_" + uuid.uuid4().hex[:8]

        response = self.send(
            "CreateObject",
            handle=self.app_handle,
            params=[{
                "qInfo": {"qId": sheet_id, "qType": "sheet"},
                "qMetaDef": {"title": title, "description": description},
                "rank": -1,
                "thumbnail": {"qStaticContentUrlDef": {}},
                "columns": self.GRID_COLUMNS,
                "rows": self.GRID_ROWS,
                "cells": [],
                "qChildListDef": {
                    "qData": {
                        "title": "/title",
                        "description": "/description",
                        "meta": "/meta",
                        "order": "/order",
                        "id": "/qInfo/qId",
                        "type": "/qInfo/qType",
                        "hc": "/qHyperCubeDef",
                    }
                },
            }],
        )

        self.sheet_handle = response["result"]["qReturn"]["qHandle"]
        self.sheet_id = sheet_id
        self._reset_grid()

        return response

    def find_sheet(self, name_or_id):
        self._require_app()

        wanted = (name_or_id or "").strip()
        if not wanted:
            raise QlikEngineError("A sheet name or id is needed.")

        sheets = self.list_sheets()
        match = next((s for s in sheets if s["qId"] == wanted), None)

        if match is None:
            lowered = wanted.lower()
            titled = [
                s for s in sheets if (s["title"] or "").strip().lower() == lowered
            ]
            if len(titled) > 1:
                options = ", ".join(
                    f"{s['qId']} ({s['chart_count']} chart(s))" for s in titled
                )
                raise QlikEngineError(
                    f"{len(titled)} sheets are called {wanted!r}: {options}. "
                    "Pass the id of the one you mean."
                )
            match = titled[0] if titled else None

        if match is None:
            known = ", ".join(repr(s["title"]) for s in sheets[:12])
            raise QlikNotFoundError(
                f"No sheet called {wanted!r}. This app has: {known or '(none)'}."
            )

        return match

    def delete_sheet(self, name_or_id):
        match = self.find_sheet(name_or_id)

        self.send("DestroyObject", handle=self.app_handle, params=[match["qId"]])

        if self.sheet_id == match["qId"]:
            self.sheet_handle = None
            self.sheet_id = None
            self._reset_grid()

        return {"deleted": match["qId"], "title": match["title"],
                "charts": match["chart_count"]}

    def open_sheet(self, name_or_id):
        match = self.find_sheet(name_or_id)

        handle = self._object_handle(match["qId"])
        properties = self.send("GetProperties", handle=handle)["result"]["qProp"]

        self.sheet_handle = handle
        self.sheet_id = match["qId"]
        self._seed_grid(properties.get("cells") or [])

        return {"qId": match["qId"], "title": match["title"],
                "chart_count": match["chart_count"]}

    def _seed_grid(self, cells):
        self._reset_grid()
        if not cells:
            return
        self._next_row = max(
            int(c.get("row", 0)) + int(c.get("rowspan", 1)) for c in cells
        )

    def _reset_grid(self):
        self._next_col = 0
        self._next_row = 0
        self._row_height = 0

    def _place_on_grid(self, colspan, rowspan):
        colspan = max(1, min(int(colspan), self.GRID_COLUMNS))
        rowspan = max(1, int(rowspan))

        if self._next_col + colspan > self.GRID_COLUMNS:
            self._next_row += self._row_height or rowspan
            self._next_col = 0
            self._row_height = 0

        col, row = self._next_col, self._next_row
        self._next_col += colspan
        self._row_height = max(self._row_height, rowspan)

        return col, row, colspan, rowspan

    MIN_SHEET_ROWS = 4

    def _recompute_bounds(self, cells):
        placed = [c for c in cells if "col" in c and "row" in c]

        used_rows = max(
            [int(c.get("row", 0)) + int(c.get("rowspan", 1)) for c in placed]
            + [self.MIN_SHEET_ROWS]
        )

        for cell in placed:
            col = int(cell.get("col", 0))
            row = int(cell.get("row", 0))
            colspan = int(cell.get("colspan", 1))
            rowspan = int(cell.get("rowspan", 1))
            cell["bounds"] = {
                "x": col / self.GRID_COLUMNS,
                "y": row / used_rows,
                "width": colspan / self.GRID_COLUMNS,
                "height": rowspan / used_rows,
            }

    def _add_object_to_sheet_layout(self, object_id, object_type, colspan, rowspan):
        self._require_sheet()

        col, row, colspan, rowspan = self._place_on_grid(colspan, rowspan)

        properties = self.send("GetProperties", handle=self.sheet_handle)
        sheet_props = properties["result"]["qProp"]

        cells = sheet_props.setdefault("cells", [])
        cells.append({
            "name": object_id,
            "type": object_type,
            "col": col,
            "row": row,
            "colspan": colspan,
            "rowspan": rowspan,
        })
        self._recompute_bounds(cells)

        self.send("SetProperties", handle=self.sheet_handle, params=[sheet_props])

    DATA_PAGE_KEYS = (
        "qDataPages", "qPivotDataPages", "qStackedDataPages", "qTreeDataPages",
    )

    def chart_renders(self, object_id):
        try:
            handle = self._object_handle(object_id)
            properties = self.send("GetProperties", handle=handle)["result"]["qProp"]
            layout = self.send("GetLayout", handle=handle)["result"]["qLayout"]
        except (QlikEngineError, KeyError) as e:
            log.debug("Could not read %s back: %s", object_id, e)
            return True, "could not be read back - assumed fine"

        owner, path = hypercube_owner(properties)
        branch = layout if path == "qHyperCubeDef" else layout.get(
            path.split(".", 1)[0], {}
        )
        hypercube = (branch or {}).get("qHyperCube")

        if hypercube is None:
            return True, f"no qHyperCube at {path!r} - assumed fine"

        error = hypercube.get("qError")
        if error:
            return False, f"the engine reports an error on it: {error}"

        size = hypercube.get("qSize") or {}
        if not size.get("qcy"):
            return False, "it computes no rows, so it draws an empty chart"

        for key in self.DATA_PAGE_KEYS:
            pages = hypercube.get(key) or []
            if not pages:
                continue
            page = pages[0]
            if page.get("qMatrix") or page.get("qData") or page.get("qNodes"):
                return True, f"{key} carries data"

        return False, (
            f"it reports {size.get('qcy')} rows but returns no data on any "
            f"page, so the component has nothing to draw"
        )

    def delete_chart(self, object_id):
        self._require_app()

        placement = self.chart_sheet_map().get(object_id)
        self.send("DestroyObject", handle=self.app_handle, params=[object_id])

        if not placement:
            return {"deleted": object_id, "sheet": None}

        try:
            handle = self._object_handle(placement["sheet_id"])
            sheet_props = self.send("GetProperties", handle=handle)["result"]["qProp"]
        except QlikEngineError as e:
            log.debug("Could not tidy the sheet after deleting %s: %s", object_id, e)
            return {"deleted": object_id, "sheet": placement["sheet"]}

        cells = [c for c in (sheet_props.get("cells") or []) if c.get("name") != object_id]
        sheet_props["cells"] = cells
        self._recompute_bounds(cells)
        self.send("SetProperties", handle=handle, params=[sheet_props])

        return {"deleted": object_id, "sheet": placement["sheet"]}

    @staticmethod
    def _gen_cid(length=5):
        return "".join(random.choices(string.ascii_letters, k=length))

    MEASURE_SORTED_TYPES = ("barchart", "piechart", "sn-table")

    def _build_hypercube(self, chart_type, dimensions, measure, expressions, limit=None):
        if isinstance(dimensions, str):
            dimensions = [dimensions]
        if isinstance(expressions, str):
            expressions = [expressions]
        dimensions = [d for d in (dimensions or []) if d]
        expressions = [e for e in (expressions or []) if e]

        hypercube = {
            "qDimensions": [],
            "qMeasures": [],
            "qInitialDataFetch": [
                {"qTop": 0, "qLeft": 0, "qHeight": 100,
                 "qWidth": max(2, len(dimensions) + len(expressions))}
            ],
        }

        needs_cid = chart_type in ("barchart", "linechart", "piechart")
        by_measure = chart_type in self.MEASURE_SORTED_TYPES

        for index, dimension in enumerate(dimensions):
            dim = {"qDef": {"qFieldDefs": [dimension]}}
            if needs_cid:
                dim["qDef"]["cId"] = self._gen_cid()

            if not by_measure:
                dim["qDef"]["qSortCriterias"] = [
                    {"qSortByNumeric": 1, "qSortByAscii": 1}
                ]

            if limit and by_measure and index == 0:
                dim["qOtherTotalSpec"] = {
                    "qOtherMode": "OTHER_COUNTED",
                    "qOtherCounted": {"qv": str(int(limit))},
                    "qOtherSortMode": "OTHER_SORT_DESCENDING",
                    "qOtherLimitMode": "OTHER_GE_LIMIT",
                    "qSuppressOther": True,
                    "qForceBadValueKeeping": True,
                }

            hypercube["qDimensions"].append(dim)

        for index, expression in enumerate(expressions):
            qdef = expression if expression.startswith("=") else f"={expression}"
            label = (measure or expression) if index == 0 else expression
            meas = {"qDef": {"qDef": qdef, "qLabel": label}}
            if needs_cid:
                meas["qDef"]["cId"] = self._gen_cid()
            if by_measure and index == 0:
                meas["qSortBy"] = {"qSortByNumeric": -1}
            hypercube["qMeasures"].append(meas)

        n_dims = len(hypercube["qDimensions"])
        n_meas = len(hypercube["qMeasures"])

        if n_dims and n_meas:
            if by_measure:
                hypercube["qInterColumnSortOrder"] = (
                    list(range(n_dims, n_dims + n_meas)) + list(range(n_dims))
                )
            else:
                hypercube["qInterColumnSortOrder"] = list(range(n_dims + n_meas))

        if needs_cid:
            hypercube["qSuppressMissing"] = True

        if chart_type in CROSS_TAB_TYPES and n_dims:
            hypercube["qNoOfLeftDims"] = max(1, n_dims - 1)

        return hypercube

    def create_chart(self, chart_type, title, dimension=None, measure=None,
                     measure_expression=None, colspan=None, rowspan=None,
                     limit=None, colour=None, dimensions=None,
                     measure_expressions=None):
        self._require_sheet()

        resolved = resolve_chart_type(chart_type)
        if not resolved:
            raise QlikEngineError(
                f"Unknown chart type {chart_type!r}. Supported: "
                f"{', '.join(CHART_TYPES)}."
            )
        chart_type = resolved

        dimensions = [
            bare_field_name(d) for d in (
                dimensions if dimensions is not None
                else ([dimension] if dimension else [])
            ) if d
        ]

        expressions = [
            e.strip() for e in (
                measure_expressions if measure_expressions is not None
                else ([measure_expression] if measure_expression else [])
            ) if e and e.strip()
        ]
        if not expressions and measure:
            expressions = [f"Sum([{bare_field_name(measure)}])"]

        (min_dims, max_dims), (min_meas, max_meas) = chart_requirements(chart_type)

        if len(dimensions) < min_dims:
            raise QlikEngineError(
                f"{chart_type} needs at least {min_dims} dimension(s), got "
                f"{len(dimensions)}."
            )
        if max_dims and len(dimensions) > max_dims:
            raise QlikEngineError(
                f"{chart_type} takes at most {max_dims} dimension(s), got "
                f"{len(dimensions)}."
            )
        if len(expressions) < min_meas:
            raise QlikEngineError(
                f"{chart_type} needs at least {min_meas} measure(s), got "
                f"{len(expressions)}."
            )
        if max_meas and len(expressions) > max_meas:
            raise QlikEngineError(
                f"{chart_type} takes at most {max_meas} measure(s), got "
                f"{len(expressions)}."
            )

        default_colspan, default_rowspan = default_size(chart_type)
        colspan = default_colspan if colspan is None else colspan
        rowspan = default_rowspan if rowspan is None else rowspan

        object_id = f"{chart_type[:3].upper()}_{uuid.uuid4().hex[:8]}"
        hypercube = self._build_hypercube(
            chart_type, dimensions, measure, expressions, limit=limit
        )
        properties = build_properties(
            chart_type, object_id, title, hypercube, colour=colour
        )

        response = self.send("CreateChild", handle=self.sheet_handle, params=[properties])

        self._add_object_to_sheet_layout(object_id, chart_type, colspan, rowspan)
        log.debug("Created %s %r (qId=%s)", chart_type, title, object_id)

        return response

    def create_bar_chart(self, dimension, measure, title=None, **kwargs):
        return self.create_chart(
            "barchart", title or f"{measure} by {dimension}",
            dimension=dimension, measure=measure, **kwargs
        )

    def create_line_chart(self, dimension, measure, title=None, **kwargs):
        return self.create_chart(
            "linechart", title or f"{measure} over {dimension}",
            dimension=dimension, measure=measure, **kwargs
        )

    def create_pie_chart(self, dimension, measure, title=None, **kwargs):
        return self.create_chart(
            "piechart", title or f"{measure} share by {dimension}",
            dimension=dimension, measure=measure, **kwargs
        )

    def create_kpi(self, measure, measure_expression=None, title=None, **kwargs):
        return self.create_chart(
            "kpi", title or measure,
            measure=measure, measure_expression=measure_expression, **kwargs
        )

    def create_table(self, dimension, measure, title=None, **kwargs):
        return self.create_chart(
            "table", title or f"{measure} by {dimension}",
            dimension=dimension, measure=measure, **kwargs
        )

    def list_connections(self):
        self._require_app()
        response = self.send("GetConnections", handle=self.app_handle)
        return [
            {
                "id": c.get("qId"),
                "name": c.get("qName"),
                "path": redact_connection_string(
                    c.get("qConnectionString"), c.get("qType")
                ),
                "type": c.get("qType"),
            }
            for c in response["result"]["qConnections"]
        ]

    def resolve_connection(self, name_or_id):
        connections = self.list_connections()
        for connection in connections:
            if connection["id"] == name_or_id:
                return connection["id"]
        lowered = (name_or_id or "").strip().lower()
        for connection in connections:
            if (connection["name"] or "").lower() == lowered:
                return connection["id"]
        known = ", ".join(c["name"] for c in connections if c["name"])
        raise QlikEngineError(
            f"No data connection named {name_or_id!r}. Available: {known or '(none)'}."
        )

    def browse_connection(self, name_or_id, relative_path=""):
        self._require_app()
        connection_id = self.resolve_connection(name_or_id)
        response = self.send(
            "GetFolderItemsForConnection",
            handle=self.app_handle,
            params=[connection_id, relative_path],
        )
        return [
            {"name": item.get("qName"), "type": item.get("qType")}
            for item in response["result"]["qFolderItems"]
        ]

    def create_connection(self, name, path, connection_type="folder"):
        self._require_app()

        path = (path or "").strip()
        if not path:
            raise QlikEngineError("A connection needs a path.")

        if connection_type == "folder" and not path.endswith(("\\", "/")):
            path += "\\" if "\\" in path else "/"

        response = self.send(
            "CreateConnection",
            handle=self.app_handle,
            params=[{
                "qName": name,
                "qConnectionString": path,
                "qType": connection_type,
            }],
        )

        return {
            "id": response["result"].get("qConnectionId"),
            "name": name,
            "path": path,
            "type": connection_type,
        }

    def delete_connection(self, name_or_id):
        self._require_app()
        connection_id = self.resolve_connection(name_or_id)
        self.send("DeleteConnection", handle=self.app_handle, params=[connection_id])
        return connection_id

    def preview_file(self, name_or_id, relative_path, file_format=None, sample_rows=5):
        self._require_app()
        connection_id = self.resolve_connection(name_or_id)
        file_format = file_format or file_format_for(relative_path)

        tables = self.send(
            "GetFileTables",
            handle=self.app_handle,
            params=[connection_id, relative_path, file_format],
        )["result"]["qTables"]

        result = {"path": relative_path, "tables": []}
        for table in tables:
            table_name = table.get("qName", "")
            fields = self.send(
                "GetFileTableFields",
                handle=self.app_handle,
                params=[connection_id, relative_path, file_format, table_name],
            )["result"].get("qFields", [])

            entry = {
                "name": table_name,
                "columns": [f.get("qName") for f in fields],
                "column_count": len(fields),
            }

            if sample_rows:
                entry["sample_rows"] = self._file_sample_rows(
                    connection_id, relative_path, file_format, table_name,
                    entry["columns"], sample_rows,
                )

            result["tables"].append(entry)

        return result

    def _file_sample_rows(self, connection_id, relative_path, file_format,
                          table_name, columns, sample_rows):
        try:
            preview = self.send(
                "GetFileTablePreview",
                handle=self.app_handle,
                params=[connection_id, relative_path, file_format, table_name],
            )["result"].get("qPreview", [])
        except QlikEngineError as e:
            log.debug("No preview for %s: %s", relative_path, e)
            return []

        rows = [entry.get("qValues", []) for entry in preview]

        if rows and columns and rows[0][: len(columns)] == list(columns):
            rows = rows[1:]

        return [
            {column: value for column, value in zip(columns, row)}
            for row in rows[:sample_rows]
        ]

    def get_script(self):
        self._require_app()
        return self.send("GetScript", handle=self.app_handle)["result"]["qScript"]

    def check_script_syntax(self):
        self._require_app()
        errors = self.send("CheckScriptSyntax", handle=self.app_handle)["result"].get(
            "qErrors", []
        )
        return [
            {
                "line": e.get("qLineInTab"),
                "tab": e.get("qTabIx"),
                "column": e.get("qColInLine"),
                "length": e.get("qErrLen"),
            }
            for e in errors
        ]

    def set_script(self, script, validate=True):
        self._require_app()

        previous = self.get_script()
        self.send("SetScript", handle=self.app_handle, params=[script])

        errors = self.check_script_syntax() if validate else []
        if errors:
            applied = "was not applied"
            try:
                self.send("SetScript", handle=self.app_handle, params=[previous])
            except QlikEngineError as e:
                log.warning("Could not restore the previous script: %s", e)
                applied = "the previous script could not be restored"
            raise QlikEngineError(
                f"Script has {len(errors)} syntax error(s) and {applied}: {errors}"
            )

        return previous

    def get_reload_progress(self):
        try:
            response = self.send("GetProgress", handle=-1, params=[0])
        except QlikEngineError as e:
            log.debug("GetProgress unavailable: %s", e)
            return {"messages": [], "errors": []}

        data = response["result"].get("qProgressData", {})

        def text(entry):
            parameters = [p for p in entry.get("qMessageParameters", []) if p]
            return " ".join(parameters).strip()

        return {
            "messages": [t for t in map(text, data.get("qPersistentProgressMessages", [])) if t],
            "errors": [t for t in map(text, data.get("qErrorData", [])) if t],
        }

    def reload_data(self, mode=0, partial=False):
        self._require_app()

        result = {"success": False, "log_file": None}

        try:
            response = self.send(
                "DoReloadEx", handle=self.app_handle, params=[mode, partial, False]
            )
            reload_result = response["result"].get("qResult", {})
            result = {
                "success": bool(reload_result.get("qSuccess")),
                "log_file": reload_result.get("qScriptLogFile"),
                "memory_constrained": bool(reload_result.get("qEndedWithMemoryConstraint")),
            }
        except QlikConnectionError:
            raise
        except QlikEngineError as e:
            log.debug("DoReloadEx unavailable (%s); falling back to DoReload", e)
            response = self.send(
                "DoReload", handle=self.app_handle, params=[mode, partial, False]
            )
            result = {
                "success": bool(response["result"].get("qReturn")),
                "log_file": None,
            }

        result.update(self.get_reload_progress())
        return result

    def get_tables(self):
        self._require_app()

        response = self.send(
            "GetTablesAndKeys",
            handle=self.app_handle,
            params=[{"qcx": 1000, "qcy": 1000}, {"qcx": 0, "qcy": 0}, 30, True, False],
        )

        tables = []
        for table in response["result"].get("qtr", []):
            rows = table.get("qNoOfRows", 0)
            fields = []
            for field in table.get("qFields", []):
                non_nulls = field.get("qnNonNulls", 0)
                fields.append({
                    "name": field.get("qName"),
                    "rows": field.get("qnRows", rows),
                    "non_nulls": non_nulls,
                    "null_count": max(0, rows - non_nulls),
                    "density": field.get("qInformationDensity"),
                    "distinct": field.get("qnTotalDistinctValues"),
                    "has_duplicates": field.get("qHasDuplicates"),
                    "is_key": bool(field.get("qKeyType") not in (None, "NOT_KEY")),
                })
            tables.append({"name": table.get("qName"), "rows": rows, "fields": fields})

        return tables

    def _object_handle(self, object_id):
        return self.send(
            "GetObject", handle=self.app_handle, params=[object_id]
        )["result"]["qReturn"]["qHandle"]

    @staticmethod
    def _describe_chart(object_id, properties):
        owner, _path = hypercube_owner(properties)
        hypercube = owner.get("qHyperCubeDef", {}) or {}

        dimensions = [
            (d.get("qDef", {}).get("qFieldDefs") or [""])[0]
            for d in hypercube.get("qDimensions", [])
        ]
        measures = [
            m.get("qDef", {}).get("qDef", "") for m in hypercube.get("qMeasures", [])
        ]

        return {
            "id": object_id,
            "type": properties.get("qInfo", {}).get("qType"),
            "title": properties.get("title"),
            "dimensions": [d for d in dimensions if d],
            "measures": [m for m in measures if m],
        }

    def chart_sheet_map(self):
        self._require_app()

        placement = {}
        for sheet in self.list_sheets():
            try:
                handle = self._object_handle(sheet["qId"])
                properties = self.send("GetProperties", handle=handle)["result"]["qProp"]
            except QlikEngineError as e:
                log.debug("Could not read sheet %s: %s", sheet["qId"], e)
                continue
            for cell in properties.get("cells") or []:
                name = cell.get("name")
                if name:
                    placement[name] = {
                        "sheet": sheet["title"] or "(untitled)",
                        "sheet_id": sheet["qId"],
                    }
        return placement

    def list_charts(self, limit=200):
        self._require_app()
        placement = self.chart_sheet_map()

        infos = self.send("GetAllInfos", handle=self.app_handle)["result"]["qInfos"]
        charts = []

        for info in infos:
            if info["qType"] not in CHART_TYPES:
                continue
            if len(charts) >= limit:
                break
            try:
                handle = self._object_handle(info["qId"])
                properties = self.send("GetProperties", handle=handle)["result"]["qProp"]
            except QlikEngineError as e:
                log.debug("Could not read %s: %s", info["qId"], e)
                continue
            chart = self._describe_chart(info["qId"], properties)
            chart.update(placement.get(info["qId"], {
                "sheet": None, "sheet_id": None,
            }))
            charts.append(chart)

        return charts

    def get_chart(self, object_id):
        self._require_app()
        handle = self._object_handle(object_id)
        properties = self.send("GetProperties", handle=handle)["result"]["qProp"]
        return self._describe_chart(object_id, properties)

    def update_chart(self, object_id, title=None, measure_expression=None,
                     measure_label=None, dimension=None, colour=None, limit=None):
        self._require_app()

        if measure_expression:
            check = self.check_expression(measure_expression)
            if not check["valid"]:
                raise QlikEngineError(
                    f"Expression {measure_expression!r} was not applied: "
                    + (check["error"] or f"unknown field(s): {', '.join(check['bad_fields'])}")
                )

        handle = self._object_handle(object_id)
        properties = self.send("GetProperties", handle=handle)["result"]["qProp"]
        hypercube = properties.setdefault("qHyperCubeDef", {})
        changed = []

        if title is not None:
            properties["title"] = title
            changed.append("title")

        if measure_expression:
            measures = hypercube.setdefault("qMeasures", [])
            if not measures:
                measures.append({"qDef": {}})
            qdef = measure_expression.strip()
            measures[0]["qDef"]["qDef"] = qdef if qdef.startswith("=") else f"={qdef}"
            if measure_label:
                measures[0]["qDef"]["qLabel"] = measure_label
            changed.append("measure")

        if dimension:
            dimensions = hypercube.setdefault("qDimensions", [])
            if not dimensions:
                dimensions.append({"qDef": {}})
            dimensions[0]["qDef"]["qFieldDefs"] = [bare_field_name(dimension)]
            changed.append("dimension")

        if limit is not None and hypercube.get("qDimensions"):
            if limit:
                hypercube["qDimensions"][0]["qOtherTotalSpec"] = {
                    "qOtherMode": "OTHER_COUNTED",
                    "qOtherCounted": {"qv": str(int(limit))},
                    "qOtherSortMode": "OTHER_SORT_DESCENDING",
                    "qOtherLimitMode": "OTHER_GE_LIMIT",
                    "qSuppressOther": True,
                    "qForceBadValueKeeping": True,
                }
            else:
                hypercube["qDimensions"][0].pop("qOtherTotalSpec", None)
            changed.append("limit")

        if colour:
            resolved = resolve_colour(colour)
            if not resolved:
                raise QlikEngineError(
                    f"Unknown colour {colour!r}. Use a #rrggbb value, 'multi', "
                    f"or one of: {', '.join(sorted(set(COLOURS)))}."
                )
            properties["color"] = {
                **(properties.get("color") or {}), **colour_block(resolved)
            }
            changed.append("colour")

        if not changed:
            return {"id": object_id, "changed": [], "note": "Nothing to change."}

        self.send("SetProperties", handle=handle, params=[properties])
        result = self._describe_chart(object_id, properties)
        result["changed"] = changed
        return result

    def app_file_info(self):
        self._require_app()

        try:
            docs = self.send("GetDocList")["result"]["qDocList"]
        except QlikEngineError as e:
            log.debug("Could not read the document list: %s", e)
            return None

        for doc in docs:
            if doc.get("qDocId") == self.app_id or doc.get("qTitle") == self.app_name:
                serial = doc.get("qFileTime")
                modified = None
                if serial:
                    modified = (QLIK_EPOCH + timedelta(days=serial)).replace(
                        microsecond=0
                    ).isoformat(sep=" ")
                return {
                    "path": doc.get("qDocId"),
                    "size_bytes": doc.get("qFileSize"),
                    "modified": modified,
                }
        return None

    def save(self):
        self._require_app()
        return self.send("DoSave", handle=self.app_handle)
