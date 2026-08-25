"""Client for the Qlik Sense Engine API (JSON-RPC over a websocket).

Talks to Qlik Sense Desktop (ws://, no auth) or Qlik Sense Enterprise
on-premise (wss://, certificate auth, direct to the engine and bypassing the
proxy). The only difference between the two is how the socket is opened -
every call above `connect()` is mode-agnostic, which is what lets the same
tooling move from Desktop to Enterprise by editing .env.

Typical use:

    with QlikEngine() as engine:
        engine.open_app("data")
        fields = engine.get_fields()
        engine.create_sheet("Sales overview")
        engine.create_chart("barchart", "Sales by region",
                            dimension="Region", measure_expression="Sum([Sales])")
        engine.save()
"""

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


# Qlik reports file times as a serial number of days, on the same epoch Excel
# uses.
QLIK_EPOCH = datetime(1899, 12, 30)

_URL_IN_CONNECTION_RE = re.compile(r"url=([^;\"]+)", re.IGNORECASE)


def redact_connection_string(connection_string, connection_type=""):
    """Strip credentials out of a connection string before it leaves here.

    REST and database connection strings routinely embed an API token,
    password or key - Qlik stores the whole thing verbatim. These strings are
    returned to an LLM and, depending on the client, sent to a hosted model
    and kept in a transcript, so the secret has to be removed at the source
    rather than trusted not to travel.

    Folder connections are just local paths and are passed through, since the
    path is the useful part and there is nothing secret in it.
    """

    if not connection_string:
        return connection_string

    if (connection_type or "").lower() == "folder":
        return connection_string

    # Keep the host so it is still obvious what the connection points at, and
    # drop the query string, which is where credentials usually sit.
    match = _URL_IN_CONNECTION_RE.search(connection_string)
    if match:
        base = match.group(1).split("?", 1)[0]
        return f"{connection_type or 'custom'} connection to {base} (credentials redacted)"

    return f"{connection_type or 'custom'} connection (details redacted)"


# The engine rejects a partial FileDataFormat with "Invalid method
# parameter(s)" rather than filling in defaults, so every key is spelled out.
# qType must be upper case.
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

# Engine FileType enum value per extension. Everything used to be previewed
# as CSV whatever the file was, so an .xlsx or .qvd "preview" returned binary
# garbage as column names - and a load script generated from those columns
# could never run.
_FILE_TYPES = {
    ".csv": "CSV", ".txt": "CSV", ".tab": "CSV",
    ".xlsx": "EXCEL_OOXML", ".xls": "EXCEL_BIFF",
    ".qvd": "QVD", ".json": "JSON", ".xml": "XML", ".parquet": "PARQUET",
}

_TAB_DELIMITER = {"qName": "tab", "qScriptCode": "'\\t'", "qNumber": 9}


def file_format_for(path):
    """The FileDataFormat GetFileTables needs, chosen by file extension."""
    suffix = os.path.splitext(path or "")[1].lower()
    file_format = dict(CSV_FORMAT, qType=_FILE_TYPES.get(suffix, "CSV"))
    if suffix == ".tab":
        file_format["qDelimiter"] = dict(_TAB_DELIMITER)
    return file_format


class QlikEngineError(Exception):
    """The Qlik Engine returned an error, or the client can't proceed."""


class QlikConnectionError(QlikEngineError):
    """Could not reach the engine, or the connection dropped mid-call."""


class QlikNotConnectedError(QlikEngineError):
    """A call needs an open socket / app / sheet that isn't there yet."""


class QlikNotFoundError(QlikEngineError):
    """Asked for something that does not exist - as opposed to something
    that exists more than once, which is a different problem needing a
    different answer. Creating a replacement is right for the first and
    makes the second steadily worse."""


def bare_field_name(name):
    """Strip the brackets Qlik uses to quote field names.

    Field names go into `qFieldDefs` unquoted ("Customer Segment"), but
    inside expressions they must be bracketed ("Sum([Customer Segment])").
    Callers - especially an LLM - mix the two up constantly, and a bracketed
    name passed through as a dimension becomes a literal field named
    "[Customer Segment]" that silently matches nothing.

    A leading "=" marks a calculated dimension, which is left untouched.
    """
    if not name:
        return name
    name = name.strip()
    if name.startswith("="):
        return name
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1].strip()
    return name


