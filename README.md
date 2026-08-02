# AI Dashboard Builder for Qlik Sense

Two ways to use this:

1. **Standalone script** (`main.py`) - run it, it connects to Qlik, asks a
   local LLM to design a dashboard, and builds it.
2. **MCP server** (`mcp_server.py`) - connect Claude Code, Claude Desktop, or
   any other MCP client directly to your Qlik Sense app and have it inspect
   the data model / build sheets and charts live in the conversation.

## How it works

1. `qlik_engine.py` opens a websocket JSON-RPC connection to the Qlik Sense
   Engine API and can read field metadata, create sheets, and create charts
   (bar, line, pie, table, KPI), positioning each on the sheet's grid with
   the exact property schema Qlik's native chart renderers require.
2. `dashboard_builder.py` reads the field list from the open app, sends it
   to the LLM with a strict JSON schema (`prompts.py`) and validates the
   result against the real field list before building anything.
3. `mcp_server.py` wraps both of the above as MCP tools.

## Setup

```bash
pip install -r requirements.txt
```

### Qlik connection - Desktop or Enterprise/Server

Set `QLIK_MODE` in `.env` to choose:

**Desktop** (default) - no auth, connects to Qlik Sense Desktop on your machine:
```
QLIK_MODE=desktop
QLIK_HOST=localhost
QLIK_PORT=4848
APP_NAME=data
```

**Enterprise/Server** - certificate-based auth, direct connection to the
Engine API (bypasses the proxy):
```
QLIK_MODE=enterprise
QLIK_HOST=your-engine-host
QLIK_PORT=4747
QLIK_CERT_DIR=C:/path/to/certs
QLIK_USER_DIRECTORY=YOURDOMAIN
QLIK_USER_ID=your_user
```
`QLIK_CERT_DIR` must contain `client.pem`, `client_key.pem`, and `root.pem`,
exported from the QMC's **Certificates** section (Manage Certificates >
Export Certificates). These identify which user the engine session runs as
and must have access to the target app in the QMC.

You'll also need:
- An app already loaded with data, named to match `APP_NAME`.
- **Ollama** running locally with the model in `.env` pulled, e.g.
  `ollama pull phi4:14b` (only needed for the AI-design features, not the
  low-level MCP tools).

## Usage - standalone scripts

```bash
python test.py                       # connection smoke test, no LLM
python main.py                       # build an AI-designed dashboard end-to-end
python list_sheets.py                # list sheets the way the Hub sees them
python dump_object_properties.py     # dump every object's full properties (debugging)
python dump_object_properties.py "Sales"  # ...filtered by qId/title substring
```

## Usage - MCP server

Run:
```bash
mcp dev mcp_server.py
```

Point your MCP client at it. For Claude Code, add to your MCP config:
```json
{
  "mcpServers": {
    "qlik-dashboard-builder": {
      "command": "python",
      "args": ["/full/path/to/mcp_server.py"]
    }
  }
}
```

### Tools

Low-level (compose these yourself in conversation):
- `qlik_connect(app_name)` - connect and open an app; call this first
- `qlik_list_fields()` - real field names/tags/tables, use before choosing dimensions/measures
- `qlik_create_sheet(title, description)` - creates and activates a sheet
- `qlik_create_chart(chart_type, title, dimension, measure, measure_expression)` - adds a chart to the active sheet
- `qlik_save()` - persist changes
- `qlik_list_sheets()` - confirm sheets are visible the way the Hub sees them
- `qlik_disconnect()` - close the connection

High-level:
- `qlik_build_ai_dashboard(app_name, ollama_model)` - the whole design-and-build flow in one call

## Notes / limitations

- The Qlik connection is a single shared session per server process - fine
  for one person building one app at a time.
- `qlik_create_chart`'s `measure_expression` must be a simple aggregation
  (`Sum([Field])`, `Count([Field])`, `Count(DISTINCT [Field])`, `Avg(...)`,
  `Min(...)`, `Max(...)`) - anything with `SortBy`, `Limit`, `Aggr`, etc.
  will fail to calculate and render blank rather than erroring loudly.
- Charts that reference a field name that doesn't exist create an object
  but silently show no data - always check `qlik_list_fields()` first.
- Enterprise mode has not been tested against a live server in this repo;
  the certificate/header pattern follows Qlik's documented direct Engine
  API connection method, but if your environment uses a non-default proxy
  setup you may need to adjust `qlik_engine.py`'s connection block.

