import logging
import os
import secrets
import string

from config import (
    CLIENT_CERT,
    CLIENT_KEY,
    QLIK_CERT_DIR,
    QLIK_SSL_VERIFY,
    QRS_ADMIN_PROPERTY,
    QRS_ADMIN_ROLE,
    QRS_ADMIN_VALUE,
    QRS_AS_USER,
    QRS_ENABLED,
    QRS_HOST,
    QRS_PORT,
    QRS_SKIP_DIRECTORIES,
    QRS_TIMEOUT,
    ROOT_CERT,
)

log = logging.getLogger(__name__)

PAGE = 200


class QrsError(Exception):
    pass


def enabled():
    return bool(QRS_ENABLED)


def where():
    return f"https://{QRS_HOST}:{QRS_PORT}/qrs"


def _xrfkey():
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(16))


AS_USER_OVERRIDE = None

CANDIDATES = ("INTERNAL\\sa_api", "INTERNAL\\sa_repository")


def as_user_setting():
    return AS_USER_OVERRIDE or QRS_AS_USER


def _as_user():
    text = (as_user_setting() or "").strip()
    directory, _, user_id = text.replace("/", "\\").partition("\\")
    if not user_id:
        raise QrsError(
            f"QRS_AS_USER must look like DIRECTORY\\user, got "
            f"{as_user_setting()!r}."
        )
    return f"UserDirectory={directory};UserId={user_id}"


def _certificates():
    if not QLIK_CERT_DIR:
        raise QrsError(
            "QLIK_CERT_DIR is not set, so there is no certificate to reach the "
            "repository with. Export the platform-independent .pem set from the "
            "QMC: Certificates > Export certificates."
        )

    paths = {name: os.path.join(QLIK_CERT_DIR, name)
             for name in (CLIENT_CERT, CLIENT_KEY, ROOT_CERT)}
    missing = [name for name, path in paths.items() if not os.path.exists(path)]
    if missing:
        raise QrsError(
            f"QLIK_CERT_DIR ({QLIK_CERT_DIR}) is missing {', '.join(missing)}."
        )

    verify = paths[ROOT_CERT] if QLIK_SSL_VERIFY else False
    return (paths[CLIENT_CERT], paths[CLIENT_KEY]), verify


def _client():
    import httpx

    cert, verify = _certificates()
    if verify is False:
        log.warning(
            "QLIK_SSL_VERIFY is off - the repository's certificate is not "
            "being checked."
        )
    return httpx.Client(cert=cert, verify=verify, timeout=QRS_TIMEOUT)


def as_header(directory_name, user_id):
    return f"UserDirectory={directory_name};UserId={user_id}"


def _get(client, path, params=None, as_user=None):
    import httpx

    key = _xrfkey()
    url = f"{where()}{path}"
    try:
        response = client.get(
            url,
            params={**(params or {}), "xrfkey": key},
            headers={
                "X-Qlik-Xrfkey": key,
                "X-Qlik-User": as_user or _as_user(),
                "Accept": "application/json",
            },
        )
    except httpx.HTTPError as e:
        raise QrsError(f"Could not reach the repository at {where()}: {e}") from e

    if response.status_code == 401:
        raise QrsError(
            f"The repository rejected the certificate (401). The client "
            f"certificate in {QLIK_CERT_DIR} has to be one the QMC exported, "
            f"and QRS_AS_USER ({as_user_setting()!r}) has to be an account "
            f"the repository trusts."
        )
    if response.status_code == 403:
        raise QrsError(_explain_403(client, path))
    if response.status_code >= 400:
        raise QrsError(
            f"The repository returned {response.status_code} for {path}: "
            f"{(response.text or '')[:300]}"
        )

    try:
        return response.json()
    except ValueError as e:
        raise QrsError(
            f"The repository returned something that is not JSON for {path}."
        ) from e


def _explain_403(client, path):
    lines = [
        f"The repository accepted the certificate but refused the request "
        f"(403): {as_user_setting()} is not allowed to read {path}.",
    ]

    reachable = None
    if path != "/about":
        try:
            _get(client, "/about")
            reachable = True
        except QrsError:
            reachable = False

    if reachable is True:
        lines += [
            "  /about worked as the same account, so the certificate and the "
            "identity header are both fine.",
            f"  This account simply lacks rights to read {path}. In the QMC, "
            f"give it RootAdmin, or use an account that already has it.",
        ]
    elif reachable is False:
        lines += [
            "  /about was refused too, so this is not about one endpoint - "
            "the identity is not being accepted at all.",
            "  Check that the certificate in QLIK_CERT_DIR was exported from "
            "THIS Qlik server (QMC > Certificates > Export certificates, "
            "platform independent), and that port 4242 is reachable.",
        ]

    lines += [
        "  Accounts worth trying, in order:",
        "    QRS_AS_USER=INTERNAL\\sa_api          (the usual one for "
        "certificate calls)",
        "    QRS_AS_USER=INTERNAL\\sa_repository",
        "    QRS_AS_USER=<YOURDIRECTORY>\\<an account with RootAdmin>",
        "  Try one without editing .env:  manage_users.py sync --dry-run "
        "--as INTERNAL\\sa_api",
    ]
    return "\n".join(lines)


