import argparse
import datetime
import io
import platform
import subprocess
import sys
import traceback

REPORT = "doctor-report.txt"

SECRET_HINTS = ("KEY", "PASSWORD", "SECRET", "TOKEN")


class Report:
    def __init__(self):
        self.lines = []
        self.problems = []

    def rule(self, title):
        self.lines.append("")
        self.lines.append("=" * 76)
        self.lines.append(title)
        self.lines.append("=" * 76)

    def say(self, text=""):
        self.lines.append(text)

    def ok(self, text):
        self.lines.append(f"  ok    {text}")

    def note(self, text):
        self.lines.append(f"  note  {text}")

    def bad(self, text):
        self.lines.append(f"  FAIL  {text}")
        self.problems.append(text)

    def blew_up(self, what, error):
        self.lines.append(f"  FAIL  {what}: {error.__class__.__name__}: {error}")
        for line in traceback.format_exception_only(type(error), error):
            self.lines.append(f"        {line.rstrip()}")
        self.problems.append(f"{what}: {error}")

    def text(self):
        return "\n".join(self.lines)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Collect everything needed to diagnose this deployment "
                    "into one report. Reads only; changes nothing."
    )
    parser.add_argument("--out", default=REPORT,
                        help=f"where to write the report (default {REPORT})")
    parser.add_argument("--skip-model", action="store_true",
                        help="do not contact the model endpoint")
    parser.add_argument("--skip-qlik", action="store_true",
                        help="do not open any Qlik connection")
    return parser.parse_args(argv)


def which_code(report):
    report.rule("1. WHICH CODE IS THIS")
    report.say(f"  when            {datetime.datetime.now().isoformat(timespec='seconds')}")
    report.say(f"  python          {sys.version.split()[0]} on {platform.system()} "
               f"{platform.release()}")
    report.say(f"  running from    {sys.path[0] or '.'}")

    for label, command in (("commit", ["git", "rev-parse", "--short", "HEAD"]),
                           ("branch", ["git", "rev-parse", "--abbrev-ref", "HEAD"]),
                           ("subject", ["git", "log", "-1", "--format=%s"])):
        try:
            value = subprocess.run(command, capture_output=True, text=True,
                                   timeout=10).stdout.strip()
            report.say(f"  {label:<15} {value or '(unknown)'}")
        except Exception as e:
            report.say(f"  {label:<15} (could not read: {e})")

    try:
        dirty = subprocess.run(["git", "status", "--short"], capture_output=True,
                               text=True, timeout=10).stdout.strip()
        report.say(f"  local edits     {'none' if not dirty else dirty.count(chr(10)) + 1}")
        if dirty:
            for line in dirty.splitlines()[:12]:
                report.say(f"                  {line}")
    except Exception:
        pass

    report.say()
    report.say("  features present in this copy:")
    checks = [
        ("identity guard", "session", "QlikIdentityMissing"),
        ("landing-app fallback", "session", None, "Session", "open_default"),
        ("startup checks", "preflight", "findings"),
        ("repository client", "qrs", "hub_apps"),
        ("chart overrides", "chart_specs", "prefer_available"),
    ]
    for entry in checks:
        label, module_name, attribute = entry[0], entry[1], entry[2]
        try:
            module = __import__(module_name)
            if attribute is None:
                holder = getattr(module, entry[3])
                present = hasattr(holder, entry[4])
            else:
                present = hasattr(module, attribute)
            verdict = "yes" if present else "NO - this copy predates it"
            report.say(f"    {label:<24} {verdict}")
        except Exception as e:
            report.say(f"    {label:<24} could not check ({e})")


def configuration(report):
    report.rule("2. CONFIGURATION")
    try:
        import config
        report.say(config.summary())
    except Exception as e:
        report.blew_up("reading the configuration", e)
        return

    report.say()
    for name in ("QLIK_MODE", "QLIK_PORT", "AUTH_ENABLED", "PASSWORD_MIN",
                 "ALLOW_SHARED_QLIK_IDENTITY", "COOKIE_SECURE", "QRS_ENABLED",
                 "QRS_AS_USER", "LDAP_ENABLED", "APP_NAME",
                 "QLIK_USER_DIRECTORY", "QLIK_USER_ID"):
        value = getattr(config, name, "(not in this build)")
        report.say(f"  {name:<28} {value!r}")

    report.say()
    report.say("  (nothing above is a secret; keys and passwords are never "
               "printed)")

    try:
        import preflight
        found = preflight.findings()
        report.say()
        report.say("  startup checks:")
        report.say(preflight.render(found, indent="  ") or "    (nothing)")
        for level, body in found:
            if level != preflight.OK:
                report.problems.append(body[0])
    except Exception as e:
        report.blew_up("running the startup checks", e)


