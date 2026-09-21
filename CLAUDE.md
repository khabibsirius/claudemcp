# Qlik AI

A FastAPI web app (`web_app.py`, `web/`) and an MCP server (`mcp_server.py`)
that let an LLM (any OpenAI-compatible endpoint - `llm.py`, `chat_tools.py`)
read and build Qlik Sense apps through the Engine JSON API over a websocket
(`qlik_engine.py`).

**Picking up the Enterprise user-isolation work? Read `SERVER-HANDOFF.md`
before doing anything else.**

## Two modes

- `QLIK_MODE=desktop` - `ws://localhost:4848`, Qlik Sense Desktop, one shared
  connection for everyone. Cannot isolate users; do not use it to test
  isolation.
- `QLIK_MODE=enterprise` - `wss://<host>:4747` with QMC-exported client
  certificates. Each signed-in account gets its own websocket that
  impersonates its own Qlik user through the `X-Qlik-User` header.

## User isolation (Enterprise)

- The only thing separating users is each account's `qlik_directory` /
  `qlik_user_id` (`users.py`), turned into a connection by
  `session.Session._new_engine()`. Sign-in, sessions and chat history are
  separated independently and are not where app leaks come from.
- This app can never be more isolated than the QMC's own security rules.
  Testers who share a stream see the same apps even when impersonation is
  working perfectly.
- Config is not evidence. `QlikEngine.whoami()` / `identity_matches()` ask the
  engine who it thinks the connection is. `python doctor.py --skip-model` does
  that for every account and writes `doctor-report.txt`.
- On Enterprise an account with a blank identity is refused
  (`QlikIdentityMissing`) - unless `ALLOW_SHARED_QLIK_IDENTITY=true`, in which
  case it silently runs as the service account. Keep that setting unset.
- `users.create()` and `users.update()` refuse blank and duplicate identities
  (case-insensitive).

## No Qlik license on the test server (as of 2026-09-21)

Qlik Sense Enterprise November 2024 is installed on this server but **has no
valid license** - `Repository\Trace\*_License_Repository.txt` logs
"Invalid license, aborting license maintenance". The user cannot obtain an
Enterprise key. Never work around a Qlik license; it is a commercial control.

Consequences:

- No real engine is reachable, so isolation cannot be proven against Qlik here.
- Instead, `tests/fake_engine.py` is a real websocket server speaking the
  Engine JSON-RPC subset (`GetAuthenticatedUser`, `GetDocList`, `OpenDoc`) and
  enforcing per-user app visibility from the `X-Qlik-User` header. The
  isolation tests run the whole stack against it with several users.
- Qlik Sense Desktop (free via Qlik's courtesy `.unlock` file) would run the
  app but proves nothing about isolation - it is single-user by design.
- Qlik Cloud Analytics has a 30-day full-feature trial with real per-user
  identity (OAuth M2M impersonation / JWT), but needs a new connection mode in
  `qlik_engine.py`. That is the fallback if a real backend becomes necessary.

## Test server constraints

- Not domain-joined: no LDAP/AD. `LDAP_ENABLED` stays off until isolation is
  proven.
- QRS user sync is not permitted: `manage_users.py sync` and everything in
  `qrs.py` are unavailable there.
- Accounts are created by hand in the admin page, each given a real Qlik
  user's identity. Logins are handed to testers out of band.

## Files that matter for accounts

- `config.py` - every environment setting
- `users.py` - accounts database, identity guards, first-admin bootstrap
- `auth.py` - sign-in, and which session a request is given
- `session.py` - per-user engines and the shared one
- `qlik_engine.py` - connection, impersonation header, `whoami()`
- `web_app.py` - admin API (`/api/admin/users`, `/api/admin/health`)
- `preflight.py` - startup warnings; `doctor.py` - read-only deployment report
- `manage_users.py` - account CLI
- `tests/fake_engine.py` - the stand-in engine used by the isolation tests

## Tests

- `python -m pytest tests/ -q --ignore=tests/test_doctor.py`
- `tests/test_doctor.py` is left out because it can open real Qlik connections.
- Test classes and methods read as sentences
  (`TestAnEditCannotBlankAnIdentity`,
  `test_clearing_just_the_user_id_is_refused`). Match the surrounding code:
  descriptive names, sparse comments.

## Qlik gotchas

- Live Desktop testing needs Qlik Sense Desktop **running** with the app
  **closed** in its UI (sitting on the hub). With the app open in a sheet,
  Qlik overwrites anything written from outside. "I closed Qlik" usually means
  there is no engine at all - confirm which state is meant.
- Charts fail silently: the engine computes the rows and the client draws
  nothing. Never verify a chart by re-querying its data - read the created
  object back (`GetProperties`, `GetLayout`) and check the right data-page kind.

## Not in git

`.env`, `*.pem`, `certs/`, `*.db`, `local-test/` and `chart_overrides.py` are
gitignored. Every machine needs its own `.env` and certificates. Never commit
them.
