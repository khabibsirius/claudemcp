import logging

log = logging.getLogger(__name__)

OK = "ok"
WARN = "warn"
STOP = "stop"


def findings():
    import config
    import session
    import users

    out = []

    if not config.AUTH_ENABLED:
        out.append((WARN, [
            "Sign-in is off. Everyone who opens the page shares one session,",
            "one set of conversations and one Qlik connection. Set",
            "AUTH_ENABLED=true to separate people.",
        ]))
        return out

    per_user = session.per_user_engines()

    if config.QLIK_MODE == config.ENTERPRISE and not per_user:
        out.append((STOP, ["QLIK_MODE says enterprise but per-user "
                           "connections are off. This should not happen."]))

    if not per_user:
        service = (f"{config.QLIK_USER_DIRECTORY or '(unset)'}\\"
                   f"{config.QLIK_USER_ID or '(unset)'}")
        out.append((WARN, [
            f"QLIK_MODE is {config.QLIK_MODE!r}, so every signed-in person "
            f"shares ONE",
            f"connection as {service} and sees every app that account can "
            f"see.",
            "Sign-in separates conversations but NOT Qlik data.",
            "Set QLIK_MODE=enterprise (and QLIK_PORT=4747) to give each "
            "person",
            "their own connection.",
        ]))
        return out

    if config.ALLOW_SHARED_QLIK_IDENTITY:
        out.append((WARN, [
            "ALLOW_SHARED_QLIK_IDENTITY is true, so an account with no Qlik",
            "identity runs as the service account and sees everything it can "
            "see.",
            "Leave it unset unless you know you need it.",
        ]))

    try:
        rows = list(users.listing())
    except Exception as e:
        log.debug("Could not read the accounts for the preflight: %s", e)
        return out

    blank = [r["username"] for r in rows
             if not (r["qlik_directory"] and r["qlik_user_id"])]
    if blank:
        lines = [
            f"{len(blank)} account(s) have no Qlik identity and will be "
            f"refused at Qlik:",
            "  " + ", ".join(blank),
            "Give each one the directory and user id it connects as:",
        ]
        lines += [f"  manage_users.py qlik {name} <DIRECTORY> <qlik user id>"
                  for name in blank[:5]]
        out.append((WARN, lines))
    elif rows:
        out.append((OK, [f"All {len(rows)} account(s) have their own Qlik "
                         f"identity."]))

    return out


def render(items, indent="  "):
    marks = {OK: "  ok  ", WARN: " note ", STOP: " STOP "}
    lines = []
    for level, body in items:
        for position, text in enumerate(body):
            mark = marks[level] if position == 0 else " " * len(marks[level])
            lines.append(f"{indent}{mark}  {text}")
        lines.append("")
    return "\n".join(lines).rstrip()


def worst(items):
    levels = {level for level, _ in items}
    if STOP in levels:
        return STOP
    if WARN in levels:
        return WARN
    return OK