def accounts(report):
    report.rule("3. ACCOUNTS")
    try:
        import users
        rows = list(users.listing())
    except Exception as e:
        report.blew_up("reading the account database", e)
        return []

    if not rows:
        report.note("No accounts yet. The server creates the first "
                    "administrator on startup.")
        return []

    report.say(f"  {'username':<18} {'role':<7} {'source':<9} "
               f"{'qlik identity':<26} active")
    for row in rows:
        identity = (f"{row['qlik_directory']}\\{row['qlik_user_id']}"
                    if row["qlik_user_id"] else "(BLANK)")
        report.say(f"  {row['username']:<18} {row['role']:<7} "
                   f"{row['auth_source']:<9} {identity:<26} "
                   f"{'yes' if row['active'] else 'no'}")
    return rows


def duplicates(report, rows):
    """Two accounts on one Qlik identity see one hub. Nothing else checks this."""
    seen = {}
    for row in rows:
        if not (row["qlik_directory"] and row["qlik_user_id"]):
            continue
        key = (row["qlik_directory"].strip().lower(),
               row["qlik_user_id"].strip().lower())
        seen.setdefault(key, []).append(row["username"])

    shared = {key: names for key, names in seen.items() if len(names) > 1}
    for (directory, user_id), names in sorted(shared.items()):
        report.bad(
            f"{', '.join(sorted(names))} all connect as {directory}\\{user_id}. "
            f"They are one Qlik user and will see one identical hub."
        )
    return shared


def who_the_engine_thinks(engine):
    """(ok, reported) without letting a failed GetAuthenticatedUser look like
    a failed connection. ok is None whenever we genuinely cannot tell."""
    try:
        return engine.identity_matches()
    except Exception as e:
        return None, f"(the engine would not answer: {e.__class__.__name__}: {e})"


def qlik(report, rows, skip):
    report.rule("4. QLIK")
    if skip:
        report.note("skipped (--skip-qlik)")
        return

    try:
        import config
        import session
        from qlik_engine import QlikEngine, QlikEngineError
    except Exception as e:
        report.blew_up("importing the Qlik client", e)
        return

    report.say(f"  per-user connections  {session.per_user_engines()}")
    report.say(f"  shared identity allowed  {config.ALLOW_SHARED_QLIK_IDENTITY}")
    if config.ALLOW_SHARED_QLIK_IDENTITY:
        report.bad(
            "ALLOW_SHARED_QLIK_IDENTITY is true, so any account without its "
            "own Qlik identity silently connects as the service account "
            "instead of being refused."
        )
    report.say()

    service = f"{config.QLIK_USER_DIRECTORY}\\{config.QLIK_USER_ID}"

    # These two need no Qlik connection, so run them before anything can
    # fail and cut the section short.
    duplicates(report, rows)
    for row in rows:
        if (row["qlik_directory"].strip().lower(),
                row["qlik_user_id"].strip().lower()) == (
                    (config.QLIK_USER_DIRECTORY or "").strip().lower(),
                    (config.QLIK_USER_ID or "").strip().lower()):
            report.bad(
                f"the account {row['username']!r} connects as {service}, the "
                f"same identity this server uses for its own service "
                f"connections. That person sees everything the service "
                f"account can reach. Give them their own Qlik user."
            )

    report.say(f"  As the service account {service}:")
    service_apps = []
    try:
        with QlikEngine() as engine:
            report.ok(f"connected ({engine.mode})")
            ok, reported = who_the_engine_thinks(engine)
            report.say(f"        the engine says this connection is: "
                       f"{reported or '(no answer)'}")
            if ok is False:
                report.bad(
                    f"asked Qlik to connect as {service} but the engine says "
                    f"{reported!r}. The X-Qlik-User header is not taking "
                    f"effect, so NOBODY is isolated."
                )
            service_apps = [a["name"] for a in engine.list_apps() if a["name"]]
            report.ok(f"{len(service_apps)} app(s): "
                      f"{', '.join(service_apps[:15])}")
            try:
                engine.open_app(config.APP_NAME)
                report.ok(f"opened APP_NAME {config.APP_NAME!r}")
            except QlikEngineError as e:
                report.bad(f"cannot open APP_NAME {config.APP_NAME!r}: {e}")
    except QlikEngineError as e:
        report.bad(f"could not connect: {e}")
        return
    except Exception as e:
        report.blew_up("connecting to Qlik", e)
        return

    if not session.per_user_engines():
        report.say()
        report.bad("QLIK_MODE is not enterprise, so every signed-in person "
                   "shares the connection above and sees exactly those apps.")
        return

    report.say()
    report.say("  As each account, through impersonation:")
    for row in rows:
        name = row["username"]
        if not (row["qlik_directory"] and row["qlik_user_id"]):
            report.bad(f"{name} has no Qlik identity, so it is refused at Qlik.")
            continue

        who = f"{row['qlik_directory']}\\{row['qlik_user_id']}"
        try:
            with QlikEngine(user_directory=row["qlik_directory"],
                            user_id=row["qlik_user_id"]) as engine:
                ok, reported = who_the_engine_thinks(engine)
                theirs = [a["name"] for a in engine.list_apps() if a["name"]]
        except QlikEngineError as e:
            report.bad(f"{name} ({who}) could not connect: {e}")
            continue
        except Exception as e:
            report.blew_up(f"connecting as {who} for {name}", e)
            continue

        report.say(f"    {name} -> asked for {who}")
        report.say(f"        engine says: {reported or '(no answer)'}")
        report.say(f"        {len(theirs)} app(s): "
                   f"{', '.join(theirs[:10]) or '(none)'}")

        if ok is False:
            report.bad(
                f"{name} asked to connect as {who} but the engine says "
                f"{reported!r}. Impersonation is not taking effect for this "
                f"account."
            )
        elif ok is None:
            report.note(f"{name}: could not read the identity back from the "
                        f"engine, so impersonation is unconfirmed.")

        if set(theirs) == set(service_apps):
            if ok is True:
                report.note(
                    f"{name} is correctly connected as {who}, yet sees exactly "
                    f"the service account's apps. Impersonation is working - "
                    f"this is a QMC security rule granting them the same "
                    f"apps, not a bug in this server."
                )
            else:
                report.bad(
                    f"{name} sees exactly the service account's apps and the "
                    f"identity could not be confirmed. Treat this as a leak "
                    f"until the line above says otherwise."
                )



