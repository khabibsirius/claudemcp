import argparse
import getpass
import sys

import users
from config import USERS_DB


def _fail(message):
    print(f"error: {message}", file=sys.stderr)
    return 1


def _find(username):
    user = users.by_username(username)
    if user is None:
        _fail(f"no user called {username!r} - `manage_users.py list` shows them all")
    return user


def _ask_password(given, who):
    if given:
        return given
    first = getpass.getpass(f"New password for {who}: ")
    if first != getpass.getpass("Repeat it: "):
        raise ValueError("the two passwords did not match")
    return first


def cmd_list(args):
    people = users.listing()
    if not people:
        print("No accounts yet. The server creates the first administrator on startup.")
        return 0

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
        print(f"  note: no Qlik identity set. On Enterprise this account can "
              f"sign in but is refused the moment it touches Qlik, because it "
              f"would otherwise run as the service account. Fix it with:\n"
              f"    manage_users.py qlik {user['username']} "
              f"<DIRECTORY> <qlik user id>")
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


def cmd_apps(args):
    import qrs

    if not qrs.enabled() and not args.force:
        return _fail("QRS_ENABLED is false. Set it in .env, or pass --force.")

    if args.directory and args.user_id:
        directory_name, user_id, label = args.directory, args.user_id, "given"
    else:
        user = _find(args.username)
        if user is None:
            return 1
        directory_name = user["qlik_directory"]
        user_id = user["qlik_user_id"]
        label = f"account {user['username']!r}"
        if not (directory_name and user_id):
            return _fail(
                f"{user['username']} has no Qlik identity, so there is nobody "
                f"to ask about. Set one:\n"
                f"    manage_users.py qlik {user['username']} "
                f"<DIRECTORY> <qlik user id>"
            )

    print(f"Asking {qrs.where()} what {directory_name}\\{user_id} can open "
          f"({label})")
    try:
        apps = qrs.hub_apps(directory_name, user_id)
    except qrs.QrsError as e:
        return _fail(str(e))

    if not apps:
        print("\n  Nothing. Qlik shows this person no apps at all - check the "
              "security rules in the QMC.")
        return 0

    print(f"\n{len(apps)} app(s):")
    print(f"  {'app':<34} {'stream':<18} {'owner':<22} privileges")
    for app in apps:
        print(f"  {app['name'][:33]:<34} "
              f"{(app['stream'] or '-')[:17]:<18} "
              f"{(app['owner'] or '-')[:21]:<22} "
              f"{', '.join(app['privileges']) or '(none)'}")

    writable = [a["name"] for a in apps if "update" in a["privileges"]]
    print(f"\n  can change {len(writable)} of them"
          + (f": {', '.join(writable[:6])}" if writable else ""))
    print("  'read' alone means they can open it but the assistant cannot "
          "save changes there.")
    return 0


def cmd_check(args):
    import config
    import session

    print("What this copy of the code and this .env actually do")
    print("-" * 68)

    guard_present = hasattr(session.Session, "borrows_service_account")
    print(f"  code has the identity guard   {'yes' if guard_present else 'NO'}")
    if not guard_present:
        print("      This copy predates the fix. A user with no Qlik identity")
        print("      will silently run as the service account.")

    print(f"  QLIK_MODE                     {config.QLIK_MODE}")
    if config.AUTH_ENABLED and not session.per_user_engines():
        print("      *** QLIK_MODE is not 'enterprise', so every signed-in "
              "person shares")
        print("      *** ONE connection as "
              f"{config.QLIK_USER_DIRECTORY or '(unset)'}\\"
              f"{config.QLIK_USER_ID or '(unset)'} and sees every app that "
              "account can")
        print("      *** see. Sign-in separates conversations but NOT Qlik "
              "data.")
        print("      *** Set QLIK_MODE=enterprise in .env and restart.")
    print(f"  AUTH_ENABLED                  {config.AUTH_ENABLED}")
    print(f"  ALLOW_SHARED_QLIK_IDENTITY    "
          f"{getattr(config, 'ALLOW_SHARED_QLIK_IDENTITY', 'not in this build')}")
    print(f"  per-user Qlik connections     {session.per_user_engines()}")
    print(f"  service account               "
          f"{config.QLIK_USER_DIRECTORY}\\{config.QLIK_USER_ID}")
    print(f"  accounts database             {config.USERS_DB}")

    import qrs

    print(f"  QRS_ENABLED                   "
          f"{getattr(config, 'QRS_ENABLED', 'not in this build')}")
    if getattr(config, "QRS_ENABLED", False):
        print(f"  reads accounts from           {qrs.where()}")
        print(f"  as                            {qrs.as_user_setting()}")
    else:
        print("      'Sync from Qlik' stays disabled until this is true, and")
        print("      the server has to be restarted after changing .env.")

    import preflight

    items = preflight.findings()
    if items:
        print()
        print(preflight.render(items, indent=""))

    enforcing = (guard_present and config.AUTH_ENABLED
                 and session.per_user_engines()
                 and not getattr(config, "ALLOW_SHARED_QLIK_IDENTITY", False))

    print()
    print("Accounts")
    print("-" * 68)
    rows = list(users.listing())
    if not rows:
        print("  (none yet)")
    blank = []
    for row in rows:
        has = bool(row["qlik_directory"] and row["qlik_user_id"])
        identity = (f"{row['qlik_directory']}\\{row['qlik_user_id']}" if has
                    else "(blank)")
        if has:
            verdict = "own Qlik connection"
        elif enforcing:
            verdict = "REFUSED at Qlik until an identity is set"
        else:
            verdict = "WOULD RUN AS THE SERVICE ACCOUNT"
            blank.append(row["username"])
        print(f"  {row['username']:<16} {row['role']:<6} {identity:<24} {verdict}")

    print()
    if not config.AUTH_ENABLED:
        print("  AUTH_ENABLED is false: everyone shares one session, one set of")
        print("  conversations and one Qlik connection. Nothing below separates")
        print("  people until you turn it on.")
        return 1

    if blank:
        print(f"  {len(blank)} account(s) would see everything the service account")
        print(f"  can see: {', '.join(blank)}")
        print("  Give each one an identity:")
        for name in blank:
            print(f"    manage_users.py qlik {name} <DIRECTORY> <qlik user id>")
        return 1

    missing = [row["username"] for row in rows
               if not (row["qlik_directory"] and row["qlik_user_id"])]
    if missing:
        print(f"  {len(missing)} account(s) have no Qlik identity and are refused")
        print(f"  at Qlik: {', '.join(missing)}")
        return 1

    print("  Every account has its own Qlik identity. Qlik decides what each")
    print("  person can open.")
    return 0


