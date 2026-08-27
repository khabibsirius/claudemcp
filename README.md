# AI Dashboard Builder for Qlik Sense

Connect a local LLM to a Qlik Sense app and have it inspect the data model
and build sheets and charts — what Qlik Cloud gives you with a hosted
assistant, running entirely on your own machine.

Today it targets **Qlik Sense Desktop + any OpenAI-compatible model endpoint**. Enterprise on-premise is
implemented behind the same interface (see [Enterprise](#enterprise-on-premise)).

## One command

```bash
python web_app.py
```

```
  Qlik AI      -> http://127.0.0.1:8000
  Admin        -> http://127.0.0.1:8000/admin
  MCP endpoint -> http://127.0.0.1:8000/mcp
  app 'data' (desktop), model qwen2.5-coder:7b
  sign-in required
```

On the very first run it also creates an administrator and prints its
password once:

```
  ---------------------------------------------------
  First run: an administrator account was created.
  username: admin
  password: nQ8vT2wKcPzR
  This is shown once. Change it after signing in.
  ---------------------------------------------------
```

That is the whole product. The page has both halves of Qlik behind a view
switcher, the way Qlik itself does it:

- **Data load editor** — sections, script, data connections, **Load data**
- **Sheets** — existing sheets, a field-by-field view of what's loaded, and
  a box to describe a dashboard you want

…with the assistant beside both, and app and model dropdowns in the header.

**Everyone signs in, and everything behind the login is theirs**
([session.py](session.py)) — their own conversation, their own saved history,
their own settings, and on Enterprise their own Qlik connection opened as
*them*. See [Accounts](#accounts).

Point Claude Code or Claude Desktop at `http://127.0.0.1:8000/mcp` and it
sees the same open app you do, live, without starting anything else. That
endpoint needs a token and is limited to administrators — see
[Usage — MCP server](#usage--mcp-server) for why.

Secondary entry points, all optional:

| | |
| --- | --- |
| `python chat.py` | The same assistant in a terminal. It can also reload (asks first). |
| `python mcp_server.py` | MCP over stdio, if you'd rather not run the web server |
| `python main.py` | One-shot: design a dashboard with the local model and build it |
| `python check_connection.py` | Tests Qlik and the model separately when something breaks |
| `python manage_users.py` | Accounts from the command line - unlock, reset a password, add an administrator |

## How it works

```
config.py           settings from .env, validated at import
users.py            accounts, logins, API tokens and the audit trail (SQLite)
auth.py             who is asking, on every request - the gate and the cookie
session.py          one Qlik session and conversation per person
qlik_engine.py      websocket JSON-RPC client for the Qlik Engine API
chart_specs.py      property trees for each native chart type
prompts.py          system prompt + field-list prompt
llm.py              the model client, constrained to JSON where needed
dashboard_builder.py  design -> validate against real fields -> build
insights.py         reads a sheet's numbers and does the arithmetic itself
glossary.py         your own definitions, prepended to the system prompt
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
3. `insights.py` closes the other half of that gap. Building a chart never
   reads a value out of it, so an assistant asked what the dashboard *means*
   is answering from field names - and a small local model asked to fetch the
   rows and do the percentages itself returns a fluent paragraph with wrong
   numbers in it. The rows are read and the arithmetic is done in Python;
   totals, shares, concentration and period-on-period change are handed to
   the model as facts it did not compute, for it to put into the reader's own
   language. Shares and totals are withheld for a measure that does not add
   up, such as an average or a margin. `query` carries the same shares, so
   the percentage is computed in Python whichever way the assistant reaches
   the data - and a "top 5" is a share of the whole book rather than of the
   five rows on the page.
4. `mcp_server.py` wraps both as MCP tools.

## Deploying it

Windows Server, Qlik Sense Enterprise, hundreds of users: **[DEPLOYMENT.md](DEPLOYMENT.md)**
is the whole procedure — what to install, the Qlik certificate export,
Active Directory sign-in, sizing, backup, and what to do when the only
administrator is locked out.

Two ways to run it: a Windows service ([deploy/install-service.ps1](deploy/install-service.ps1)),
or a container ([Dockerfile](Dockerfile), [docker-compose.yml](docker-compose.yml)).
The service is the primary path — one fewer moving part between this and the
Qlik engine.

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
- **A model endpoint** that speaks the OpenAI API, set in `.env` as
  `OPENAI_BASE_URL` / `OPENAI_MODEL` — only for the AI-design features, not the
  low-level MCP tools.

### Your own definitions

The assistant does not know that your financial year starts in April, or
what your institution means by "retail". Copy `glossary.example.md` to
`glossary.md` and write it down in plain sentences:

```markdown
- **Retail** means `agent` = Физические лица.
- **Concentration** is the share held in the three largest regions.
  Anything above 60% must be flagged as a risk.
- We do not hold interest rates in this app. Say so rather than using
  deposit size as a proxy.
```

It is read at the start of every conversation, so an edit takes effect in
the next chat without a restart. Delete the file to turn it off. It is
reference the assistant reads, not instructions - the rules that keep it to
real fields and real figures come after it and cannot be overridden from
there.

`glossary.md` is gitignored: the definitions are yours, not the project's.

### Interface language

English, Russian and Uzbek, from the picker in the header. It follows the
browser on a first visit and is remembered per machine afterwards.

The setting does both halves: it translates the software's own words *and*
pins the language the assistant answers in. Those are separate problems - the
assistant would otherwise follow whatever language each question happened to
be typed in, so a field name written in English mid-sentence flipped the
whole answer to English.

What is never translated is the data. Field names, table names, chart types
and category values keep the spelling Qlik has for them, in every language -
a translated field name builds a chart that renders empty, and a renamed
deposit category is as wrong as a wrong number to the person reading it.
Strings live in `STRINGS` at the top of the script block in
[web/index.html](web/index.html); a new language is one more entry.

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

## Accounts

Everyone has an account, and everything behind the login belongs to the person
who signed in: their conversation, their saved history, their language and
reload settings, and — on Enterprise — their own connection to Qlik.

This is what `readiness.md` filed as blockers **B1** (there was no login
anywhere, and `/mcp` was mounted with no check at all) and **B2** (one
module-level `_state` meant two people were literally in the *same*
conversation — each saw the other's questions).

### Signing in

The first run creates an administrator and prints its password once. Set
`ADMIN_USERNAME` / `ADMIN_PASSWORD` in `.env` to choose them yourself;
leave the password blank and one is generated.

Passwords are stored as PBKDF2-HMAC-SHA256 with a per-user salt, in a format
that records its own cost — so `PBKDF2_ROUNDS` can be raised later without
invalidating anything already stored. Five failed attempts locks an account
for fifteen minutes; an administrator can unlock it.

Sessions are held server-side rather than signed into a token, so disabling
an account or resetting a password ends its sessions **now** rather than
whenever a cookie would have expired.

### The admin page

`/admin`, administrators only. Four tabs:

| | |
| --- | --- |
| **Users** | Create, edit, disable and delete; reset passwords; set role and Qlik identity; issue an MCP token; act as a user |
| **Conversations** | Every question anyone has asked and every answer they were given, filterable by person |
| **Signed in** | Live browser sessions and the Qlik connections they hold, each endable |
| **Audit** | Who signed in, who rewrote a load script, who reloaded the data, who read whose conversation |

Two things are deliberate rather than oversights:

- **Reading somebody's conversation is itself recorded.** The people whose
  questions these are cannot see this page, so the only thing keeping it
  honest is that looking leaves a mark.
- **Deleting an account keeps its conversations.** A personnel change is not
  a decision to destroy the record of what was asked of a bank's data.

**Acting as a user** puts you in their conversation, under their Qlik
identity, with a red banner across the page throughout. Both ends are
audited, and work done while borrowed names the real person as well as the
borrowed one — "the admin did it as this user" and "this user did it" are
different events, and only one of them is the user's fault. While acting as
somebody you are refused administrator's powers, because the point is to
reproduce what *they* see.

### Qlik identity

Each account carries a **user directory** and **user id** — who that person
connects to Qlik Sense as. On Enterprise the engine session is opened with
their `X-Qlik-User`, so the QMC's own app permissions decide what they can
see, and anything they build belongs to them. A user with no Qlik identity
recorded falls back to the one in `.env`, which is what makes the system
demonstrable before every account has been matched up.

**On Qlik Sense Desktop there is one engine and everyone shares it.** Desktop
permits one session per app and has no identities to impersonate, so this is
not a choice. Logins, conversations, history and the admin pages are all real
there; only the Qlik connection underneath is common. When two people are on
different apps, the shared connection is re-opened for whichever of them is
asking — slower if they ping-pong, and the alternative is answering one
person's question against another person's data.

Nothing above the socket changes between the two, so the same accounts work
unchanged when this points at Enterprise.

### Turning it off

`AUTH_ENABLED=false` returns the single-operator behaviour, for a desktop
install with one person at it. The server then **refuses to bind to anything
but loopback** — the failure this guards is somebody running `--host 0.0.0.0`
so a colleague can try it, which puts the bank's figures, the load script and
a reload button on the network with no login at all.

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

Browser → this server → Qlik and your model endpoint.

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

The model you point it at drives the Qlik Engine
API directly. It shows each action as it takes it, and asks before anything
that would replace your data.

**The model must support tool calling**, and several good ones do not —
`phi4` and `deepseek-coder` return a 400 for tools. If the configured model
can't, `chat.py` and `web_app.py` pick an installed one that can and say so,
rather than refusing to start. Set your own with `CHAT_MODEL` in `.env`,
separate from `OPENAI_MODEL` so the dashboard designer (which only needs
JSON, not tools) can keep using a different one.

The endpoint's `/v1/models` list is what the picker reads; look for `tools` under
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

### Over HTTP, from the running web server

`http://127.0.0.1:8000/mcp` is the same tools against the live session. It
needs a token: issue one from the admin page (**Users → MCP token**), which
shows it once, and send it as a bearer token.

The endpoint is at `/mcp` exactly — the sub-application mounts its own route
at `/mcp`, so mounting *that* at `/mcp` used to put it on `/mcp/mcp` while
every example said `/mcp`. It is mounted with `streamable_http_path="/"` so
the documented address is the real one.

**Connect by `127.0.0.1` or `localhost`.** MCP's transport security checks
the `Host` header against the address it is serving, as DNS-rebinding
protection, and answers `421 Misdirected Request` to anything else — so
reaching this over a LAN name needs that host allowed.

```json
{
  "mcpServers": {
    "qlik": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp",
      "headers": {"Authorization": "Bearer PASTE_THE_TOKEN"}
    }
  }
}
```

**This endpoint is limited to administrators, and that is a limitation rather
than a policy.** The MCP tool handlers run in tasks the session manager
starts from the server's *lifespan*, not from the request, so the identity
established for the request cannot reach them — every tool call lands in the
shared system session no matter who authenticated. Serving that to an
ordinary user would quietly answer their questions from somebody else's open
app and let them rewrite that app's load script. Refusing is the honest
version until the tools take a session explicitly; the browser interface is
per user and has no such caveat.

### Tools

Ten: four that build, six that read. **Nothing here changes your data or your
load script** — see *What MCP deliberately cannot do* below.

| Tool | Purpose |
| --- | --- |
| `qlik_open(app)` | Open an app — no argument lists them. Call this first. |
| `qlik_data_sources(connection, path)` | Connections → folder contents → a file's real columns **and sample rows**, without loading it |
| `qlik_build_sheet(instruction \| charts, title)` | Build a sheet — describe it in plain language, or specify the charts |
| `qlik_save()` | Persist changes |
| `qlik_query(dimensions, measures, limit)` | Read actual values, with each row's share of the whole computed in Python |
| `qlik_data_model()` | Tables, row counts, every field with cardinality and nulls, and quality problems |
| `qlik_script(tab)` | Read the load script, or one tab of it |
| `qlik_list_charts()` | Every chart with its type, dimensions and measures; every sheet with its count |
| `qlik_check_expression(expression)` | Ask Qlik whether an expression is valid, without building anything |
| `qlik_analyze_sheet(sheet, chart_ids)` | The real numbers behind a sheet's charts: totals, shares, concentration, change |

The reading half exists because an assistant that can build a chart but never
read a figure out of one is answering from field names. Building a chart does
not read a value out of it, and a small model asked to fetch the rows and do
the percentages itself returns a fluent paragraph with wrong numbers in it —
so the arithmetic is done in Python and handed over as facts.

### What MCP deliberately cannot do

`reload_data`, `write_script` and `delete_sheet` are **not** exposed, and this
is a rule enforced in code (`READ_ONLY` in [mcp_server.py](mcp_server.py))
rather than an oversight:

- MCP's tool handlers run in tasks started from the server's *lifespan*, so
  they cannot see which user authenticated.
- They all share one Qlik session.
- There is no confirmation step — nowhere to ask "this replaces every row,
  continue?".
- Nothing they did would appear in the audit trail.

A load-script rewrite that no record attributes to anybody is exactly what
this must not have. Those tools live in the web assistant and `python
chat.py`, where a person is present, destructive actions ask first, and the
change is recorded against them.

`qlik_build_sheet` takes it either way. Pass `instruction="sales by region
and customer segment, 4 charts"` and the model designs it by
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

This is **the assistant's** territory, not the MCP endpoint's — the chat
panel in the web page, and `python chat.py`. The assistant reads the script,
rewrites it, runs it, reads the errors and fixes them. Three things make that
safe to do repeatedly:

- **Edits are per-tab.** `write_script` with `mode="replace_tab"` rewrites one
  section and leaves every hand-written tab exactly as it was. Regenerating
  replaces that tab instead of appending, so iterating doesn't pile up dead
  code.
- **A broken script never lands.** The result is syntax-checked before it's
  applied; if it doesn't parse, the previous script is restored and the
  errors come back instead.
- **Every edit is undoable** with `mode="undo"`, and a failed reload returns
  the engine's own error messages so the script can actually be repaired.

So "load every CSV in my Downloads folder, merge them and clean them" is one
sentence to the assistant, which runs as:

```
add_data_source("downloads", "C:/Users/me/Downloads")
data_sources("downloads")                      → which files are there
data_sources("downloads", "orders.csv")        → real columns + sample rows
build_load_script(sources=[...], mode="concatenate")
write_script(content=..., tab="Loader")        → syntax-checked
reload_data()                                  → asks first; errors back if it fails
data_model()                                   → what's constant / mostly null
build_load_script(..., drop_fields=[...])      → second pass, now informed
```

Those are the assistant's own tool names, not MCP tools. **The MCP endpoint
cannot do any of it** — see [Usage — MCP server](#usage--mcp-server) for why
everything it exposes is read-only.

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

- **Everything needs a login**, including `/mcp`, which was previously
  mounted with no check at all. Deny by default: the list of endpoints
  reachable without signing in is short and explicit, so a new endpoint is
  protected by being forgotten rather than exposed by it.
- **Who did what is recorded.** Logins and failed logins, load-script
  rewrites, reloads, account changes, and any administrator reading somebody
  else's conversation.
- **Connection strings are redacted.** Qlik stores REST/database connection
  strings verbatim, API tokens included. Listing connections strips
  credentials before returning them, because these values otherwise reach the
  model and the conversation transcript. Loading data only ever needs a
  connection's *name* for a `lib://` path.
- **Writing the script goes to its own tab** by default, leaving
  hand-written tabs alone, and is syntax-checked before applying — a script
  that doesn't parse is rolled back rather than left in the app, and
  `mode="undo"` reverses the last write.
- **Reloading is the destructive one.** It rebuilds the data model from
  source; charts referencing fields that no longer exist go blank. It asks
  first, is not reachable over MCP at all, and is recorded in the audit
  trail against whoever ran it.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The suite runs against a fake websocket — no Qlik and no model endpoint needed.

## Notes / limitations

- **On Qlik Sense Desktop, one shared Qlik connection.** Desktop permits one
  session per app, so everyone works through it in turn and the document is
  re-opened for whoever is asking. Conversations, history and permissions are
  still per person. Enterprise gives each user a connection of their own.
- **The MCP endpoint runs in the shared system session**, which is why it is
  limited to administrators. See [Usage — MCP server](#usage--mcp-server).
- **Answers are slow and single-file per connection.** A question is 6-8 tool
  calls plus a long reply against a local model; a second question on the
  same connection waits.
- **Nothing persists until `qlik_save()`.**
- **`measure_expression` must be a simple aggregation** — `Sum([Field])`,
  `Count([Field])`, `Count(DISTINCT [Field])`, `Avg(...)`, `Min(...)`,
  `Max(...)`. Anything with `SortBy`, `Limit` or `Aggr` fails to calculate
  and renders blank rather than erroring. Rejected at validation.
- **Field names must match exactly**, character for character. A wrong name
  creates a chart that silently shows no data, so always check
  the field list first — `qlik_data_model` over MCP, or just ask the
  assistant.
- **Chart property trees are load-bearing.** The values in `chart_specs.py`
  came from real objects read back with `GetProperties`, not from the docs —
  `CreateChild` fills in no defaults, and the nebula.js renderers either
  crash or draw an empty box when a key is missing. To add a chart type,
  build one by hand in the Qlik client, dump it with
  `dump_object_properties.py`, and copy the real shape.


