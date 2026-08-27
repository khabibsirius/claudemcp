"""Accounts from the command line, for when the browser is not an option.

    python manage_users.py list
    python manage_users.py unlock admin
    python manage_users.py password admin
    python manage_users.py create jsmith --name "J Smith" --qlik-user jsmith

This exists because of a specific failure with no other way out: five wrong
passwords lock an account for fifteen minutes, and on an installation with
one administrator that locks the only person who could unlock it. The admin
page is behind the login that is refusing them. Without this the recovery
procedure was hand-editing SQLite, which is not a procedure to hand anyone
running a bank's server at four in the afternoon.

It talks to the database directly rather than to the running server, so it
works whether or not the service is up. Changes take effect immediately
either way: every request re-reads the account, so disabling somebody or
resetting their password ends their sessions on the next request rather than
whenever the process next restarts.
"""

import argparse
import getpass
import sys

import users
from config import USERS_DB


def _fail(message):
    print(f"error: {message}", file=sys.stderr)
    return 1


def _find(username):
    """A user, or None with the reason already printed."""
    user = users.by_username(username)
    if user is None:
        _fail(f"no user called {username!r} - `manage_users.py list` shows them all")
    return user


def _ask_password(given, who):
    """A password from the arguments, or typed twice without echoing.

    Not echoed and not in the shell history, because a password in
    `.bash_history` or a Windows console buffer is a password on disk.
    """
    if given:
        return given
    first = getpass.getpass(f"New password for {who}: ")
    if first != getpass.getpass("Repeat it: "):
        raise ValueError("the two passwords did not match")
    return first


# ----------------------------------------------------------------------


def cmd_list(args):
    people = users.listing()
    if not people:
        print("No accounts yet. The server creates the first administrator on startup.")
        return 0

    # The header is eight characters, so a table of short usernames must not
    # narrow the column below it.
    width = max([len("USERNAME")] + [len(u["username"]) for u in people])
    print(f"{'USERNAME'.ljust(width)}  ROLE   SIGN-IN  STATUS      "
          f"QLIK IDENTITY        LAST SEEN")
    for user in people:
        status = "disabled" if not user["active"] else (
            "LOCKED" if user["locked_until"] and users.locked(user["username"])
            else "active")
        identity = (f"{user['qlik_directory']}\\{user['qlik_user_id']}"
                    if user["qlik_directory"] and user["qlik_user_id"] else "-")
        source = "AD" if user["from_directory"] else "local"
        print(f"{user['username'].ljust(width)}  "
              f"{user['role'][:5].ljust(5)}  {source.ljust(7)}  {status.ljust(10)}  "
              f"{identity[:20].ljust(20)} {user['last_login'] or 'never'}"
              + ("  [MCP token]" if user["has_token"] else ""))
    return 0


def cmd_create(args):
    try:
        password = _ask_password(args.password, args.username)
        user = users.create(
            args.username, password,
            role=users.ADMIN if args.admin else users.USER,
            display_name=args.name or "",
            qlik_directory=args.qlik_directory or "",
            qlik_user_id=args.qlik_user or "",
        )
    except (users.UserError, ValueError) as e:
        return _fail(str(e))

    users.audit("user.created", detail={"username": user["username"],
                                        "role": user["role"], "by": "command line"})
    print(f"Created {user['username']} ({user['role']}).")
    if not (user["qlik_directory"] and user["qlik_user_id"]):
        # Worth saying: on Enterprise these people share one connection and
        # one lock, so a rollout that forgets this has a queue nobody can see.
        print("  note: no Qlik identity set, so on Enterprise this account "
              "will share the server's connection rather than having its own.")
    return 0


def cmd_password(args):
    user = _find(args.username)
    if user is None:
        return 1
    if user["from_directory"]:
        return _fail(
            f"{user['username']} signs in with their Windows password, which "
            "lives in Active Directory - there is nothing to set here. Reset "
            "it in AD, or use `unlock` if they are locked out of this server."
        )
    try:
        users.set_password(user["id"], _ask_password(args.password, user["username"]))
    except (users.UserError, ValueError) as e:
        return _fail(str(e))

    # Everywhere they are signed in now was signed in with the old password.
    users.end_sessions(user["id"])
    users.audit("password.reset", detail={"username": user["username"],
                                          "by": "command line"})
    print(f"Password changed for {user['username']}. "
          "They have been signed out everywhere.")
    return 0


