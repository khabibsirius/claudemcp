# Deploying Qlik AI

Windows Server, Qlik Sense Enterprise on-premise, an OpenAI-compatible model
endpoint, and hundreds of users.

This is the whole procedure, in the order it has to happen. Where something
is a decision rather than a step, it says so.

---

## What you are deploying

```
   browsers ──HTTPS──▶  reverse proxy  ──HTTP──▶  Qlik AI (this)
                                                    │
                                    wss:4747 ───────┼─────── Qlik Sense
                                    (per-user certs)│         Enterprise
                                                    │
                                    HTTPS ──────────┘         model endpoint
```

One Windows service. It holds a Qlik Engine session per signed-in user,
opened **as that user**, and calls a model endpoint over HTTPS. Its own state
is two things: an SQLite account database and a directory of conversation
files.

---

## What to install

Two ways to run it. **The Windows service is the primary path** — one fewer
moving part between this and the Qlik engine, and certificate and DNS
problems are far easier to diagnose without a container boundary in the way.
Docker is there for hosts that already run it.

### Needed either way

| | Why | Check |
|---|---|---|
| **Qlik Sense Enterprise** | The engine, reachable on **4747** from this host | `Test-NetConnection qlik-engine -Port 4747` |
| **Qlik certificates** | `client.pem`, `client_key.pem`, `root.pem` from the QMC | step 1 |
| **Qlik access passes** | **One per user.** 300 users = 300 passes | your licence |
| **A model endpoint** | URL + key, OpenAI-compatible (your Qwen server) | step 4 |
| **A domain controller** | Reachable on **636** (LDAPS), if using AD sign-in | `Test-NetConnection dc01 -Port 636` |
| **TLS** | A reverse proxy, or a certificate for this host | step 5 |

### Windows service (primary)

