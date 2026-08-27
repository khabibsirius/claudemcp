# Qlik Assistant — Pre-Production Readiness

Prepared 21 Aug 2026, revised 24 Aug · 844 tests passing
**Revised 25 Aug: B1 and B2 are closed** — see *Since this was written*
below. 1,266 tests passing.
Findings verified against the live app; unverified judgement is marked as such.

---

## Verdict

**The assistant works; the surface around it does not yet.**

Answer quality is now genuinely good: it routes vague requests correctly,
fetches real figures, and refuses to build charts that would render blank.
What is not ready is everything a bank requires around that — there is no
authentication, all users share one conversation, and it runs on Qlik Sense
**Desktop**. None of those are model problems, and none are fixed by
prompting.

---

## Since this was written

The open question in *Suggested order* step 2 — single-operator or
multi-user — was answered: **multi-user**, with each person holding their own
Qlik Sense Enterprise account.

| Was | Now |
|---|---|
| **B1** No authentication anywhere; `/mcp` mounted with no check | Closed. Everyone signs in; the gate is middleware so the `/mcp` mount is covered; deny by default; server refuses a non-loopback bind with auth off |
| **B2** One `_state`: every user in the same conversation | Closed. One `Session` per person — own conversation, history, settings, and on Enterprise their own engine socket opened as them |
| **B3** Runs against Desktop | Unchanged, and now explicit rather than assumed. Desktop shares one connection because it can do nothing else; Enterprise impersonates per user. The same accounts work on both |
| **O2** No audit trail of who asked what or changed the script | Partly closed. Logins, failed logins, script rewrites, reloads, account changes and admin reads of a conversation are recorded. Retention policy still an open question |

New: `users.py` (accounts, SQLite), `auth.py` (the gate), `web/login.html`,
`web/admin.html`. `session.py` and `history.py` became per person.

**Still open, and worth naming:** the MCP endpoint is limited to
administrators, because its tool handlers run in tasks started from the
server's lifespan rather than from the request and so cannot see who
authenticated. Every MCP tool call lands in the shared system session. That
is stated in the README rather than papered over, and closing it means the
tools taking a session explicitly.

R1–R3 and O1/O4 are untouched by this work.

### Losing a dependency no longer costs everybody the product

Not on the original list, and it should have been: a dropped connection put
the server into a state it never left. Six failures were found by probing
rather than reading, and all six are closed.

| Failure | Was | Now |
|---|---|---|
| Websocket dies mid-call | 2 of the 3 ways it can die left the engine reporting itself **connected**, so nothing ever rebuilt it | Every path discards the socket; measured 3 of 3 |
| A session's connection goes away | "No app is open. Open one first.", forever, on every request until a restart | Rebuilt on the next request, on the app that person was using |
| Qlik unreachable at startup | Process exits — under a service manager, a restart loop that takes the login page, the admin pages, saved conversations and the audit trail with it | Starts anyway and says so; connects on the first request that works |
| Qlik down, many users | Every request from every user attempts its own connection, each holding a worker thread for the connect timeout | One attempt per session per `RECONNECT_COOLDOWN_SECONDS`; measured 1 attempt per 100 requests |
| Model endpoint blackholed | 600s connect budget per turn, holding a worker thread and the session lock — 128 threads was 21 thread-hours | Connect 10s, generation still 600s |
| Idle sessions reaped | Websockets closed while holding the registry lock, which every request takes — measured 0.95s stalls on requests needing only a dictionary lookup | Chosen under the lock, closed outside it; median 0.00s |

Two things a person can press, so nobody phones an administrator: **Reconnect**
on the page when their own connection is gone, and **Reconnect everyone to
Qlik** on the admin page when Qlik has come back — neither signs anybody out
or discards a conversation. `/api/health` and `/api/admin/health` report what
is actually reachable; `/healthz` stays a flat 200 on purpose, because a
probe that failed during a Qlik outage would have the service restarted for
somebody else's problem.

Covered by `tests/test_resilience.py` (31 tests) and verified end-to-end
against a real server over a real socket.

**Not closed:** a full disk or a locked database file still fails the request
that hits it. Reconnecting does not fix either, so nothing pretends to.

---

## What shipped this week

