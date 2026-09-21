# Server handoff - user isolation on Qlik Sense Enterprise

Rewritten 2026-09-21 on the test server. The previous copy was deleted before
the work was finished; this replaces it. **Delete it only once isolation is
proven against a real licensed engine.**

## The problem

Testers signed in to the web app can see each other's Qlik apps, and the
problem kept coming back.

## Where things stand

**Blocked on licensing.** Qlik Sense Enterprise November 2024 is installed on
this server but has no valid licence:

```
Repository\Trace\*_License_Repository.txt
WARN ... LicenseMaintenance   Invalid license, aborting license maintenance
```

The user cannot obtain an Enterprise key. **Never work around a Qlik licence -
it is a commercial control, not a bug.** Consequences:

- No real engine can be reached, so nothing can be confirmed against Qlik here.
- Qlik Sense Desktop is free via Qlik's courtesy `.unlock` file, but it is
  single-user with one shared connection. It would run the app and prove
  nothing about isolation. Do not use it for this.
- Qlik Cloud Analytics has a 30-day full-feature trial with genuine per-user
  identity (OAuth machine-to-machine impersonation, or JWT). It is the only
  free route to a real per-user backend, and it needs a new connection mode in
  `qlik_engine.py`. Hold it in reserve.

**So isolation is proven against a stand-in engine instead.** See below.

## How isolation works - read before changing any code

- The only separator is each account's `qlik_directory` / `qlik_user_id`,
  sent as `X-Qlik-User: UserDirectory=X; UserId=Y` on that person's own
  websocket (`session.py` `_new_engine`, `qlik_engine.py` `connect`).
- A blank identity is refused at Qlik (`QlikIdentityMissing`) unless
  `ALLOW_SHARED_QLIK_IDENTITY=true`, which silently turns it into the service
  account (`QLIK_USER_DIRECTORY` / `QLIK_USER_ID`).
- **The app cannot be more isolated than the QMC's security rules.** If two
  testers can both read a stream, both see its apps, and that is correct.
- **Config is not evidence.** Only the engine's own answer to
  `GetAuthenticatedUser` says who a connection really is.

## The stand-in engine

`tests/fake_engine.py` is a fake Qlik engine reached by patching
`websocket.create_connection`, so `QlikEngine.connect()` builds the URL, the
header and the SSL options for real. It decides what a connection may see from
the `X-Qlik-User` header it was opened with.

- `World` - who can see which apps, standing in for QMC security rules.
- `Connection` - one socket, pinned to the identity its header carried.
  Serves `GetAuthenticatedUser`, `GetDocList`, `OpenDoc`.
- `Switchboard` - records every connection. `header_ignored="QS\svc"`
  reproduces the original failure: a server that resolves every connection to
  one user whatever the header asked for.
- `install()` - patches the socket factory, writes the dummy certificate files
  enterprise mode insists on.
- `enterprise_engines()` - wraps `session.QlikEngine` so enterprise settings
  actually apply. **`QlikEngine`'s defaults are bound when the module is
  imported, so monkeypatching `config` afterwards does not reach them.** This
  is the same trap that makes `tests/test_doctor.py` open real connections.

`tests/test_user_isolation.py` drives the whole chain against it: separate
connections per person, the header's exact wording, each person's own app
list, one person refused another's app, an unknown identity seeing nothing,
`identity_matches()` catching an ignored header, and the account-database
guards.

## Already done (commit 0413b09 and later)

- `qlik_engine.py`: `whoami()` and `identity_matches()`. The second returns
  `(True/False, reported)`, or `None` instead of a false pass when the reply
  cannot be parsed or the mode is not enterprise.
- `doctor.py` section 4 shows, per account: the identity asked for, the
  identity the engine reports back, and the apps it can open. It flags
  duplicate identities, an account reusing the service account's identity, and
  `ALLOW_SHARED_QLIK_IDENTITY=true`. It separates "identity confirmed but same
  apps as the service account" (a *note* - QMC rules) from "identity
  unconfirmed and same apps" (a *FAIL*). The report file is written before
  printing, so a cp1252 console cannot lose it.
- `users.py`: `check_identity_free()` refuses two accounts on one Qlik
  identity, case-insensitively. `create()` enforces identity for every auth
  source. `update()` checks the identity the account would end up with, so the
  admin page can no longer blank or borrow one.
- `tests/test_qlik_identity.py`: 12 tests for those guards.

## When a licence finally arrives

1. Export certificates from this server's QMC, put them in `QLIK_CERT_DIR`.
2. Write `.env` (there is none yet - start from `.env.example`).
3. Allocate a Professional or Analyzer access pass to **each** tester's Qlik
   user in QMC > License management. Without one a user can authenticate and
   list apps but cannot open one.
4. `python doctor.py --skip-model`, then read section 4 with the table below.
5. **Ask the user for the intended access matrix** - which tester should see
   which apps. Without it a leak cannot be told from correct behaviour.

## Reading doctor section 4