def cmd_unlock(args):
    user = _find(args.username)
    if user is None:
        return 1
    was = users.locked(user["username"])
    users.unlock(user["id"])
    users.audit("user.unlocked", detail={"username": user["username"],
                                         "by": "command line"})
    print(f"{user['username']} is unlocked."
          if was else f"{user['username']} was not locked; failure count cleared.")
    return 0


def cmd_enable(args):
    return _set_active(args.username, True)


def cmd_disable(args):
    return _set_active(args.username, False)


def _set_active(username, active):
    user = _find(username)
    if user is None:
        return 1
    try:
        users.update(user["id"], active=active)
    except users.UserError as e:
        return _fail(str(e))
    users.audit("user.updated", detail={"username": user["username"],
                                        "active": active, "by": "command line"})
    print(f"{user['username']} is now {'enabled' if active else 'disabled'}.")
    if not active:
        print("  Their sessions have been ended.")
    return 0


def cmd_role(args):
    user = _find(args.username)
    if user is None:
        return 1
    try:
        users.update(user["id"], role=args.role)
    except users.UserError as e:
        return _fail(str(e))
    users.audit("user.updated", detail={"username": user["username"],
                                        "role": args.role, "by": "command line"})
    print(f"{user['username']} is now {args.role}.")
    return 0


def cmd_qlik(args):
    user = _find(args.username)
    if user is None:
        return 1
    try:
        users.update(user["id"], qlik_directory=args.directory,
                     qlik_user_id=args.user_id)
    except users.UserError as e:
        return _fail(str(e))
    users.audit("user.updated", detail={
        "username": user["username"], "qlik_directory": args.directory,
        "qlik_user_id": args.user_id, "by": "command line"})
    print(f"{user['username']} now connects to Qlik as "
          f"{args.directory}\\{args.user_id}.")
    return 0


def cmd_token(args):
    user = _find(args.username)
    if user is None:
        return 1
    if args.revoke:
        users.revoke_token(user["id"])
        users.audit("token.revoked", detail={"username": user["username"],
                                             "by": "command line"})
        print(f"Token revoked for {user['username']}.")
        return 0

    if not user["is_admin"]:
        print(f"note: {user['username']} is not an administrator, and the MCP "
              "endpoint is administrators only - the token will be refused.",
              file=sys.stderr)
    token = users.mint_token(user["id"])
    users.audit("token.minted", detail={"username": user["username"],
                                        "by": "command line"})
    print(f"MCP token for {user['username']} - shown once, not stored readably:")
    print(f"\n  {token}\n")
    return 0


def cmd_sessions(args):
    if args.end:
        if users.end_session_by_handle(args.end):
            users.audit("session.ended", detail={"handle": args.end,
                                                 "by": "command line"})
            print(f"Session {args.end} ended.")
            return 0
        return _fail(f"no session starting {args.end!r}")

    users.purge_expired()
    live = users.active_sessions()
    if not live:
        print("Nobody is signed in.")
        return 0
    print(f"{'HANDLE':<10} {'USERNAME':<16} {'FROM':<16} LAST SEEN")
    for entry in live:
        print(f"{entry['handle']:<10} {entry['username']:<16} "
              f"{(entry['ip'] or '-'):<16} {entry['last_seen']}"
              + ("  [acting as another user]" if entry["acting_as"] else ""))
    return 0


def cmd_audit(args):
    user_id = None
    if args.user:
        user = _find(args.user)
        if user is None:
            return 1
        user_id = user["id"]

    entries = users.audit_trail(limit=args.limit, user_id=user_id,
                               action=args.action)
    if not entries:
        print("Nothing recorded that matches.")
        return 0
    for entry in reversed(entries):
        print(f"{entry['at']}  {(entry['username'] or '-'):<14} "
              f"{entry['action']:<18} {entry['detail'] or ''}")
    return 0