| Change | Why it mattered | Evidence |
|---|---|---|
| Empty charts refused | `create_chart` validated nothing and returned `{"created": …}` regardless, so the model reported success for charts that render blank | +7 tests |
| Rewritten system prompt | Intent routing, a completeness floor, per-chart reporting with figures, banker-facing language | 1.9k → 8.6k tok |
| Empty sheets made visible | `list_charts` only returned sheets holding charts, so "you have 3 sheets" was wrong | 3 → 4 sheets |
| Context budget guard | The reserve was sized for a prompt a quarter of today's; overflow silently deletes the system prompt mid-chat | reserve 18k |
| Model swapped | Off the Q2_K abliterated merge onto the official Q4_K_M build | Q2_K → Q4_K_M |
| Realistic test data | 1,269 rows over 6 months hid every scale problem | 1,777,919 rows |
| Capability answers grounded | Asked what it could do, it invented four dashboard "types", drill-down groups and status indicators — none exist. Now routed to the real catalogue with an explicit list of what it cannot do | 23 of 23 |

---

## Blockers

Each of these makes the system unfit to put in front of a banker, regardless
of how good the answers are.

### B1 — The web app and MCP endpoint have no authentication — **CLOSED 25 Aug**

There is no login, no token, no session identity anywhere in `web_app.py`.
Anyone who can reach the port can read the bank's figures, rewrite the load
script, and trigger a reload that replaces every row. The `/mcp` endpoint is
mounted with the same access and no separate check.

It binds to `127.0.0.1` by default, which is the only thing containing it
today. The first time someone runs it with `--host 0.0.0.0` so a colleague
can try it, that containment is gone and nothing warns them.

**Before pre-prod:** authentication in front of both the UI and `/mcp`, and
refuse to start on a non-loopback host unless auth is configured.

### B2 — Every user shares one conversation and one Qlik session — **CLOSED 25 Aug**

`session.py` holds a single module-level `_state`: one engine, one message
list, one `chat_id`. Two people using the assistant at once are in the *same*
conversation — each sees the other's questions, and the second person's
request lands mid-way through the first person's context.

This is deliberate and correct for what it was built as: a single-operator
tool where the web UI and the MCP client deliberately share one open app. It
just does not survive a second concurrent user.

**Decision needed:** either scope state per session, or state plainly that
this is a single-operator tool and license it that way. The second is a
legitimate answer — but it must be a decision, not a discovery.

### B3 — It runs against Qlik Sense Desktop

`QLIK_MODE=desktop`, port 4848. Desktop is a single-user workstation product:
it permits one session per app, which is why the `hi` app could not be
written to while it was open in the Qlik window. Enterprise support exists in
`config.py` — certificates, user directory, `QLIK_SSL_VERIFY` — but has not
been exercised.

**Before pre-prod:** stand up against Enterprise with certificate auth and
run the full flow there. Expect the per-user identity question to surface
immediately; it interacts with B1.

### B4 — `.env` is tracked by git, and now holds a live API key

`.gitignore` lists `.env`, but gitignore does not untrack a file that was
already committed — and this one was, in the first commit.

Verified:

    git ls-files .env          -> .env            (tracked)
    git show HEAD:.env         -> APP_NAME, OLLAMA_MODEL,
                                  QLIK_HOST, QLIK_PORT
    git log -p --all -- .env   -> 0 occurrences of the key

The committed version holds only harmless settings, so **nothing has
leaked**. The risk is latent: the working copy now contains a live
`OPENROUTER_API_KEY`, and the next `git commit -a` publishes it.

**One command:** `git rm --cached .env` — untracks it, leaves the file on
disk. Do this before the next commit, not after.

---

## Correctness risks

Specific to putting a language model in front of financial figures. They do
not crash anything, which is what makes them dangerous.

### R1 — The model does its own arithmetic, and sometimes gets it wrong

Observed in a live run: *"the top 3 regions hold roughly 79%"* when its own
returned figures give 64%. Earlier runs computed the same figure correctly,
so it is intermittent — worse than consistent, because it passes review.

A banker has no way to tell a computed percentage from a fabricated one, and
the surrounding text is accurate and confident either way.

**Recommended:** compute shares, totals and period-on-period change inside
`query` and hand the model finished percentages. Never let it derive a number
it could instead be given.

### R2 — Coverage varies run to run on the same question

The same request produced 6, 7 and 8 tool calls across three runs. One run
queried the high-value flag and found it captures 99.99% of the book — a
genuinely useful finding. The next run skipped it, and said nothing about
having skipped it.

In the last measured run, 11 of 28 chart rows carried figures; the other 17
described the chart's purpose instead.

**Recommended:** the prompt already requires an explicit "not queried"
marker. It is being applied twice where it should apply seventeen times —
tighten it, or enforce coverage in code rather than by instruction.