| The report says | It means | Fix it in |
|---|---|---|
| `could not connect` | certificate, host or port | `.env`: `QLIK_HOST` must be the name on the certificate; certs exported from *this* server's QMC; port 4747 open |
| engine says a different identity than asked (FAIL) | the impersonation header is not taking effect | transport - see "If the header is ignored" |
| could not read the identity back (note) | `GetAuthenticatedUser` reply not parsed | `qlik_engine.py` `identity_matches()` |
| identity confirmed, same apps as the service account (note) | the QMC grants them the same apps | QMC security rules or streams - not this repo |
| identity confirmed, lists differ, but a tester sees an app outside the matrix | shared stream, co-ownership, or a broad rule | QMC |
| two accounts on one identity (FAIL) | they are one Qlik user | admin page |
| an account connects as the service account (FAIL) | that person sees everything the service account can | admin page or `.env` |
| an account has no Qlik identity (FAIL) | refused at Qlik | admin page |

## If doctor is clean but testers still see each other's apps

- **Same browser = same person.** The session cookie (`qlikai_session`) is per
  browser profile, so two tabs in one browser are one signed-in account. Test
  each tester in a separate browser or private window.
- An admin using "act as" (`/api/admin/act-as`) sees the other user's session
  on purpose.
- `/api/admin/health` lists live sessions with user and open app.
- Make sure the web app runs the same code and `.env` as doctor, and restart it
  after `.env` changes.
- `session.py` `open_default()` silently opens a *different* app the user can
  see when `APP_NAME` will not open.

## If the header is ignored

- `QLIK_PORT=4747` must point at the **engine** directly, not a proxy or
  virtual proxy. Header auth through a proxy is a different mechanism.
- Certificates must be exported from this server's QMC.
- `qlik_engine.py` sends `UserDirectory=X; UserId=Y` **with** a space;
  `qrs.py` sends it without. If the reported identity is wrong, try the other
  form, and read the engine and proxy logs on the Qlik server.

## Server constraints

- Not domain-joined: no LDAP/AD. `LDAP_ENABLED` stays off until isolation is
  proven.
- QRS user sync is not permitted: no `manage_users.py sync`, no `/qrs` calls.
- Accounts are created by hand in the admin page, each given a real Qlik
  user's identity. Logins are handed out separately.
- The user configures the server themselves. Ask before changing server config.
- The code lives at
  `C:\code\claudemcp-before-comment-strip\claudemcp-before-comment-strip`
  (unzipped, not a git checkout - there is no `.git`). Virtual env at `.venv`.

## Deliberately not changed

- `users.py` `bootstrap()` copies `QLIK_USER_DIRECTORY` / `QLIK_USER_ID` onto
  the first admin when the accounts database is empty. Refusing would lock the
  first admin out of Qlik. Doctor flags it. The fix is operational: use a
  service identity that is not a tester, and change the admin's identity after
  first sign-in.
- `users.py` `from_directory()` rewrites the identity on **every** LDAP
  sign-in, so admin-page corrections revert. Irrelevant while LDAP is off.
  **Fix this before enabling LDAP.**
- `session.py` `open_default()` falls back to an arbitrary app on any
  `QlikEngineError`, including transient ones, and does not tell the user.
  Once a licence exists this will also fire on a missing access pass and try
  every app in turn - worth fixing then.
- `web_app.py` `_engine()` maps every `QlikEngineError` to HTTP 503, so a
  licensing refusal will read as a Qlik outage rather than "you have no access
  pass".

## Still open from the earlier code review

Irrelevant while QRS is blocked:
- `qrs.py` `_explain_403` probes `/about` as the service account, not the
  refused identity.
- `manage_users.py` `cmd_sync`: the account fallback never runs for a
  malformed `QRS_AS_USER`, and it leaves `qrs.AS_USER_OVERRIDE` set.

Relevant:
- `web_app.py` startup prints the `ADMIN_PASSWORD` remedy for every
  `UserError`, including the missing-identity one from `bootstrap()`.
- `manage_users.py` `cmd_check` repeats the preflight warnings, and crashes on
  builds without `ALLOW_SHARED_QLIK_IDENTITY`.
- `tests/conftest.py` uses `os.environ.setdefault`, so an exported `USERS_DB`
  or `HISTORY_DIR` in the shell makes tests use real data. Don't export them.
- `tests/test_doctor.py` monkeypatches `config` after `qlik_engine` has bound
  its defaults, so it opens real connections.
- `web/index.html` loads `/api/options` before `/api/state`, costing an extra
  Qlik connect per page load.

## Done looks like

- `pytest tests/ -q --ignore=tests/test_doctor.py` green, including
  `test_user_isolation.py`.
- Then, with a licence: each tester, in their own browser, sees exactly the
  apps in the agreed matrix, and doctor section 4 shows engine-confirmed
  identities with no FAIL lines.
- Then, and only then: fix `from_directory()` and start on LDAP.

## Working with this user

- They commit and push themselves. Commit only when asked.
- If they ask whether something is necessary, give a direct recommendation
  with the reason, not a menu of options.