class QlikEngine:

    # Qlik Sense sheets use a 24-column responsive grid, and the client
    # translates cell positions into fractional 0-1 bounds against a square
    # unit grid - so the nominal height is the same 24.
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

        # One socket, one request in flight. `send()` writes then reads, and
        # two threads interleaving those would each consume the other's
        # reply. MCP clients can and do call tools concurrently.
        self._lock = threading.Lock()

        # Where the next chart goes on the sheet grid, plus the height of the
        # row currently being filled (see _place_on_grid).
        self._next_col = 0
        self._next_row = 0
        self._row_height = 0

        if autoconnect:
            self.connect()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

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
            # The engine's certificate is issued to the server's hostname, so
            # connecting by IP or by an alias fails verification. Turning it
            # off means the server is no longer authenticated - fine to
            # diagnose with, not to leave on.
            log.warning(
                "QLIK_SSL_VERIFY is off - the engine's certificate is not "
                "being verified. Connect by the hostname on the certificate "
                "and turn this back on once it works."
            )
            paths["cert_reqs"] = ssl.CERT_NONE
            paths["check_hostname"] = False

        return paths

    def connect(self):
        """Open the websocket. Called by __init__ unless autoconnect=False."""

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

        # The connect timeout was only for the handshake; individual engine
        # calls get the longer budget.
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
        """Close the websocket. Safe to call more than once.

        Takes the send lock so the socket is not torn down under a request
        another thread has in flight - send() writes then reads, and closing
        between the two turned a clean shutdown into a hung recv.

        Identity is cleared even when the socket is already gone:
        _await_response nulls `ws` when the engine drops the connection but
        leaves the app/sheet handles, and a reconnect after such a close ran
        against that stale identity - handles from a session that no longer
        exists, and a grid cursor half way down a sheet that is not open.
        """
        with self._lock:
            if self.ws is not None:
                try:
                    self.ws.close()
                except Exception as e:  # nothing useful to do about a failed close
                    log.debug("Ignoring error while closing the websocket: %s", e)
                self.ws = None
            self.app_handle = None
            self.app_id = None
            self.app_name = None
            self.sheet_handle = None
            self.sheet_id = None
            self._reset_grid()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    # ------------------------------------------------------------------
    # JSON-RPC plumbing
    # ------------------------------------------------------------------

    def send(self, method, handle=-1, params=None):
        """Issue one JSON-RPC call and return the parsed response."""

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

            # A reset socket surfaces as a raw OSError (ConnectionResetError),
            # not a WebSocketException, and used to escape untyped - callers
            # that retry on QlikConnectionError never saw it.
            try:
                self.ws.send(json.dumps(request))
            except (OSError, websocket.WebSocketException) as e:
                raise QlikConnectionError(f"{method} could not be sent: {e}") from e

            return self._await_response(method, request_id)

    def _await_response(self, method, request_id):
        """Read until the reply with our id turns up, or the budget runs out.

        The engine interleaves unsolicited notifications (OnConnected,
        OnAuthenticationInformation, change events) with responses, and a
        reply to a call that already timed out can still be sitting in the
        buffer. Both are skipped by id rather than treated as ours.
        """

        deadline = time.monotonic() + self.request_timeout

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise QlikConnectionError(
                    f"{method} timed out after {self.request_timeout:g}s waiting "
                    "for the engine. Raise QLIK_REQUEST_TIMEOUT if this app is "
                    "just slow to respond."
                )

            try:
                self.ws.settimeout(remaining)
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue  # loop re-checks the deadline and raises there
            except websocket.WebSocketConnectionClosedException as e:
                self.ws = None
                raise QlikConnectionError(
                    f"The engine closed the connection during {method}: {e}"
                ) from e
            except websocket.WebSocketException as e:
                raise QlikConnectionError(f"{method} failed to read a reply: {e}") from e

            try:
                response = json.loads(raw)
            except (TypeError, ValueError) as e:
                raise QlikConnectionError(
                    f"{method} got a non-JSON reply from the engine: {e}"
                ) from e

            if response.get("id") != request_id:
                # A notification (no id) or a straggler from an earlier call.
                log.debug("Skipping engine message: %s", str(raw)[:200])
                continue

            if "error" in response:
                error = response["error"]
                message = error.get("message", error) if isinstance(error, dict) else error
                raise QlikEngineError(f"{method} failed: {message}")

            return response

    # ------------------------------------------------------------------
    # Apps
    # ------------------------------------------------------------------

    def list_apps(self):
        """Every app the current session can see.

        On Desktop `id` is the .qvf path; on Enterprise it is the app GUID.
        Either way it is what OpenDoc wants, which is why open_app() resolves
        a friendly name through here first.
        """
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
        """Map a human app name to the id OpenDoc needs, or None.

        This is the single biggest Desktop/Enterprise difference: Desktop
        happily opens an app by name, Enterprise requires the GUID. Resolving
        first means APP_NAME in .env keeps working after the move.
        """
        if apps is None:
            apps = self.list_apps()

        wanted = (app_name or "").strip()
        if not wanted:
            return None

        for app in apps:  # already an id
            if app["id"] == wanted:
                return app["id"]

        for key in ("name", "path"):  # exact title, then filename
            for app in apps:
                if app[key] == wanted:
                    return app["id"]

        lowered = wanted.lower()
        for key in ("name", "path"):  # case-insensitive, .qvf optional
            for app in apps:
                candidate = app[key].lower()
                if candidate in (lowered, f"{lowered}.qvf"):
                    return app["id"]

        return None

    def open_app(self, app_name=APP_NAME):
        """Open an app by title, filename or id, and remember its handle."""

        apps = []
        target = app_name
        try:
            apps = self.list_apps()
            resolved = self.resolve_app_id(app_name, apps=apps)
            if resolved:
                target = resolved
        except QlikEngineError as e:
            # Not fatal: OpenDoc may still accept the raw name.
            log.debug("Could not list apps (%s); opening %r as given", e, app_name)

        try:
            response = self.send("OpenDoc", params=[target])
        except QlikEngineError as e:
            if "already open" in str(e).lower():
                # Error 1002, and the engine's own words for it are "A
                # document is already open" - ANY document. Qlik Sense
                # Desktop keeps one app open per engine and has no way to
                # close one, so this is usually not about app_name at all:
                # something else is still holding a different app. Naming
                # app_name as the thing to close sent people hunting for a
                # window that was never open.
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

        # A new app means the previous sheet handle is meaningless.
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

    # ------------------------------------------------------------------
    # Session objects
    # ------------------------------------------------------------------

    @contextmanager
    def _session_object(self, definition):
        """Create a temporary session object and always destroy it after.

        DestroySessionObject takes the object's *generic id* (a string), not
        its handle - passing the handle fails every time, which is how these
        used to pile up for the life of the connection.
        """
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

    # ------------------------------------------------------------------
    # Data model introspection - this is what feeds the AI
    # ------------------------------------------------------------------

    def get_fields(self):
        """Field metadata (name, tags, source tables) for the open app."""

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
                # Distinct value count, which the engine hands us for free.
                # Without it a model has no way to tell a 5-value category
                # apart from a 65,000-value id, and cheerfully groups a chart
                # by the id.
                "cardinality": item.get("qCardinal"),
                "is_numeric": "$numeric" in item.get("qTags", []),
                "is_key": "$key" in item.get("qTags", []),
            }
            for item in items
        ]

    # ------------------------------------------------------------------
    # Reading actual data - what turns a chart-drawer into an analyst
    # ------------------------------------------------------------------

    def evaluate(self, expression):
        """Evaluate a Qlik expression and return its value.

        Unlike chart creation this answers a question rather than drawing
        one, e.g. evaluate("Sum([Sales])") -> 36784735.01.
        """
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
        """Ask the engine whether an expression is valid.

        This is what makes complex measures safe. Without it the only way to
        avoid a silently-broken chart was to allow nothing but a single
        Sum/Count/Avg of one field - so set analysis, Aggr and nested
        aggregations were all off the table. The engine will happily judge
        any of them, and it reports unknown field names separately, which
        catches an invented field that is otherwise perfectly good syntax.
        """
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

        # qBadFieldNames gives character ranges into the expression, not
        # names, so the offending text has to be sliced back out - otherwise
        # the message reads "unknown field(s): None".
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
        """Cardinality plus real sample values for one field.

        Field names alone don't say what a field means - "Type" could be a
        payment type or a shipping type. Seeing five actual values settles
        it, and the distinct count says whether it can serve as a dimension
        at all.
        """
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

        # A range only means something for a numeric field. The engine
        # reports qMin/qMax as 0 for text fields, which reads as a real
        # measurement unless it's filtered out here.
        if "$numeric" in (info.get("qTags") or []):
            profile["min"] = info.get("qMin")
            profile["max"] = info.get("qMax")

        return profile

    def profile_fields(self, field_names, sample_size=10):
        """Profile several fields, skipping any that error rather than
        failing the whole batch."""
        profiles = []
        for name in field_names:
            try:
                profiles.append(self.profile_field(name, sample_size=sample_size))
            except QlikEngineError as e:
                log.debug("Could not profile %r: %s", name, e)
                profiles.append({"name": name, "error": str(e)})
        return profiles

    def query(self, dimensions=None, measures=None, limit=50, sort_by_measure=True):
        """Run an ad-hoc aggregation and return the resulting rows.

        This is the tool that lets a model *read* the data instead of only
        describing it: ask for Sales by Region and get the actual numbers
        back, so it can check an assumption before committing it to a chart.

        Sorting descending by the first measure is done through the
        hypercube's own sort order, which is how "top N" is meant to be
        expressed - putting SortBy into the expression string is what
        produces a chart that silently fails to calculate.
        """
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

        # Sort by the first measure when there is one, else by dimension.
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
                # Cells carry qNum as a real number for numeric values and as
                # the string "NaN" for text, so the type - not qIsNumeric,
                # which measure cells omit - is what to branch on.
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
        """Sheets as the Hub's sheet navigator sees them.

        Reading the SheetList rather than GetAllInfos is what distinguishes a
        sheet that is genuinely discoverable from one that merely exists as a
        raw object.
        """

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

    # ------------------------------------------------------------------
    # Sheets
    # ------------------------------------------------------------------

    def create_sheet(self, title, description="Created by AI"):
        """Create a sheet and make it the target for subsequent charts."""

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
        """The one sheet meant by a name or an id, or an error saying why not.

        Shared by opening and deleting, because "which sheet did they mean"
        has to be answered the same way for both - and getting it wrong when
        deleting costs a sheet rather than a misplaced chart.
        """
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
                # Picking one arbitrarily is how a chart lands on a sheet the
                # person isn't looking at. Make them choose by id.
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
        """Remove a sheet and everything on it.

        Destructive and not undoable from here: the charts go with the sheet.
        Nothing could delete a sheet before this existed, which is worse than
        it sounds - a session that built twenty sheets by mistake had no way
        to undo any of it, and the model, asked to tidy up, said it had.
        """
        match = self.find_sheet(name_or_id)

        self.send("DestroyObject", handle=self.app_handle, params=[match["qId"]])

        if self.sheet_id == match["qId"]:
            # The target for new charts just stopped existing.
            self.sheet_handle = None
            self.sheet_id = None
            self._reset_grid()

        return {"deleted": match["qId"], "title": match["title"],
                "charts": match["chart_count"]}

    def open_sheet(self, name_or_id):
        """Make an existing sheet the target for new charts.

        Without this, "add a chart to the Full Dashboard sheet" could only be
        served by creating a second sheet with the same name - which is
        exactly what happened, leaving duplicates and the chart nowhere the
        person was looking.
        """
        match = self.find_sheet(name_or_id)

        handle = self._object_handle(match["qId"])
        properties = self.send("GetProperties", handle=handle)["result"]["qProp"]

        self.sheet_handle = handle
        self.sheet_id = match["qId"]
        self._seed_grid(properties.get("cells") or [])

        return {"qId": match["qId"], "title": match["title"],
                "chart_count": match["chart_count"]}

    # ------------------------------------------------------------------
    # Sheet layout
    # ------------------------------------------------------------------

    def _seed_grid(self, cells):
        """Continue the layout below whatever is already on the sheet.

        The packer starts at the top-left, so adding to an existing sheet
        without this drops the new chart straight on top of the first one.
        """
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
        """Left-to-right, top-to-bottom packer returning (col, row, w, h)."""

        colspan = max(1, min(int(colspan), self.GRID_COLUMNS))
        rowspan = max(1, int(rowspan))

        if self._next_col + colspan > self.GRID_COLUMNS:
            # Drop below the *tallest* object in the row just filled, not
            # below the incoming one. Advancing by the incoming rowspan is
            # what let a short KPI wrapping under a taller chart land on top
            # of it.
            self._next_row += self._row_height or rowspan
            self._next_col = 0
            self._row_height = 0

        col, row = self._next_col, self._next_row
        self._next_col += colspan
        self._row_height = max(self._row_height, rowspan)

        return col, row, colspan, rowspan

    # One full-height chart row. Content at least this tall fills the sheet;
    # below it, a single KPI keeps a sane shape instead of being stretched
    # across the whole viewport.
    MIN_SHEET_ROWS = 4

    def _recompute_bounds(self, cells):
        """Rewrite the placed cells' fractional bounds against the sheet's height.

        Modern Qlik Sense positions sheet objects by fractional `bounds`
        (x/y/width/height, 0-1 relative to the sheet); the integer
        col/row/colspan/rowspan are kept only for backward compatibility.
        Omitting bounds doesn't error - the client quietly falls back to a
        small default at the origin, which is the "everything minimised and
        stacked at the top" symptom.

        The divisor is the content's own height, not a fixed 24. Too small a
        divisor put objects off the bottom of the sheet; too large left dead
        space - seven charts ending at row 18 divided by 24 wasted the bottom
        quarter of the sheet on every dashboard.
        """

        # Only cells this class placed carry the integer grid fields - a cell
        # authored in the Qlik client has real fractional bounds and nothing
        # else. Recomputing those from the 0/0/1/1 defaults stacked every
        # hand-placed object at the origin the moment a chart was added here,
        # so cells without col/row keep the bounds they came with.
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
        """Register a child object's position in the sheet's `cells` grid."""

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

    # A cube's mode decides which page the data comes back on, and reading
    # the wrong one makes a healthy chart look broken. A treemap is stacked
    # (qMode 'K') and a grid chart is a tree (qMode 'T'); an audit that
    # checked only qDataPages reported five working treemaps as empty. All
    # four are checked, so the verdict does not depend on knowing the mode.
    DATA_PAGE_KEYS = (
        "qDataPages", "qPivotDataPages", "qStackedDataPages", "qTreeDataPages",
    )

    def chart_renders(self, object_id):
        """Read a built chart back and say whether it will draw. (ok, detail).

        Every check made before a chart is created asks about the DATA - does
        the field exist, does the expression evaluate, does the query return
        rows. Three separate bugs got past all of it by being about the
        OBJECT: a table built from a property tree the client had outgrown, a
        pivot with its dimensions on one axis, a box plot whose cube was
        written to the top level when the component reads boxplotDef. In each
        the engine computed the numbers perfectly and the client drew nothing.

        So this asks the only question those had in common: is the data in the
        place this object's own component reads it from?

        Deliberately biased towards "ok". The caller acts on a false verdict
        by deleting the chart, and an audit written the obvious way - checking
        one page kind - was wrong about five charts out of eight. Anything
        unreadable, unrecognised, or merely odd returns ok.
        """
        try:
            handle = self._object_handle(object_id)
            properties = self.send("GetProperties", handle=handle)["result"]["qProp"]
            layout = self.send("GetLayout", handle=handle)["result"]["qLayout"]
        except (QlikEngineError, KeyError) as e:
            log.debug("Could not read %s back: %s", object_id, e)
            return True, "could not be read back - assumed fine"

        # Wherever this bundle keeps its cube. A box plot's lives under
        # boxplotDef, and the layout mirrors the properties' shape.
        owner, path = hypercube_owner(properties)
        branch = layout if path == "qHyperCubeDef" else layout.get(
            path.split(".", 1)[0], {}
        )
        hypercube = (branch or {}).get("qHyperCube")

        if hypercube is None:
            # The component may not surface a cube in its layout at all.
            # Not evidence of a fault, and not worth deleting a chart over.
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

        # qSize promises rows and not one page kind delivered them. This is
        # the box plot's signature: the cube exists and the component's own
        # branch of the tree is empty.
        return False, (
            f"it reports {size.get('qcy')} rows but returns no data on any "
            f"page, so the component has nothing to draw"
        )

    def delete_chart(self, object_id):
        """Remove one chart object and its cell in the sheet's grid.

        DestroyObject alone leaves the sheet's `cells` list pointing at an id
        that no longer exists - a hole the client renders as a broken tile,
        which is a worse outcome than the chart this is meant to remove.
        """
        self._require_app()

        placement = self.chart_sheet_map().get(object_id)
        self.send("DestroyObject", handle=self.app_handle, params=[object_id])

        if not placement:
            return {"deleted": object_id, "sheet": None}

        try:
            handle = self._object_handle(placement["sheet_id"])
            sheet_props = self.send("GetProperties", handle=handle)["result"]["qProp"]
        except QlikEngineError as e:  # pragma: no cover - sheet vanished
            log.debug("Could not tidy the sheet after deleting %s: %s", object_id, e)
            return {"deleted": object_id, "sheet": placement["sheet"]}

        cells = [c for c in (sheet_props.get("cells") or []) if c.get("name") != object_id]
        sheet_props["cells"] = cells
        self._recompute_bounds(cells)
        self.send("SetProperties", handle=handle, params=[sheet_props])

        return {"deleted": object_id, "sheet": placement["sheet"]}

    # ------------------------------------------------------------------
    # Charts
    # ------------------------------------------------------------------

    @staticmethod
    def _gen_cid(length=5):
        """A short component id in the style Qlik assigns to dimensions and
        measures (e.g. 'ayj', 'qfARm'). Only needs to be unique within the
        object, not globally."""
        return "".join(random.choices(string.ascii_letters, k=length))

    # Charts where the interesting order is "biggest first". A line chart is
    # the exception: it reads along its dimension, so sorting it by value
    # turns a trend into noise.
    # "sn-table" rather than "table": the name resolves to the bundle object
    # now, and this tuple is what gates both the descending sort and the
    # top-N limit. Leaving the old name here would not error - it would
    # quietly stop "Top 10 clients" being a top 10.
    MEASURE_SORTED_TYPES = ("barchart", "piechart", "sn-table")

    def _build_hypercube(self, chart_type, dimensions, measure, expressions, limit=None):
        # A bare string here would be iterated character by character, so
        # "Region" would become six dimensions. Silent and baffling; wrap it.
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
                # Ascending along the dimension, so a time series reads
                # left to right.
                dim["qDef"]["qSortCriterias"] = [
                    {"qSortByNumeric": 1, "qSortByAscii": 1}
                ]

            if limit and by_measure and index == 0:
                # Qlik cannot rank inside an expression, so "top 5" is
                # expressed as a dimension limit: keep the largest N by the
                # measure and drop the rest rather than showing an "Others"
                # bar that dwarfs them.
                #
                # qOtherTotalSpec sits beside qDef on the dimension, not
                # inside it. Nested in qDef the engine silently ignores it -
                # the chart just shows every value, which is exactly how a
                # "Top 5" chart ends up with twenty bars.
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
                # Descending by value. Without this a chart comes out in data
                # load order, which is what made "Sales by Region" a row of
                # bars in no discernible sequence.
                meas["qSortBy"] = {"qSortByNumeric": -1}
            hypercube["qMeasures"].append(meas)

        n_dims = len(hypercube["qDimensions"])
        n_meas = len(hypercube["qMeasures"])

        # Set for every type that has both, not just the nebula charts: a
        # table also carries qSortBy, and without the measure column leading
        # here that sort is simply ignored.
        if n_dims and n_meas:
            if by_measure:
                # A hand-built chart lists measure column(s) before dimension
                # column(s) - [1, 0] for one dimension and one measure.
                # Without it a nebula chart renders blank even though the
                # data is fine, and it is what makes the measure sort apply.
                hypercube["qInterColumnSortOrder"] = (
                    list(range(n_dims, n_dims + n_meas)) + list(range(n_dims))
                )
            else:
                hypercube["qInterColumnSortOrder"] = list(range(n_dims + n_meas))

        if needs_cid:
            hypercube["qSuppressMissing"] = True

        # A pivot table is the one type where the dimension LIST is not the
        # whole story: qNoOfLeftDims decides how many of them go down the
        # side, and the rest go across the top. Left unset, the split is
        # whatever the engine defaults to - which is how a pivot ends up
        # with every dimension stacked on one axis and nothing on the other,
        # drawing an empty grid from data that is perfectly fine.
        #
        # Qlik's own client writes this explicitly for the other qMode
        # charts it ships (the treemap carries qNoOfLeftDims: -1) and omits
        # it here, so it is set rather than assumed.
        if chart_type in CROSS_TAB_TYPES and n_dims:
            # All but the last dimension down the side, the last across the
            # top - the shape a person means by "pivot". With a single
            # dimension that leaves one on the left and nothing on top,
            # which draws as a plain table rather than as nothing.
            hypercube["qNoOfLeftDims"] = max(1, n_dims - 1)

        return hypercube

    def create_chart(self, chart_type, title, dimension=None, measure=None,
                     measure_expression=None, colspan=None, rowspan=None,
                     limit=None, colour=None, dimensions=None,
                     measure_expressions=None):
        """Add a chart to the active sheet.

        chart_type:         one of chart_specs.CHART_TYPES
        dimension:          field name to group by (omit for kpi)
        measure:            label for the measure
        measure_expression: full Qlik expression, e.g. "Sum([Sales])".
                            Defaults to Sum(measure) when not given.
        colspan/rowspan:    grid footprint; defaults per chart type.
        limit:              keep only the top N by the measure - this is how
                            "top 5" is expressed, since Qlik cannot rank
                            inside an expression.
        colour:             a colour name, #hex, or "multi" for one colour
                            per category. Ignored by kpi and table.
        """

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

        # Each type declares what it can draw, straight from Qlik's own
        # bundle. A scatter plot with one measure has nothing to plot against
        # and renders empty rather than complaining.
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

    # ------------------------------------------------------------------
    # Data connections and file inspection
    # ------------------------------------------------------------------

    def list_connections(self):
        """Data connections defined in this app (folders, databases, REST)."""
        self._require_app()
        response = self.send("GetConnections", handle=self.app_handle)
        return [
            {
                "id": c.get("qId"),
                "name": c.get("qName"),
                # Redacted for anything but a folder - see the note on
                # redact_connection_string. Loading data only ever needs the
                # connection *name* for a lib:// path, never the string.
                "path": redact_connection_string(
                    c.get("qConnectionString"), c.get("qType")
                ),
                "type": c.get("qType"),
            }
            for c in response["result"]["qConnections"]
        ]

    def resolve_connection(self, name_or_id):
        """Accept a connection's friendly name or its id, return the id."""
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
        """List the files and folders inside a folder connection."""
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
        """Add a data connection, e.g. to a folder anywhere on disk.

        Without this, only folders someone already registered in the Qlik UI
        can be loaded from - so "load the CSVs in my Downloads folder" is
        impossible however good the script is.
        """
        self._require_app()

        path = (path or "").strip()
        if not path:
            raise QlikEngineError("A connection needs a path.")

        # Qlik stores folder connections with a trailing separator.
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
        """Remove a data connection. The data it points at is untouched."""
        self._require_app()
        connection_id = self.resolve_connection(name_or_id)
        self.send("DeleteConnection", handle=self.app_handle, params=[connection_id])
        return connection_id

    def preview_file(self, name_or_id, relative_path, file_format=None, sample_rows=5):
        """Read a data file's tables, columns and a few real rows, WITHOUT
        loading it.

        The sample rows matter as much as the column names: whether a column
        needs trimming, whether dates are ISO or US format, whether "N/A"
        is being used for null - none of that is visible from a header alone,
        and all of it changes the load script.
        """
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
        """A few rows of real data from a file, as dicts. Best effort."""
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

        # With embedded labels the first row is the header, which the caller
        # already has as `columns`.
        if rows and columns and rows[0][: len(columns)] == list(columns):
            rows = rows[1:]

        return [
            {column: value for column, value in zip(columns, row)}
            for row in rows[:sample_rows]
        ]

    # ------------------------------------------------------------------
    # Load script
    # ------------------------------------------------------------------

    def get_script(self):
        """The app's current load script."""
        self._require_app()
        return self.send("GetScript", handle=self.app_handle)["result"]["qScript"]

    def check_script_syntax(self):
        """Syntax-check the script currently set on the app.

        Takes no parameters - it validates what SetScript last stored, not a
        string you hand it, so set the script first and check afterwards.
        Returns a list of errors; empty means it parsed.
        """
        self._require_app()
        errors = self.send("CheckScriptSyntax", handle=self.app_handle)["result"].get(
            "qErrors", []
        )
        # qErrLen is the length of the offending text, not an error code -
        # labelling it "code" put a meaningless number in every syntax report
        # and sent people hunting for error codes that do not exist.
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
        """Replace the app's load script.

        Returns the previous script so a caller can put it back. Overwriting
        someone's load script is destructive and easy to do by accident, so
        the old one is always handed back rather than discarded, and the new
        one is parsed before this returns.
        """
        self._require_app()

        previous = self.get_script()
        self.send("SetScript", handle=self.app_handle, params=[script])

        errors = self.check_script_syntax() if validate else []
        if errors:
            # Put back what was working rather than leaving the app holding a
            # script that cannot run. If the rollback itself fails, the
            # syntax report must still come out - it is the only thing
            # telling the caller what to fix, and an unguarded rollback error
            # used to replace it entirely.
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
        """Messages and errors from the reload that just ran.

        Read from the engine rather than the log file on disk: a failed
        reload has to explain itself to whoever is fixing the script, and on
        Enterprise the log lives on the server where we can't read it.
        """
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
        """Run the load script, replacing the app's data.

        mode 0 stops on error, 1 continues, 2 continues and logs. This
        rebuilds the data model from scratch unless partial=True, so it is
        the most destructive call in this class.
        """
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
            # A timeout or dropped socket is not "this engine lacks
            # DoReloadEx". Falling back here kicked off a SECOND full reload
            # while the first was often still running server-side.
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

    # ------------------------------------------------------------------
    # Data model / quality
    # ------------------------------------------------------------------

    def get_tables(self):
        """Tables in the loaded data model, with row counts and field stats.

        `qInformationDensity` is the fraction of rows where a field is
        populated, so 1.0 means no nulls and 0.4 means the column is 60%
        empty - which is the difference between a field worth charting and
        one worth cleaning.
        """
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

    # ------------------------------------------------------------------
    # Editing charts that already exist
    # ------------------------------------------------------------------

    def _object_handle(self, object_id):
        return self.send(
            "GetObject", handle=self.app_handle, params=[object_id]
        )["result"]["qReturn"]["qHandle"]

    @staticmethod
    def _describe_chart(object_id, properties):
        """The few things about a chart someone would want to change."""
        # A box plot keeps its cube under boxplotDef, so reading the top
        # level would report it as having no dimensions and no measures -
        # and the assistant would then describe an empty chart to the user.
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
        """Which sheet each chart sits on, keyed by chart id.

        A chart's own properties say nothing about where it lives; only the
        sheet's `cells` list knows. Without this, "which sheet is that on?"
        has no answer - and a chart on no sheet at all is invisible in Qlik
        while still existing in the app.
        """
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
        """Every chart in the app, with its id, type, title, expressions and
        the sheet it is on.

        The id is what identifies a chart for editing - Qlik has no other
        stable handle on it.
        """
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
            # "not on a sheet" is a real state: an object can exist in the app
            # while being invisible in Qlik.
            chart.update(placement.get(info["qId"], {
                "sheet": None, "sheet_id": None,
            }))
            charts.append(chart)

        return charts

    def get_chart(self, object_id):
        """One chart's editable settings."""
        self._require_app()
        handle = self._object_handle(object_id)
        properties = self.send("GetProperties", handle=handle)["result"]["qProp"]
        return self._describe_chart(object_id, properties)

    def update_chart(self, object_id, title=None, measure_expression=None,
                     measure_label=None, dimension=None, colour=None, limit=None):
        """Change an existing chart in place.

        Everything was create-only before, so a chart with the wrong measure
        had to be rebuilt - or left wrong. Only the arguments given are
        touched; the rest of the property tree is preserved exactly, which
        matters because these trees carry keys the renderer needs and a
        rewrite would lose them.
        """
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
                # Dropping an unrecognised name silently ended in "Nothing to
                # change", which read as "the colour setting does nothing".
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

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def app_file_info(self):
        """Size and on-disk modification time of the open app, or None.

        Used to prove a save actually reached the .qvf. Qlik Sense Desktop
        keeps its own in-memory copy of an open document, so a sheet created
        here is genuinely saved yet still invisible in a Qlik window that was
        already open - being able to show the file's timestamp is the
        difference between "it didn't work" and "Qlik needs reopening".
        """
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
                    # Qlik dates are days since 1899-12-30, like Excel's.
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
        """Persist the app. Nothing created in this session survives without it."""
        self._require_app()
        return self.send("DoSave", handle=self.app_handle)