### R3 — Nothing checks the answer against the tool results

Every guarantee about the reply is a sentence in the system prompt. There is
no code path comparing the figures in the final answer to what the tools
returned, so R1 and R2 are invisible until a person notices.

The same gap covers claims the assistant makes about *itself*. Asked what it
could build, it described dashboard templates, drill-down hierarchies and
red/green status indicators — none of which exist anywhere in the code. That
specific case is now fixed, but the fix is another prompt rule, with the same
fragility as the rest.

**Worth discussing:** a cheap post-check — every number in the reply must
appear in a tool result — would catch fabricated figures without a second
model call. Chart-type names could be checked the same way, against the
catalogue.

---

## Operational gaps

Not blockers on their own, but each becomes a support ticket nobody can
answer.

### O1 — Four minutes per question, single-threaded

An inventory question runs ~250 s: 6–8 tool calls plus a long reply. The
22 GB model already occupies a 12 GB GPU and spills into system RAM, and
there is no queueing — a second request waits on the first through the
shared lock.

### O2 — Conversations are written to disk in the clear

Full transcripts, including figures pulled from the app, are saved as JSON
under `~/.qlik-ai/history`, capped at 200 files. For a bank this needs a
retention policy and a decision on whether transcripts count as customer
data. There is also no audit trail of who asked what or who changed the load
script.

### O4 — The system prompt now takes a third of the context window

Every behaviour fixed this week was fixed by adding rules, and the prompt
grew with each one. It is now **8,572 tokens**; with the tool schemas that is
**10,698 of 32,768 — 33%** spent before the user says anything.

`CHAT_HISTORY_RESERVE`, raised three times in four days:

    Fri   6,000  -> 10,000    prompt outgrew the original reserve
    Fri  10,000  -> 13,000    room for a full inventory briefing
    Mon  13,000  -> 17,000    per-chart reporting
    Mon  17,000  -> 18,000    capability intent
                              history now ~14,700 tok (~20 turns)

Nothing is broken — a guard test recomputes the real prompt size and fails
the build before it can overflow, which is how each of these was caught
rather than discovered in production. But the trend is one-directional, and
each raise buys prompt room by taking conversation memory.

**Not filed as a blocker:** it breaks nothing today, and calling it one would
dilute B1–B4. It becomes a blocker the moment either the window shrinks or a
session needs more than 20 turns. The next increase should be a decision to
*trim* the prompt — the note is already in `config.py`.

### O3 — Dead configuration, and repo dust

`LLM_PROVIDER`, `OPENROUTER_API_KEY` and `OPENROUTER_MODEL` are set in `.env`
and read by nothing — grep finds no reference anywhere in the project.
Setting `LLM_PROVIDER=openrouter` today silently keeps using the local model.
Six `__pycache__/*.pyc` files and an empty file named `python` are also
tracked.

**Either or:** implement the provider path (~40 lines; OpenRouter speaks
OpenAI chat-completions, not Ollama's format) or delete the keys. A hosted
model at full precision would also retire R1.

---

## Suggested order

Sequenced because the dependencies are real, not to rank importance.

1. **Untrack `.env`.** One command, thirty seconds, and it stops being a risk
   permanently. Do it before any other commit.
2. **Decide single-operator or multi-user.** B2 and B1 both hang off this
   answer, and so does the Enterprise work in B3. Nothing else should start
   until it is settled.
3. **Load the 1.78M-row dataset and re-run the benchmark.** The 17-phrase
   list catches routing and shrinkage; at this scale the arithmetic in R1 is
   far easier to spot, because the numbers stop being round.
4. **Move share and change calculations into `query`.** Removes the largest
   single source of wrong numbers, and shortens replies at the same time.
5. **Stand up Enterprise with certificates.** Longest lead time of anything
   here, so it should start today even if it finishes later.
6. **Agree a prompt budget and stop raising the reserve.** O4 is at 33% of
   the window after three raises in four days. Pick a ceiling now, while
   trimming is a choice rather than an emergency.

---

## Open questions for the room

- How many people use this at once on day one? The honest answer may be
  "one", and that makes B1 and B2 much cheaper.
- Is a four-minute answer acceptable to a banker, or does that alone force a
  hosted model?
- Do chat transcripts containing customer figures fall under the bank's
  data-retention rules? This decides whether O2 is paperwork or an
  architecture change.
- Who owns the Qlik Enterprise certificates, and how long does issuing them
  take?