def model(report, skip):
    report.rule("5. MODEL ENDPOINT")
    if skip:
        report.note("skipped (--skip-model)")
        return

    try:
        import config
        import llm
    except Exception as e:
        report.blew_up("importing the model client", e)
        return

    report.say(f"  endpoint  {config.OPENAI_BASE_URL}")
    report.say(f"  model     {config.OPENAI_MODEL}")
    report.say(f"  api key   {'set' if config.OPENAI_API_KEY else 'NOT SET'}")
    report.say()

    try:
        client = llm.build_client()
    except Exception as e:
        report.blew_up("building the model client", e)
        return

    try:
        import httpx

        headers = {}
        if config.OPENAI_API_KEY:
            headers["Authorization"] = f"Bearer {config.OPENAI_API_KEY}"
        raw = httpx.get(f"{config.OPENAI_BASE_URL.rstrip('/')}/models",
                        headers=headers, timeout=20)
        if raw.status_code == 200:
            try:
                ids = sorted(str(entry.get("id") or entry.get("name"))
                             for entry in (raw.json().get("data") or []))
            except ValueError:
                ids = []
            report.ok(f"GET /models -> 200, {len(ids)} model(s): "
                      f"{', '.join(ids[:12])}")
            if config.OPENAI_MODEL not in ids and ids:
                report.bad(f"OPENAI_MODEL {config.OPENAI_MODEL!r} is not in "
                           f"that list")
        else:
            detail = ""
            try:
                detail = ((raw.json().get("error") or {}).get("message")
                          or "")[:200]
            except ValueError:
                detail = (raw.text or "")[:200]
            report.bad(f"GET /models -> {raw.status_code}. {detail}")
    except Exception as e:
        report.bad(f"could not reach {config.OPENAI_BASE_URL}: {e}")

    report.say()
    report.say("  (the LLM tab in the browser shows a model name even when "
               "this fails,")
    report.say("   because the app falls back to OPENAI_MODEL when it cannot "
               "list;")
    report.say("   the two tool-call checks below are the honest test)")

    probe = [{
        "type": "function",
        "function": {
            "name": "create_chart",
            "description": "Create a chart in the open Qlik app.",
            "parameters": {
                "type": "object",
                "properties": {"chart_type": {"type": "string"},
                               "title": {"type": "string"}},
                "required": ["chart_type", "title"],
            },
        },
    }]
    ask = [
        {"role": "system", "content": "You build charts in Qlik. Call "
                                      "create_chart when asked for one."},
        {"role": "user", "content": "Draw me a table of BANK and BRANCH."},
    ]

    def called(message):
        names = []
        for call in (message.get("tool_calls") or []):
            function = call.get("function") or {}
            name = getattr(function, "name", None) or function.get("name")
            if name:
                names.append(name)
        return names

    for label, streaming in (("blocking", False), ("streaming", True)):
        try:
            if streaming:
                names = []
                for chunk in client.chat(model=config.OPENAI_MODEL,
                                         messages=list(ask), tools=probe,
                                         stream=True):
                    names += called(chunk.get("message") or {})
            else:
                response = client.chat(model=config.OPENAI_MODEL,
                                       messages=list(ask), tools=probe,
                                       stream=False)
                names = called(response.get("message") or {})
            if names:
                report.ok(f"{label}: called {names}")
            else:
                report.bad(f"{label}: no tool call - the assistant can only "
                           f"describe charts in chat, never build them")
        except Exception as e:
            report.bad(f"{label}: {e}")


