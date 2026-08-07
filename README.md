# AI Dashboard Builder for Qlik Sense

Connect a local LLM to a Qlik Sense app and have it inspect the data model
and build sheets and charts — what Qlik Cloud gives you with a hosted
assistant, running entirely on your own machine.

Today it targets **Qlik Sense Desktop + Ollama**. Enterprise on-premise is
implemented behind the same interface (see [Enterprise](#enterprise-on-premise)).

## One command

```bash
python web_app.py
```

```
  Qlik AI      -> http://127.0.0.1:8000
  MCP endpoint -> http://127.0.0.1:8000/mcp
  app 'data' (desktop), model qwen2.5-coder:7b
```

That is the whole product. The page has both halves of Qlik behind a view
switcher, the way Qlik itself does it:

- **Data load editor** — sections, script, data connections, **Load data**
- **Sheets** — existing sheets, a field-by-field view of what's loaded, and
  a box to describe a dashboard you want

…with the assistant beside both, and app and model dropdowns in the header.

**One Qlik connection is shared by everything** ([session.py](session.py)) —
the browser, the assistant and the MCP endpoint all work on the same open
app. That isn't an optimisation: Qlik Sense Desktop allows only one session
per app, so separate connections would lock each other out.

Point Claude Code or Claude Desktop at `http://127.0.0.1:8000/mcp` and it
sees the same open app you do, live, without starting anything else.

Secondary entry points, all optional:

| | |
| --- | --- |
| `python chat.py` | The same assistant in a terminal. It can also reload (asks first). |
| `python mcp_server.py` | MCP over stdio, if you'd rather not run the web server |
| `python main.py` | One-shot: design a dashboard with the local model and build it |
| `python check_connection.py` | Tests Qlik and Ollama separately when something breaks |

## How it works

```
config.py           settings from .env, validated at import
qlik_engine.py      websocket JSON-RPC client for the Qlik Engine API
chart_specs.py      property trees for each native chart type
prompts.py          system prompt + field-list prompt
ollama_client.py    local LLM calls, constrained to JSON
dashboard_builder.py  design -> validate against real fields -> build
mcp_server.py       all of the above as MCP tools and resources
```

1. `qlik_engine.py` opens a websocket to the Engine API and can read field
   metadata, create sheets, and create charts (bar, line, pie, table, KPI),
   positioning each on the sheet grid with the property schema Qlik's native
   renderers actually require.
2. `dashboard_builder.py` sends the field list to the LLM with a strict JSON
   schema and **validates the result against the real field list** before
   building anything. This matters: Qlik does not reject a chart that
   references a field which doesn't exist — it creates the object and renders
   an empty box. Validation is the only thing standing between a hallucinated
   field name and a silently blank chart.
3. `mcp_server.py` wraps both as MCP tools.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env      # then edit it
python check_connection.py
```

`check_connection.py` tests each dependency separately, so you find out which
one is broken rather than watching the whole pipeline fail at once. Run it
first whenever something stops working.

You'll need:

- An app already loaded with data, named to match `APP_NAME`
  (`python list_sheets.py --apps` shows what's available).
- **Ollama** running with the model in `.env` pulled, e.g.
  `ollama pull phi4:14b` — only for the AI-design features, not the
  low-level MCP tools.

### Qlik Sense Desktop (default)

No auth. Desktop only listens on 4848 while the Hub is open.

```
QLIK_MODE=desktop
QLIK_HOST=localhost
QLIK_PORT=4848
APP_NAME=data
```

### Enterprise on-premise

Certificate auth, connecting directly to the Engine API and bypassing the
proxy.

```
QLIK_MODE=enterprise
QLIK_HOST=your-engine-host
QLIK_PORT=4747
QLIK_CERT_DIR=C:/path/to/certs
QLIK_USER_DIRECTORY=YOURDOMAIN
QLIK_USER_ID=your_user
```

`QLIK_CERT_DIR` must contain `client.pem`, `client_key.pem` and `root.pem`,
exported from the QMC (**Certificates > Export certificates**, platform
independent `.pem` format). These identify which user the engine session runs
as; that user needs access to the app in the QMC.

Everything above the socket is mode-agnostic, so the same tools and prompts
work in both. Two things to know about the move:

- Desktop opens an app by name; Enterprise requires the app **GUID**.
  `open_app()` resolves a friendly name to an id via `GetDocList` first, so
  `APP_NAME` keeps working either way.
- TLS is verified against `root.pem` by default. The engine certificate is
  issued to the server's hostname, so connect by that name — not by IP. If
  you need to diagnose a handshake failure, `QLIK_SSL_VERIFY=false` disables
  verification, but it also stops authenticating the server, so turn it back
  on once it works.

Enterprise has not been tested against a live server from this repo; it
follows Qlik's documented direct-Engine-API pattern. If your environment uses
a non-default proxy setup you may need to adjust the connection block in
`qlik_engine.py`.

## Usage — AI Data Load Editor

```bash
python web_app.py --model gemma4:26b
```

Open <http://127.0.0.1:8000>. The page mirrors Qlik's Data load editor —
Sections on the left, the script in the middle, Data connections on the
right, **Save** and **Load data** at the top — with a chat panel underneath:

```
> load every csv in my downloads folder and merge them
> drop the columns that are always the same
> what columns does this file have?
```

The assistant writes into the editor; **you** press **Load data**. That
split is deliberate and mirrors Qlik itself: the model is not given the
reload action at all here, so it can prepare a script but can never rebuild
your data on its own.

### Why a page of our own

Qlik has no extension point for the Data load editor — Qlik Sense extensions
render inside sheets only, and Qlik Sense Desktop runs in its own embedded
browser window where browser extensions aren't practical either. So this is
our page, editing the **real** load script through the Engine API. What the
assistant writes is what Qlik runs.

It behaves identically against Desktop and Enterprise on-premise, because the
only thing that differs between them is how the websocket is opened.

Nothing leaves the machine: browser → this server → Qlik and Ollama.

## Usage — chatbot

```bash
python chat.py
python chat.py --app data --model gemma4:26b
```

```
> load every csv in my downloads folder and merge them
> what is total sales by market?
> clean the data - drop the columns that are always the same
> build me a sales dashboard
```

Everything runs on your machine: a local Ollama model drives the Qlik Engine
API directly. It shows each action as it takes it, and asks before anything
that would replace your data.

**The model must support tool calling**, and several good ones do not —
`phi4` and `deepseek-coder` return a 400 for tools. If the configured model
can't, `chat.py` and `web_app.py` pick an installed one that can and say so,
rather than refusing to start. Set your own with `CHAT_MODEL` in `.env`,
separate from `OLLAMA_MODEL` so the dashboard designer (which only needs
JSON, not tools) can keep using a different one.

Check any model with `ollama show <model>` and look for `tools` under
capabilities — that's read from metadata, so it doesn't load the model.

Two things keep a local model from doing damage:

- **It is not allowed to write Qlik script by hand.** Small models produce
  QVS that looks right and isn't — `DROP FIELDS` inside a `LOAD`, say. Script
  is generated by `build_load_script`, and anything applied is syntax-checked
  and rolled back if it doesn't parse.
- **Reloading asks first.** It replaces every row in the app, and a model
  will occasionally reach for it unprompted.

## Usage — CLI

```bash
python check_connection.py                 # smoke test, no LLM calls
python main.py                             # build an AI-designed dashboard
python main.py --app Sales --instruction "3 charts, focus on region"
python list_sheets.py                      # sheets as the Hub sees them
python list_sheets.py --apps               # apps and their ids
python dump_object_properties.py           # every object's full properties
python dump_object_properties.py --type piechart
```

## Usage — MCP server

```bash
python mcp_server.py     # stdio, in this environment
mcp dev mcp_server.py    # with the MCP Inspector web UI
```

`mcp dev` does **not** run the server in your current Python environment. It
launches it via `uv run` in a throwaway environment containing only `mcp`
plus the packages the server declares in `SERVER_DEPENDENCIES`
([mcp_server.py](mcp_server.py)). Without that list the server dies on
`import websocket` before registering a single tool and the Inspector shows
an empty server, so keep it in sync with `requirements.txt` whenever you add
a dependency.

For Claude Code, add to your MCP config:

```json
{
  "mcpServers": {
    "qlik-dashboard-builder": {
      "command": "python",
      "args": ["C:/project/claudemcp/mcp_server.py"]
    }
  }
}
```

### Tools

Four tools, deliberately.

| Tool | Purpose |
| --- | --- |
| `qlik_open(app)` | Open an app — no argument lists them. Call this first. |
| `qlik_data_sources(connection, path)` | Connections → folder contents → a file's real columns **and sample rows**, without loading it |
| `qlik_build_sheet(instruction \| charts, title)` | Build a sheet — describe it in plain language, or specify the charts |
| `qlik_save()` | Persist changes |

`qlik_build_sheet` takes it either way. Pass `instruction="sales by region
and customer segment, 4 charts"` and the local Ollama model designs it by
reading the data model itself — no JSON to write, nothing else required.
Pass `charts=[...]` when the caller wants to choose precisely; a capable
assistant will get a better result that way than the local model does.
Either way, every chart is validated against the real data first.

Loading data, editing the load script, cleaning, and exploring values are
**not** here. They live in [the chatbot](#usage--chatbot), where you reach
them by asking rather than by filling in tool arguments by hand.

Resources: `qlik://fields` and `qlik://sheets`.

Resources: `qlik://fields` and `qlik://sheets`.

### The load editor is meant to be edited by the AI

`qlik_script` / `qlik_edit_script` / `qlik_reload` are the centrepiece, not a
side feature — the assistant reads the script, rewrites it, runs it, reads
the errors, and fixes them. Three things make that safe to do repeatedly:

- **Edits are per-tab.** `replace_tab` rewrites one section and leaves every
  hand-written tab exactly as it was. Regenerating replaces that tab instead
  of appending, so iterating doesn't pile up dead code.
- **A broken script never lands.** The result is syntax-checked before it's
  applied; if it doesn't parse, the previous script is restored and the
  errors come back instead.
- **Every edit is undoable** with `mode='undo'`, and a failed reload returns
  the engine's own error messages so the script can actually be repaired.

So "load every CSV in my Downloads folder, merge them and clean them" runs as:

```
qlik_add_data_source("downloads", "C:/Users/me/Downloads")
qlik_data_sources("downloads")                    → which files are there
qlik_data_sources("downloads", "orders.csv")      → real columns + sample rows
qlik_generate_load_script(sources=[...], mode="concatenate")
qlik_edit_script(content=..., tab="Loader")       → syntax-checked
qlik_reload()                                     → errors back if it fails
qlik_data_model()                                 → what's constant / mostly null
qlik_generate_load_script(..., drop_fields=[...]) → second pass, now informed
```

**Cleaning knows types.** `Trim()` returns text, so trimming a numeric column
silently turns it into strings — the field loses its `$numeric` tag and every
`Sum()` over it stops working. Sample rows are used to detect numeric columns
and leave them alone. On the bundled dataset that protects 26 of 53 columns.

### Qlik Sense Desktop caches open apps — read this

Qlik Sense Desktop keeps its **own in-memory copy** of any app it has open.
Everything this tool creates is written to the `.qvf` immediately, but a Qlik
window that already had the app open will not show it. That looks exactly
like nothing happened.

Two consequences:

- **To see new sheets, close the app in Qlik and open it again.** If that
  isn't enough, restart Qlik Sense Desktop — its copy is held per process.
- **Never press Save in Qlik after the assistant has worked on the app.**
  Qlik would write its older copy over what was just saved, and the new
  sheets are gone.

The safest habit is to keep the app **closed in Qlik** while working here,
and open it when you want to look.

To make this diagnosable rather than mysterious, saves report the `.qvf`'s
size and modification time, and the assistant says so whenever it creates a
sheet — so you can tell "it didn't save" from "Qlik is showing you a cached
copy".

This is a Desktop limitation. Enterprise on-premise runs a shared engine
where sessions see each other's changes, so it largely goes away on the
migration.

### Why cardinality matters

A model given only field *names* cannot tell a category from an identifier,
and will cheerfully build a bar chart grouped by an order id with 65,000
distinct values — it renders, and it is useless. So the field list now
carries `cardinality` (which the engine returns for free), low-cardinality
fields are profiled for real sample values, and any dimension above
`MAX_DIMENSION_CARDINALITY` is rejected before it reaches Qlik.

### Safety

- **Connection strings are redacted.** Qlik stores REST/database connection
  strings verbatim, API tokens included. `qlik_list_connections` strips
  credentials before returning them, because these values otherwise reach the
  model and the conversation transcript. Loading data only ever needs a
  connection's *name* for a `lib://` path.
- **`qlik_set_script` writes to its own tab** by default, leaving
  hand-written tabs alone, and syntax-checks before applying — a script that
  doesn't parse is rolled back rather than left in the app.
  `qlik_restore_script` undoes the last write.
- **`qlik_reload` is the destructive one.** It rebuilds the data model from
  source; charts referencing fields that no longer exist go blank.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The suite runs against a fake websocket — no Qlik or Ollama needed.

## Notes / limitations

- **One shared Qlik session per server process.** Suits one person building
  one app at a time. Engine calls are serialised internally, so concurrent
  MCP tool calls are safe, but they share the same open app.
- **Nothing persists until `qlik_save()`.**
- **`measure_expression` must be a simple aggregation** — `Sum([Field])`,
  `Count([Field])`, `Count(DISTINCT [Field])`, `Avg(...)`, `Min(...)`,
  `Max(...)`. Anything with `SortBy`, `Limit` or `Aggr` fails to calculate
  and renders blank rather than erroring. Rejected at validation.
- **Field names must match exactly**, character for character. A wrong name
  creates a chart that silently shows no data, so always check
  `qlik_list_fields()` first.
- **Chart property trees are load-bearing.** The values in `chart_specs.py`
  came from real objects read back with `GetProperties`, not from the docs —
  `CreateChild` fills in no defaults, and the nebula.js renderers either
  crash or draw an empty box when a key is missing. To add a chart type,
  build one by hand in the Qlik client, dump it with
  `dump_object_properties.py`, and copy the real shape.