def cmd_prune(args):
    """Apply the retention policy now, or say what it would remove."""
    import history
    from config import AUDIT_RETENTION_DAYS, HISTORY_RETENTION_DAYS

    audit_days = args.audit_days if args.audit_days is not None else AUDIT_RETENTION_DAYS
    chat_days = args.chat_days if args.chat_days is not None else HISTORY_RETENTION_DAYS

    if audit_days <= 0 and chat_days <= 0:
        print("No retention policy is set, so nothing would be removed.")
        print("  Set AUDIT_RETENTION_DAYS and HISTORY_RETENTION_DAYS in .env,")
        print("  or pass --audit-days / --chat-days to try one out.")
        return 0

    records = users.prune_audit(audit_days, dry_run=args.dry_run)
    chats = history.prune_old(chat_days, dry_run=args.dry_run)

    verb = "would remove" if args.dry_run else "removed"
    if audit_days > 0:
        print(f"Audit trail:   {verb} {records} record(s) older than "
              f"{audit_days} day(s)")
    else:
        print("Audit trail:   kept (no policy set)")

    if chat_days > 0:
        print(f"Conversations: {verb} {chats} chat(s) last used over "
              f"{chat_days} day(s) ago")
    else:
        print("Conversations: kept (no policy set)")

    if args.dry_run:
        print()
        print("Nothing was deleted. Run without --dry-run to apply.")
    return 0


def cmd_where(args):
    print(f"Account database: {USERS_DB}")
    print(f"Accounts: {users.count()}   Administrators: {users.count(users.ADMIN)}")
    return 0


# ----------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="manage_users.py",
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Passwords are asked for without echoing unless --password is "
               "given; a password on the command line ends up in the shell "
               "history, so prefer the prompt.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="every account").set_defaults(run=cmd_list)
    sub.add_parser("where", help="which database is being used").set_defaults(run=cmd_where)

    prune = sub.add_parser(
        "prune", help="apply the retention policy to the trail and conversations")
    prune.add_argument("--dry-run", action="store_true",
                       help="say what would go, delete nothing")
    prune.add_argument("--audit-days", type=int,
                       help="override AUDIT_RETENTION_DAYS for this run")
    prune.add_argument("--chat-days", type=int,
                       help="override HISTORY_RETENTION_DAYS for this run")
    prune.set_defaults(run=cmd_prune)

    make = sub.add_parser("create", help="add an account")
    make.add_argument("username")
    make.add_argument("--password", help="prompted for if omitted")
    make.add_argument("--admin", action="store_true", help="make them an administrator")
    make.add_argument("--name", help="display name")
    make.add_argument("--qlik-directory", help="Qlik user directory, e.g. YOURDOMAIN")
    make.add_argument("--qlik-user", help="Qlik user id")
    make.set_defaults(run=cmd_create)

    reset = sub.add_parser("password", help="set somebody's password")
    reset.add_argument("username")
    reset.add_argument("--password", help="prompted for if omitted")
    reset.set_defaults(run=cmd_password)

    unlock = sub.add_parser("unlock", help="clear a lockout after failed logins")
    unlock.add_argument("username")
    unlock.set_defaults(run=cmd_unlock)

    for name, handler, helptext in (
        ("enable", cmd_enable, "let an account sign in again"),
        ("disable", cmd_disable, "stop an account signing in, and end its sessions"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("username")
        p.set_defaults(run=handler)

    role = sub.add_parser("role", help="promote or demote")
    role.add_argument("username")
    role.add_argument("role", choices=list(users.ROLES))
    role.set_defaults(run=cmd_role)

    qlik = sub.add_parser("qlik", help="set who they connect to Qlik Sense as")
    qlik.add_argument("username")
    qlik.add_argument("directory", help="Qlik user directory, e.g. YOURDOMAIN")
    qlik.add_argument("user_id", help="Qlik user id")
    qlik.set_defaults(run=cmd_qlik)

    token = sub.add_parser("token", help="issue or revoke an MCP token")
    token.add_argument("username")
    token.add_argument("--revoke", action="store_true")
    token.set_defaults(run=cmd_token)

    live = sub.add_parser("sessions", help="who is signed in")
    live.add_argument("--end", metavar="HANDLE", help="end one session")
    live.set_defaults(run=cmd_sessions)

    trail = sub.add_parser("audit", help="what has happened")
    trail.add_argument("--limit", type=int, default=50)
    trail.add_argument("--user", help="only this username")
    trail.add_argument("--action", help="only this action, e.g. login.failed")
    trail.set_defaults(run=cmd_audit)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        users.connect()
    except Exception as e:
        return _fail(f"could not open {USERS_DB}: {e}")
    try:
        return args.run(args)
    except KeyboardInterrupt:
        print()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