def about():
    with _client() as client:
        return _get(client, "/about")


def fetch_users():
    people = []
    with _client() as client:
        skip = 0
        while True:
            page = _get(client, "/user/full", {
                "orderby": "userDirectory, userId",
                "skip": skip,
                "take": PAGE,
            })
            if not isinstance(page, list):
                raise QrsError("The repository did not return a list of users.")
            people.extend(page)
            if len(page) < PAGE:
                break
            skip += len(page)
            if skip > 100_000:
                raise QrsError("The repository returned an unreasonable number "
                               "of users; stopping.")
    return people


def hub_apps(directory_name, user_id):
    header = as_header(directory_name, user_id)
    with _client() as client:
        rows = _get(client, "/app/hublist/full", as_user=header)

    if not isinstance(rows, list):
        raise QrsError("The repository did not return a list of apps.")

    apps = []
    for row in rows:
        stream = row.get("stream") or {}
        apps.append({
            "id": row.get("id"),
            "name": (row.get("name") or "").strip(),
            "stream": (stream.get("name") or "").strip(),
            "published": bool(row.get("published")),
            "owner": "\\".join(
                part for part in (
                    (row.get("owner") or {}).get("userDirectory"),
                    (row.get("owner") or {}).get("userId"),
                ) if part
            ),
            "privileges": sorted(row.get("privileges") or []),
        })
    return sorted(apps, key=lambda a: a["name"].lower())


def _properties(row):
    for entry in row.get("customProperties") or []:
        definition = entry.get("definition") or {}
        name = definition.get("name") or entry.get("name")
        value = entry.get("value")
        if name:
            yield str(name), str(value or "")


def is_admin(row):
    if QRS_ADMIN_PROPERTY:
        wanted_name = QRS_ADMIN_PROPERTY.strip().lower()
        wanted_value = (QRS_ADMIN_VALUE or "").strip().lower()
        for name, value in _properties(row):
            if name.strip().lower() != wanted_name:
                continue
            if not wanted_value or value.strip().lower() == wanted_value:
                return True
        return False

    if not QRS_ADMIN_ROLE:
        return False

    wanted = QRS_ADMIN_ROLE.strip().lower()
    return any(str(role).strip().lower() == wanted
               for role in (row.get("roles") or []))


def skipped(row):
    directory = (row.get("userDirectory") or "").strip().lower()
    if directory in QRS_SKIP_DIRECTORIES:
        return f"in the {row.get('userDirectory')} directory"
    if row.get("removedExternally"):
        return "removed from the directory it came from"
    if row.get("inactive"):
        return "marked inactive in Qlik"
    if not (row.get("userId") or "").strip():
        return "has no user id"
    return None


def identity(row):
    return ((row.get("userDirectory") or "").strip(),
            (row.get("userId") or "").strip())


def summary():
    if not enabled():
        return "QRS sync is off (QRS_ENABLED=false)"
    marker = (f"custom property {QRS_ADMIN_PROPERTY!r}" if QRS_ADMIN_PROPERTY
              else f"role {QRS_ADMIN_ROLE!r}")
    return (f"{where()} as {as_user_setting()}, administrators marked by "
            f"{marker}")


def _generated_password():
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(16))


def sync(rows=None):
    import directory
    import users

    if rows is None:
        rows = fetch_users()

    by_ldap = directory.enabled()
    outcome = {"created": [], "updated": [], "unchanged": [],
               "skipped": [], "failed": [], "passwords": {}}

    for row in rows:
        directory_name, user_id = identity(row)
        who = f"{directory_name}\\{user_id}" if user_id else str(row.get("id"))

        why = skipped(row)
        if why:
            outcome["skipped"].append((who, why))
            continue

        name = users.clean_username_soft(user_id)
        if not name:
            outcome["skipped"].append((who, "the user id is not usable here"))
            continue

        role = users.ADMIN if is_admin(row) else users.USER
        display = (row.get("name") or "").strip() or name

        existing = users.by_username(name)
        try:
            if existing is None:
                password = None if by_ldap else _generated_password()
                created = users.create(
                    name, password, role=role, display_name=display,
                    qlik_directory=directory_name, qlik_user_id=user_id,
                    auth_source=users.DIRECTORY if by_ldap else users.LOCAL,
                )
                users.audit("user.synced", user=created,
                            detail={"username": name, "from": "qrs"})
                outcome["created"].append((name, who, role))
                if password:
                    outcome["passwords"][name] = password
                continue

            wanted = {
                "display_name": display,
                "qlik_directory": directory_name,
                "qlik_user_id": user_id,
                "role": role,
            }
            changed = {k: v for k, v in wanted.items() if existing.get(k) != v}
            if not changed:
                outcome["unchanged"].append((name, who, role))
                continue

            users.update(existing["id"], **changed)
            outcome["updated"].append((name, who, sorted(changed)))
        except users.UserError as e:
            outcome["failed"].append((name, str(e)))
            log.warning("Could not sync %s: %s", who, e)

    return outcome
