"""Smoke test: is everything this needs actually reachable?

    python check_connection.py
    python check_connection.py --ldap-user jsmith    # test a real sign-in

Run this first when something isn't working, and run it before go-live. It
checks each dependency separately so you find out *which* one is broken,
rather than watching the whole pipeline fail at once.

Four things, because there are now four ways a deployment fails on its first
morning: Qlik, the model endpoint, Active Directory, and the accounts
database. The model check costs a handful of tokens - it asks for one tool
call, because "the endpoint answers" and "this model can drive the
assistant" are different questions and only the second one matters.
"""

import argparse
import getpass

import directory
import llm
import session
import users
from config import (
    APP_NAME,
    ENTERPRISE,
    LDAP_BIND_USER,
    OLLAMA_MODEL,
    OPENAI_MODEL,
    QLIK_MODE,
)
from config import summary as config_summary
from ollama_client import OllamaClient, OllamaError
from qlik_engine import QlikEngine, QlikEngineError

OK = "  ok   "
FAIL = " FAIL  "


def check_qlik():
    """Connect, list apps, open the configured one, read its fields."""

    try:
        engine = QlikEngine()
    except QlikEngineError as e:
        print(f"{FAIL} Qlik connection\n        {e}")
        return False

    with engine:
        print(f"{OK} Connected to the Qlik Engine ({engine.mode} mode)")

        try:
            apps = engine.list_apps()
            names = [a["name"] for a in apps if a["name"]]
            print(f"{OK} {len(apps)} app(s) visible: {', '.join(names[:10]) or '(none named)'}")
        except QlikEngineError as e:
            print(f"{FAIL} Listing apps\n        {e}")
            return False

        try:
            engine.open_app(APP_NAME)
            print(f"{OK} Opened app {APP_NAME!r}")
        except QlikEngineError as e:
            print(f"{FAIL} Opening app {APP_NAME!r}\n        {e}")
            return False

        try:
            fields = engine.get_fields()
        except QlikEngineError as e:
            print(f"{FAIL} Reading the data model\n        {e}")
            return False

        if not fields:
            print(f"{FAIL} App {APP_NAME!r} has no fields - is data loaded into it?")
            return False

        preview = ", ".join(f["name"] for f in fields[:8])
        print(f"{OK} {len(fields)} fields: {preview}{' ...' if len(fields) > 8 else ''}")

        try:
            sheets = engine.list_sheets()
            print(f"{OK} {len(sheets)} existing sheet(s)")
        except QlikEngineError as e:
            print(f"{FAIL} Listing sheets\n        {e}")
            return False

    # Outside the `with`, so this socket has let go of the app it opened.
    # Changing app depends on exactly that, which is why the check comes
    # after rather than inside.
    if not check_switching(names):
        print("        ^ this is what stops the App picker changing app.")

    return True


def check_switching(names):
    """Change app the way the App picker does.

    Qlik Sense Desktop keeps ONE app open per engine and offers no way to
    close one - the app is freed when the session holding it disconnects, so
    changing app means a new session. This walks session.open_app, which is
    the path the web UI and the MCP tools both take, because holding the
    connection across a switch is what used to make the app unchangeable.

    A failure here with another app named is usually a second client - a
    running web_app.py, or the Qlik Sense window sitting inside an app.
    """
    others = [name for name in names if name != APP_NAME]
    if not others:
        print(f"{OK} Only one app on this Qlik - nothing to switch to")
        return True

    target = others[0]
    try:
        session.open_app(APP_NAME)
        session.open_app(target)
        print(f"{OK} Changed app from {APP_NAME!r} to {target!r}")
        session.open_app(APP_NAME)
        print(f"{OK} Changed back to {APP_NAME!r}")
    except QlikEngineError as e:
        print(f"{FAIL} Changing app to {target!r}\n        {e}")
        return False
    finally:
        session.close()

    return True


# ----------------------------------------------------------------------
# The model
# ----------------------------------------------------------------------