def charts(report):
    report.rule("6. CHART TYPES")
    try:
        import chart_specs
    except Exception as e:
        report.blew_up("reading the chart catalogue", e)
        return

    available = getattr(chart_specs, "AVAILABLE_TYPES", ())
    if not available:
        report.note("No chart_overrides.py, so every chart type in "
                    "chart_defaults.py is assumed present.")
        report.note("If a chart draws 'Invalid visualization' in Qlik, run "
                    "harvest_charts.py.")
    else:
        report.ok(f"harvested from this server: {', '.join(available)}")

    for asked in ("table", "pivot", "barchart", "kpi", "linechart",
                  "piechart"):
        try:
            resolved = chart_specs.resolve_chart_type(asked)
            report.say(f"    asking for {asked:<12} writes qType "
                       f"{resolved!r}")
        except Exception as e:
            report.say(f"    asking for {asked:<12} FAILED: {e}")


def repository(report):
    report.rule("7. QLIK REPOSITORY (QRS)")
    try:
        import qrs
    except Exception as e:
        report.blew_up("importing the repository client", e)
        return

    if not qrs.enabled():
        report.note("QRS_ENABLED is false. This is optional - it only "
                    "automates account creation.")
        return

    report.say(f"  {qrs.summary()}")
    try:
        info = qrs.about()
        report.ok(f"reachable, build {info.get('buildVersion', '?')}")
    except Exception as e:
        report.bad(f"/about failed: {e}")
        return

    try:
        people = qrs.fetch_users()
        report.ok(f"can read accounts: {len(people)} in the repository")
    except Exception as e:
        report.bad(f"reading accounts failed: {e}")


def main(argv=None):
    args = parse_args(argv)
    report = Report()

    report.say("QLIK AI - DEPLOYMENT REPORT")
    report.say("Paste this whole file back. It contains no keys or passwords.")

    for step, call in (
        ("which code", lambda: which_code(report)),
        ("configuration", lambda: configuration(report)),
    ):
        try:
            call()
        except Exception as e:
            report.blew_up(step, e)

    try:
        rows = accounts(report)
    except Exception as e:
        report.blew_up("accounts", e)
        rows = []

    for step, call in (
        ("qlik", lambda: qlik(report, rows, args.skip_qlik)),
        ("model", lambda: model(report, args.skip_model)),
        ("charts", lambda: charts(report)),
        ("repository", lambda: repository(report)),
    ):
        try:
            call()
        except Exception as e:
            report.blew_up(step, e)

    report.rule("SUMMARY")
    if report.problems:
        report.say(f"  {len(report.problems)} problem(s) found:")
        for problem in report.problems:
            report.say(f"    - {problem}")
    else:
        report.say("  Nothing looks wrong from here.")

    body = report.text()

    # Write before printing. A Windows console on cp1252 raises on the first
    # non-ASCII app name, and the file is the thing worth keeping.
    written = None
    try:
        with io.open(args.out, "w", encoding="utf-8", newline="") as handle:
            handle.write(body + "\n")
        written = args.out
    except Exception as e:
        print(f"(could not write {args.out}: {e})", file=sys.stderr)

    try:
        print(body)
    except UnicodeEncodeError:
        safe = body.encode("ascii", "replace").decode("ascii")
        print(safe)
        print("\n(some characters could not be shown in this console; "
              "the file has them intact)")

    if written:
        print(f"\nWritten to {written} - paste that file back.")

    return 1 if report.problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