| | Version | Notes |
|---|---|---|
| **Python** | 3.10 or later | 3.12 recommended. `py -0p` lists what is installed |
| **pip packages** | `requirements.txt` | 7 direct, all pure Python — no compiler needed |
| **NSSM** | any | [nssm.cc/download](https://nssm.cc/download) — the service wrapper. WinSW or a Scheduled Task also work |
| **IIS** | with **URL Rewrite** + **ARR** | Only if IIS is your reverse proxy. `Install-WindowsFeature Web-Server` |

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Nothing here needs a C compiler. `ldap3` is pure Python on purpose —
`python-ldap` needs OpenLDAP's C libraries and is a bad afternoon on a
locked-down server.

### Docker (alternative)

| | Notes |
|---|---|
| **Docker Desktop** or **Docker Engine** | With the **WSL2** backend on Windows Server |
| **WSL2** | `wsl --install`, then a reboot |
| Nothing else | Python and the packages are inside the image |

```powershell
docker compose up -d
docker compose logs -f
```

See [Option B: Docker](#option-b-docker) below for what differs.

### Not needed

Node.js, a C compiler, a database server, Redis. The model runs on your own
OpenAI-compatible endpoint, not on this box. `tools/` contains Node scripts used once
to extract Qlik's chart property trees — they are not part of running this.

---

## 1. Qlik certificates

The app connects **directly to the Engine API**, bypassing the proxy. That
needs the server's certificates.

In the QMC: **Certificates → Export certificates**. Choose the
**platform-independent `.pem`** format. You get three files:

```
client.pem      client_key.pem      root.pem
```

Put them somewhere the service account can read and nobody else can:

```powershell
$dir = "C:\ProgramData\QlikAI\certs"
New-Item -ItemType Directory -Force -Path $dir
# copy the three .pem files in, then lock it down:
icacls $dir /inheritance:r
icacls $dir /grant "DOMAIN\svc_qlikai:(OI)(CI)R"
icacls $dir /grant "Administrators:(OI)(CI)F"
```

> **`QLIK_HOST` must be the hostname on the certificate.** The engine's
> certificate is issued to the server's name, so connecting by IP or by an
> alias fails TLS verification. `QLIK_SSL_VERIFY=false` will get you past it
> and also stops authenticating the server — use it to *diagnose*, never to
> ship.

Check the firewall allows this host to reach the engine on 4747.

---

## 2. Install

```powershell
cd C:\apps\qlikai
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

---

## 3. Configure

**Start from the production template** — every value you need is in it,
marked with `<ANGLE BRACKETS>`, and everything else already has a working
default:

```powershell
Copy-Item deploy\production.env.template .env
notepad .env          # replace every <ANGLE BRACKET>
```

For reference, the settings that matter:

```ini
# --- Qlik ---------------------------------------------------------------
QLIK_MODE=enterprise
QLIK_HOST=qlik-engine.bank.internal      # the name on the certificate
QLIK_PORT=4747
QLIK_CERT_DIR=C:\ProgramData\QlikAI\certs
QLIK_USER_DIRECTORY=BANK                 # fallback identity, see below
QLIK_USER_ID=svc_qlikai
QLIK_SSL_VERIFY=true
APP_NAME=<the app everyone starts in>

# --- Model --------------------------------------------------------------
OPENAI_BASE_URL=https://your-endpoint/v1
OPENAI_API_KEY=<key>
OPENAI_MODEL=<model>

# --- State (back these up) ---------------------------------------------
USERS_DB=C:\ProgramData\QlikAI\users.db
HISTORY_DIR=C:\ProgramData\QlikAI\history

# --- Access -------------------------------------------------------------
AUTH_ENABLED=true
COOKIE_SECURE=true                       # once TLS is in front, see step 5
PASSWORD_MIN=8                           # local accounts only, see below
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<choose one, or leave blank and read it from the log once>

# --- Scale --------------------------------------------------------------
WORKER_THREADS=128                       # see "Sizing" below
LLM_MAX_CONNECTIONS=200
MAX_USER_SESSIONS=200

# --- Logging ------------------------------------------------------------
LOG_FILE=C:\ProgramData\QlikAI\logs\qlik-ai.log
```

`PASSWORD_MIN` applies **only to accounts created inside this app**. An
Active Directory password is never length-checked here — it goes straight to
the domain controller, which enforces the domain's own policy. So if sign-in
is refusing a password as too short, the account is a local one, and lowering
this setting is the fix. Turning `AUTH_ENABLED` off is not: it puts every
user into one shared conversation and one shared Qlik identity, and the
server then refuses to serve anything but `127.0.0.1`.

`.env` holds the API key and possibly the admin password. Lock it down the
same way as the certificates, and keep it out of the checkout — it is
gitignored, and it is worth confirming with `git ls-files .env` that it has
stayed that way.

---

## 4. Check before installing the service

Run it in the foreground once. Everything that is going to be wrong is wrong
here, where you can see it.

```powershell
.\.venv\Scripts\python.exe check_connection.py
.\.venv\Scripts\python.exe web_app.py
```

`check_connection.py` tests four things separately — Qlik, the model
endpoint, Active Directory and the accounts database — so you find out
*which* one is broken rather than watching the whole thing fail at once.

It asks the model for one tool call rather than just pinging the endpoint,
because "the endpoint answers" and "this model can drive the assistant" are
different questions and only the second one matters: the assistant is a
tool-calling loop, and a model that ignores tools talks fluently and builds
nothing.

On Enterprise it also warns about accounts with no Qlik identity — the
invisible queue described in step 8.

You should see:

```
  Qlik AI      -> http://127.0.0.1:8000
  Admin        -> http://127.0.0.1:8000/admin
  MCP endpoint -> http://127.0.0.1:8000/mcp
  app '<name>' (enterprise), model <model>
  sign-in required
  log          -> C:\ProgramData\QlikAI\logs\qlik-ai.log
```

If `ADMIN_PASSWORD` was blank, this run prints a generated one **once**.
Write it down.

Ctrl+C to stop.

---

## 5. TLS — and why this one is not optional

### What it is

Qlik AI listens on `127.0.0.1:8000` and speaks plain HTTP. That is
deliberate: holding a certificate is not an application server's job. You put
**IIS in front** on 443, it terminates TLS, and forwards to the app.

```
  browser ──── HTTPS 443 ────▶ IIS ──── HTTP ────▶ Qlik AI
                                                   127.0.0.1:8000
```

"TLS in front" means exactly that: something else holds the certificate and
does the encryption, and the app never sees it.

### Why you cannot skip it

Without TLS, everything typed into the login form crosses the network **in
clear text**. With Active Directory sign-in, that is not this app's password:

> **It is the user's Windows domain password.** Every sign-in puts a bank
> network credential on the wire, readable by anyone who can see the traffic
> — a switch tap, a compromised machine on the same segment, anyone with the
> right access on the network team.

Three smaller reasons on top: `COOKIE_SECURE=true` needs it (the session
cookie is a login in its own right and travels on every request); browsers
mark the page "Not secure", which is exactly the wrong signal for something
showing bank figures; and no bank's security review will pass without it.

### What you need

| | |
|---|---|
| **A certificate** issued to the name people will type | Ask whoever runs your internal CA — most banks run AD Certificate Services and this is a routine request |
| **IIS** with **URL Rewrite** and **ARR** | `Install-WindowsFeature Web-Server`, then two MSI downloads |
| **A DNS name** pointing at this machine | e.g. `qlikai.bank.internal` |

URL Rewrite and ARR are separate downloads from Microsoft — IIS cannot
reverse-proxy without them:

- [URL Rewrite 2.1](https://www.iis.net/downloads/microsoft/url-rewrite)
- [Application Request Routing 3.0](https://www.iis.net/downloads/microsoft/application-request-routing)

Install URL Rewrite first; ARR depends on it.

### Doing it

Import the certificate into `LocalMachine\My` (double-click the `.pfx`, choose
Local Machine), find its thumbprint, and run the script:

```powershell
Get-ChildItem Cert:\LocalMachine\My | Format-List Subject, Thumbprint

cd C:\apps\qlikai\deploy
.\setup-iis.ps1 -HostName qlikai.bank.internal `
                -CertificateThumbprint A1B2C3D4E5F6...
```

That creates the site, binds the certificate, enables the proxy, writes the
forwarding rule, redirects HTTP to HTTPS, and opens the firewall.

**Then set `COOKIE_SECURE=true` in `.env` and restart the service** — and not
before. A Secure cookie is never sent over plain HTTP, so setting it early
makes every login appear to succeed and every following request arrive signed
out, which reads as a broken login rather than a misconfiguration.

Check it:

```powershell
Invoke-WebRequest https://qlikai.bank.internal/healthz -UseBasicParsing
```

### Testing without a real certificate

```powershell
.\setup-iis.ps1 -HostName qlikai.bank.internal -SelfSigned
```

Fine for proving the plumbing works. **Not for production** — every browser
warns, and training people to click through a certificate warning is worse
than having no padlock at all: it teaches them to ignore the one thing that
would tell them they are being intercepted.

### Two things the script handles that are easy to miss

> **Response buffering must be off.** Chat replies stream as server-sent
> events. ARR buffers responses by default, so the whole answer would arrive
> at once after a minute of apparently nothing happening — indistinguishable
> from the model having hung. The script sets `responseBufferThreshold` to 0.

> **The app must stay on `127.0.0.1`.** If it is also listening on `0.0.0.0`,
> people can reach it directly on port 8000 and skip the TLS entirely, which
> makes the certificate decorative. `install-service.ps1` binds to loopback by
> default; leave it there.

### If IIS is not an option

Any reverse proxy works — nginx, HAProxy, or the bank's load balancer. Three
requirements, whichever you use:

- Forward to `127.0.0.1:8000`
- **Disable response buffering** (`proxy_buffering off;` in nginx)
- Pass `X-Forwarded-For`, which the audit trail records as the caller

There are no websockets between browser and app — only server-sent events —
so no websocket support is needed. (The websocket in this system is
server-to-Qlik and does not pass through the proxy.)

---

## 6. Install the service

```powershell
cd C:\apps\qlikai\deploy
.\install-service.ps1 -ListenHost 127.0.0.1 -Port 8000 `
                      -Account "DOMAIN\svc_qlikai" -Password (Read-Host -AsSecureString)
nssm start QlikAI
```

Needs [NSSM](https://nssm.cc/download) — put `nssm.exe` beside the script or
on PATH. The script explains the alternatives if your organisation will not
allow it.

Bind to `127.0.0.1` and let the proxy reach it there. Binding to `0.0.0.0`
puts the app on the network directly.

Watch it come up:

```powershell
Get-Content C:\ProgramData\QlikAI\logs\qlik-ai.log -Wait -Tail 40
curl http://127.0.0.1:8000/healthz
```

---

## 7. Active Directory sign-in

Your users already type their Windows username and password to reach Qlik
Sense. Point this at the same directory and they type the same thing here.

```ini
LDAP_ENABLED=true
LDAP_SERVER=dc01.bank.internal
LDAP_USE_SSL=true                 # LDAPS on 636
LDAP_PORT=636
LDAP_BASE_DN=DC=bank,DC=internal
LDAP_UPN_SUFFIX=bank.internal     # jsmith -> jsmith@bank.internal
LDAP_WINDOWS_DOMAIN=BANK
LDAP_QLIK_DIRECTORY=BANK          # what the QMC calls this user directory
# LDAP_ADMIN_GROUP=CN=Qlik AI Admins,OU=Groups,DC=bank,DC=internal
```

**This is a bind, not a directory read.** The password is proven by asking a
domain controller to accept it, and it is never stored here — not hashed,
not cached.

What it buys, beyond removing a second password:

- **Accounts create themselves on first sign-in**, with the Qlik identity
  already filled in *from AD*. `sAMAccountName` is exactly what Qlik's own AD
  connector uses as the user id, so both systems name the same person the
  same way — and step 8 below stops being a job.
- **Leavers are handled in AD.** Disable there, and the bind fails here.
- **Admins from a group**, if you set `LDAP_ADMIN_GROUP` — no separate role
  list to maintain.

Three things that are holes rather than preferences:

> **TLS is not optional.** A simple bind sends the password in the clear.
> LDAPS on 636 or StartTLS on 389. Running without either works and logs a
> warning every time, because somebody will turn it off to get past a
> certificate problem and then forget.

> **`LDAP_QLIK_DIRECTORY` is the name of the QMC's connector**, not a fact
> about AD. Usually the NetBIOS domain — but if the QMC calls it something
> else, engine sessions open as a user Qlik has never heard of, and it
> surfaces as a permissions error rather than a configuration one.

> **The lockout protects their Windows account.** Failed sign-ins are counted
> *here* and the account locks *here*, before anything reaches AD. Without
> that ordering, somebody hammering this login form would lock the person's
> domain account across the whole bank.

**Keep at least one local administrator.** Local accounts work alongside AD
and are the way in when a domain controller is unreachable:

```powershell
.\.venv\Scripts\python.exe manage_users.py create breakglass --admin
```

`manage_users.py list` shows a **SIGN-IN** column — `AD` or `local` — which
is the first thing to check when somebody cannot get in.

### Checking it before go-live

Sign in as a real user and confirm three things: they get in with their
Windows password, `manage_users.py list` shows them as `AD` with the right
Qlik identity, and the app opens a Qlik session as them rather than as the
service account.

---

## 8. Create the users

**With `LDAP_ENABLED=true` this step largely disappears** — accounts create
themselves correctly on first sign-in. What follows is for local accounts,
and for checking the result.

Every account carries a **Qlik identity** — the user directory and user id it
connects to Qlik Sense as. Set it from the admin page, or from the command
line:

```powershell
.\.venv\Scripts\python.exe manage_users.py create jsmith --name "J Smith" `
      --qlik-directory BANK --qlik-user jsmith
```

> **Fill in the Qlik identity for every account before go-live.**
>
> A user *with* one gets their own engine session and their own lock — they
> never wait on anybody. A user *without* one falls back to the service
> account's shared connection **and the global lock**, so all such users
> serialise against each other. It is a queue that does not show up anywhere
> until people complain the assistant is slow.
>
> `manage_users.py list` shows who is missing one.

Each impersonated user needs access to the app in the QMC, and consumes an
access pass.

---

## Option B: Docker

Everything above applies — certificates, config, TLS, sizing — with these
differences.

```powershell
# certs go beside the compose file rather than in C:\ProgramData
mkdir certs                       # client.pem, client_key.pem, root.pem
Copy-Item .env.example .env       # then edit as in step 3

docker compose up -d
docker compose logs -f            # the generated admin password is in here, once
```

| | Service | Docker |
|---|---|---|
| Config | `.env` | the same `.env`, via `env_file` |
| Paths | `C:\ProgramData\QlikAI` | volumes `qlikai-data`, `qlikai-logs` |
| Certificates | `QLIK_CERT_DIR` | `./certs` mounted read-only at `/certs` |
| Backup | copy the folder | `docker run --rm -v qlikai-data:/d -v ${PWD}:/b alpine tar czf /b/qlikai-backup.tar.gz -C /d .` |
| Restart | `nssm restart QlikAI` | `docker compose restart` |
| The CLI | `python manage_users.py …` | `docker compose exec qlikai python manage_users.py …` |

Three things that bite in a container and not in a service:

> **DNS.** The engine certificate is issued to the Qlik server's *hostname*,
> so the container has to resolve that name — connecting by IP fails TLS
> verification. On WSL2 this usually inherits the host's resolvers and simply
> works; when it does not, it is the first thing to check. `dns:` and
> `extra_hosts:` are in the compose file, commented, for when it does not.

> **The port is published to `127.0.0.1` only**, so only a reverse proxy on
> the same machine can reach it. Changing that to `8000:8000` puts the app on
> the network directly, with whatever TLS you have — which by default is none.

> **One container, not several.** `docker compose up --scale qlikai=3` would
> give each replica its own session registry and its own Qlik connections,
> and users would land in different conversations depending on which replica
> took the request. Same reason the service runs one process.

The image runs as a non-root user (uid 10001) and carries no `.env`, no
certificates and no database — `.dockerignore` keeps all three out, because a
secret baked into a layer travels wherever the image goes and `docker
history` will show it.

---

## Sizing

The setting that decides how many people can use it at once is
`WORKER_THREADS`. Every request occupies one thread for its whole duration,
and a chat turn is model latency plus several tool calls.

| Situation | `WORKER_THREADS` | `LLM_MAX_CONNECTIONS` |
|---|---|---|
| 300 accounts, ~30 asking at once | 64 | 100 |
| 300 accounts, ~100 asking at once | 192 | 200 |
| 300 genuinely simultaneous | 400 | 400 |

Threads are pooled and created on demand, so a high ceiling costs nothing
until it is used. The work is I/O-bound — a websocket to Qlik and an HTTPS
call to the model — so the threads are asleep almost all the time.

> **Run one process.** The session registry and the shared Qlik connection
> are in-process. `uvicorn --workers N` would give each worker its own
> registry, and users would land in different conversations at random
> depending on which worker took the request. Scale up, not out.

The other ceilings, in the order you will meet them:

| Limit | Where | What to do |
|---|---|---|
| Request threads | `WORKER_THREADS` | Raise it (table above) |
| Model connections | `LLM_MAX_CONNECTIONS` | Keep ≥ `WORKER_THREADS` |
| Live sessions | `MAX_USER_SESSIONS` | Soft cap; raise for many users |
| Qlik access passes | Your licence | Commercial, not technical |

---

## Backup

Two things, and only two:

```
C:\ProgramData\QlikAI\users.db     accounts, sessions, the audit trail
C:\ProgramData\QlikAI\history\     every saved conversation
```

Everything else is recreated from the checkout. Both hold customer figures,
so both have a retention setting — kept forever unless you say otherwise:

```ini
AUDIT_RETENTION_DAYS=0      # 0 keeps everything
HISTORY_RETENTION_DAYS=0
```

Enforced at startup, and on demand. Try it before you mean it:

```powershell
.\.venv\Scripts\python.exe manage_users.py prune --audit-days 365 --dry-run
.\.venv\Scripts\python.exe manage_users.py prune
```

The pruning is itself recorded in the trail it trims, so a gap is explained
rather than looking like somebody tampered with it.

SQLite runs in WAL mode, so copy `users.db`, `users.db-wal` and
`users.db-shm` together, or stop the service first.

---

## Recovering

### When Qlik goes down

**You do not restart the server.** That was true once and is not any more.

| What happens | What the server does |
|---|---|
| A connection drops — engine restart, firewall idle-timeout, lost VPN | Rebuilds it on that person's next request, on the app they were using. They usually never notice. |
| Qlik is down when a request arrives | 503 with the reason, and a **Reconnect** button on the page. Signing in, saved conversations, the admin pages and the audit trail keep working. |
| Qlik is down when the **server starts** | It starts anyway and says so. It connects on the first request that succeeds. |
| Qlik comes back | Anyone's next request reconnects them. To do everyone at once: **Admin → Signed in → Reconnect everyone to Qlik**. Nobody is signed out, no conversation is lost. |

Retries are limited to one attempt per session per `RECONNECT_COOLDOWN_SECONDS`
(15 by default), so an unreachable Qlik cannot make this server unreachable
too — three hundred users would otherwise mean three hundred connection
attempts a round, each holding a worker thread for the connect timeout.
Pressing **Reconnect** skips the limit: a person can see that Qlik is back
and the code cannot.

To see what is actually reachable:

```
GET /api/health         what the person asking can reach
GET /api/admin/health   every session's connection, administrators only
GET /healthz            flat 200 - this is the service manager's probe and
                        stays up deliberately, or the service would be
                        restarted for somebody else's outage
```

**Locked out of the only administrator account.** Five failed logins lock it
for fifteen minutes, and the admin page is behind the login that is refusing
you:

```powershell
.\.venv\Scripts\python.exe manage_users.py unlock admin
.\.venv\Scripts\python.exe manage_users.py password admin
```

Works with the service running or stopped — it talks to the database, not
the server.

**Lost the admin password entirely** and there is no other administrator:

```powershell
.\.venv\Scripts\python.exe manage_users.py create rescue --admin
```

**No accounts at all.** Deleting `users.db` makes the next start create a
fresh administrator. It also deletes every account and the audit trail;
conversations survive, because they are files.

---

## What is not ready

Stated plainly, because finding these in UAT is worse than reading them here.

- **The MCP endpoint is administrators only.** Its tool handlers run in tasks
  started from the server's lifespan and cannot see which user
  authenticated, so every call lands in the shared session. The browser
  interface has no such limit.
- **Enterprise has not been run against a live server from this repo.** The
  code path is Qlik's documented direct-Engine-API pattern and is covered by
  tests against a fake, but first contact with a real QMC will find
  something. Budget for it.
- **Active Directory sign-in has not been run against a real domain.** It is
  covered by tests against ldap3's mock server - which is a real LDAP
  implementation, so binds and filters are genuinely exercised - but the
  machine it was written on is in a workgroup. Expect first contact with a
  real DC to find something: a base DN that does not cover everybody, a
  certificate the server does not trust, or a QMC user directory named
  differently from the domain.
- **One process, one host.** No clustering, no failover. Restarting drops
  every open Qlik session; users reconnect on their next request, but a
  chat turn in flight is lost.
- **The assistant can still get arithmetic wrong.** `readiness.md` R1. A
  full-precision hosted model reduces it; nothing in this codebase checks a
  figure in the reply against what the tools returned.

---

## Quick reference

```powershell
nssm start QlikAI  /  nssm stop QlikAI  /  nssm restart QlikAI
Get-Content C:\ProgramData\QlikAI\logs\qlik-ai.log -Wait -Tail 40

.\.venv\Scripts\python.exe manage_users.py list
.\.venv\Scripts\python.exe manage_users.py unlock <user>
.\.venv\Scripts\python.exe manage_users.py password <user>
.\.venv\Scripts\python.exe manage_users.py sessions
.\.venv\Scripts\python.exe manage_users.py audit --action login.failed
.\.venv\Scripts\python.exe manage_users.py prune --dry-run
.\.venv\Scripts\python.exe check_connection.py
.\.venv\Scripts\python.exe check_connection.py --ldap-user jsmith
```