PROBE_TOOL = [{
    "type": "function",
    "function": {
        "name": "report_ok",
        "description": "Call this to confirm you can call tools.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}]


def check_model():
    """Can the configured model actually drive the assistant?

    Two different questions, and only the second one matters: an endpoint
    that answers is not the same as a model that will call a tool. The
    assistant is a tool-calling loop, so a model that ignores tools produces
    a fluent paragraph and builds nothing - which reads as the product being
    broken rather than as the wrong model being configured.
    """
    if llm.provider() == llm.OLLAMA:
        try:
            print(f"{OK} {OllamaClient(OLLAMA_MODEL).check()}")
            return True
        except OllamaError as e:
            print(f"{FAIL} Ollama\n        {e}")
            return False

    try:
        client = llm.build_client()
    except OllamaError as e:
        print(f"{FAIL} Model endpoint\n        {e}")
        return False

    try:
        names = [m["model"] for m in client.list().get("models", []) if m.get("model")]
        print(f"{OK} Model endpoint answered: {len(names)} model(s) offered")
        if names and OPENAI_MODEL not in names:
            # Not fatal - plenty of endpoints list nothing useful - but it is
            # the commonest cause of a 404 on the first question anybody asks.
            print(f"        note: {OPENAI_MODEL!r} is not in the list; "
                  f"first few are {', '.join(names[:5])}")
    except OllamaError as e:
        print(f"{FAIL} Model endpoint\n        {e}")
        return False

    return _check_tool_calling(client)


def _check_tool_calling(client):
    """One tiny call, asking the model to use a tool. Costs a few tokens."""
    try:
        response = client.chat(
            model=OPENAI_MODEL,
            messages=[{"role": "user",
                       "content": "Call the report_ok tool. Say nothing else."}],
            tools=PROBE_TOOL,
            options={"temperature": 0},
        )
    except OllamaError as e:
        print(f"{FAIL} Asking {OPENAI_MODEL!r} for a tool call\n        {e}")
        return False

    if (response.get("message") or {}).get("tool_calls"):
        print(f"{OK} {OPENAI_MODEL!r} answered and called a tool")
        return True

    print(f"{FAIL} {OPENAI_MODEL!r} answered but did not call the tool it was "
          f"asked to.\n        The assistant is a tool-calling loop; a model "
          f"that ignores tools will talk\n        fluently and build nothing. "
          f"Choose a model with tool support.")
    return False


# ----------------------------------------------------------------------
# Sign-in
# ----------------------------------------------------------------------

def check_directory():
    """Active Directory, when sign-in is delegated to it."""
    if not directory.enabled():
        print(f"{OK} Sign-in is local to this server (LDAP_ENABLED is off)")
        return True

    print(f"{OK} Directory configured: {directory.summary()}")

    if not LDAP_BIND_USER:
        # Without a service account there is nothing to bind as until
        # somebody types a password, so say so rather than implying it was
        # tested. --ldap-user is how you actually test it.
        print("        no LDAP_BIND_USER set, so nothing was bound - run with "
              "--ldap-user NAME to test a real sign-in")
        return True

    try:
        # A blank password is refused before the network, by design, so this
        # proves reachability rather than credentials.
        directory.authenticate(LDAP_BIND_USER, None)
    except directory.DirectoryError as e:
        print(f"{FAIL} Reaching the directory\n        {e}")
        return False

    print(f"{OK} Directory reachable")
    return True


def check_user_sign_in(username):
    """Prove one real person can sign in. The go-live check."""
    if not directory.enabled():
        print(f"{FAIL} --ldap-user needs LDAP_ENABLED=true")
        return False

    password = getpass.getpass(f"Windows password for {username}: ")
    try:
        entry = directory.authenticate(username, password)
    except directory.DirectoryError as e:
        print(f"{FAIL} Directory sign-in for {username!r}\n        {e}")
        return False

    if entry is None:
        print(f"{FAIL} Directory refused {username!r} - wrong password, or the "
              f"account is disabled or locked in AD")
        return False

    qlik_directory, qlik_user_id = directory.qlik_identity(entry)
    print(f"{OK} {username!r} signed in as {entry['display_name'] or username}")
    print(f"{OK} Qlik identity would be {qlik_directory}\\{qlik_user_id}")
    if not qlik_directory:
        print("        ^ LDAP_QLIK_DIRECTORY is not set, so this user would "
              "share the server's connection")
    if directory.decides_role():
        print(f"{OK} Administrator by group membership: {directory.is_admin(entry)}")
    return True


def check_accounts():
    """The accounts database, and the thing that silently costs you speed."""
    try:
        users.connect()
        people = users.listing()
    except Exception as e:
        print(f"{FAIL} Accounts database\n        {e}")
        return False

    admins = [u for u in people if u["is_admin"] and u["active"]]
    print(f"{OK} {len(people)} account(s), {len(admins)} active administrator(s)")

    if not admins:
        print(f"{FAIL} No active administrator - nobody can manage users.\n"
              f"        Fix: python manage_users.py create rescue --admin")
        return False

    if not any(u["auth_source"] == users.LOCAL for u in admins):
        print("        note: every administrator signs in through the "
              "directory. Keep one\n        local account for when a domain "
              "controller is unreachable.")

    if QLIK_MODE == ENTERPRISE:
        # The invisible queue: on Enterprise, anyone without a Qlik identity
        # falls back to the shared connection AND the global lock, so they
        # serialise against each other and nothing on screen says why.
        missing = [u["username"] for u in people
                   if u["active"] and not (u["qlik_directory"] and u["qlik_user_id"])]
        if missing:
            shown = ", ".join(missing[:8]) + (" ..." if len(missing) > 8 else "")
            print(f"        note: {len(missing)} active account(s) have no Qlik "
                  f"identity, so they\n        share the server's connection and "
                  f"queue behind each other: {shown}")
        else:
            print(f"{OK} Every active account has its own Qlik identity")

    return True


# ----------------------------------------------------------------------


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--ldap-user", metavar="NAME",
        help="prove one real person can sign in; prompts for their password")
    parser.add_argument(
        "--skip-qlik", action="store_true",
        help="check everything else when the engine is known to be down")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    print("Configuration")
    print("-" * 60)
    print(config_summary())
    print()

    print("Checks")
    print("-" * 60)
    if args.skip_qlik:
        qlik_ok = True
        print("       (Qlik skipped)")
    else:
        qlik_ok = check_qlik()

    model_ok = check_model()
    directory_ok = check_directory()
    accounts_ok = check_accounts()
    sign_in_ok = check_user_sign_in(args.ldap_user) if args.ldap_user else True
    print()

    if all((qlik_ok, model_ok, directory_ok, accounts_ok, sign_in_ok)):
        print("All good.")
        return 0

    if not qlik_ok:
        print("Fix the Qlik connection first - nothing else works without it.")
    elif not accounts_ok:
        print("Qlik works, but nobody can administer this. See above.")
    elif not model_ok:
        print("Qlik works. The editor and Load data are usable; the assistant "
              "is not.")
    else:
        print("Something above needs attention.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