def cmd_sync(args):
    import qrs

    if not qrs.enabled() and not args.force:
        return _fail("QRS_ENABLED is false. Set it in .env, or pass --force "
                     "to sync anyway.")

    if args.as_user:
        qrs.AS_USER_OVERRIDE = args.as_user

    attempts = ([qrs.as_user_setting()] if args.as_user
                else list(dict.fromkeys([qrs.QRS_AS_USER, *qrs.CANDIDATES])))

    rows = None
    refusals = []
    for position, candidate in enumerate(attempts):
        qrs.AS_USER_OVERRIDE = candidate
        print(f"Reading {qrs.where()} as {candidate}")
        try:
            rows = qrs.fetch_users()
            break
        except qrs.QrsError as e:
            refusals.append((candidate, str(e)))
            last = position == len(attempts) - 1
            if "403" not in str(e) or last:
                break
            print("   refused; trying another account")

    if rows is None:
        for candidate, message in refusals:
            print(f"\nAs {candidate}:\n{message}", file=sys.stderr)
        return 1

    if qrs.as_user_setting() != qrs.QRS_AS_USER:
        print(f"   worked as {qrs.as_user_setting()} - put this in .env:")
        print(f"     QRS_AS_USER={qrs.as_user_setting()}")

    print(f"{len(rows)} account(s) in the Qlik repository")

    if args.dry_run:
        for row in rows:
            why = qrs.skipped(row)
            directory_name, user_id = qrs.identity(row)
            who = f"{directory_name}\\{user_id or '(no user id)'}"
            verdict = (f"skip - {why}" if why else
                       ("administrator" if qrs.is_admin(row) else "user"))
            print(f"   {who:<32} {verdict}")
        print("\n--dry-run, so nothing was written.")
        return 0

    outcome = qrs.sync(rows)

    for name, who, role in outcome["created"]:
        print(f"   created   {name:<16} {who:<28} {role}")
    for name, who, fields in outcome["updated"]:
        print(f"   updated   {name:<16} {who:<28} {', '.join(fields)}")
    for name, reason in outcome["failed"]:
        print(f"   FAILED    {name:<16} {reason}", file=sys.stderr)

    print(f"\n{len(outcome['created'])} created, "
          f"{len(outcome['updated'])} updated, "
          f"{len(outcome['unchanged'])} unchanged, "
          f"{len(outcome['skipped'])} skipped, "
          f"{len(outcome['failed'])} failed")

    if args.verbose:
        for who, why in outcome["skipped"]:
            print(f"   skipped   {who:<32} {why}")

    if outcome["passwords"]:
        print("\n  ---------------------------------------------------")
        print("  New accounts were given a password. This is shown once.")
        for name, password in sorted(outcome["passwords"].items()):
            print(f"  {name:<20} {password}")
        print("  Turn LDAP on and they sign in with their Windows password")
        print("  instead, and none of these are needed.")
        print("  ---------------------------------------------------")

    return 1 if outcome["failed"] else 0


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


def build_parser():
    parser = argparse.ArgumentParser(
        prog="manage_users.py",
        description="Accounts from the command line, for when the browser is not an option.",
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

    sub.add_parser(
        "check",
        help="report whether this deployment separates users properly"
    ).set_defaults(run=cmd_check)

    reach = sub.add_parser(
        "apps", help="ask Qlik which apps one person can actually open")
    reach.add_argument("username", nargs="?", default="",
                       help="an account here; its Qlik identity is used")
    reach.add_argument("--directory", default="",
                       help="ask about a Qlik identity directly instead")
    reach.add_argument("--user-id", dest="user_id", default="")
    reach.add_argument("--force", action="store_true",
                       help="run even when QRS_ENABLED is false")
    reach.set_defaults(run=cmd_apps)

    pull = sub.add_parser(
        "sync", help="create and refresh accounts from the Qlik repository")
    pull.add_argument("--dry-run", action="store_true",
                      help="show what would change and write nothing")
    pull.add_argument("--verbose", action="store_true",
                      help="also list who was skipped and why")
    pull.add_argument("--force", action="store_true",
                      help="run even when QRS_ENABLED is false")
    pull.add_argument("--as", dest="as_user", metavar="DIRECTORY\\user",
                      help="read the repository as this account instead of "
                           "QRS_AS_USER, e.g. INTERNAL\\sa_api")
    pull.set_defaults(run=cmd_sync)

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
