import logging

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

import directory
import session
import users
from config import AUTH_ENABLED, COOKIE_SECURE, SESSION_COOKIE, SESSION_HOURS

log = logging.getLogger(__name__)

PUBLIC_PATHS = frozenset({
    "/login",
    "/api/login",
    "/api/session",
    "/healthz",
    "/favicon.ico",
})

LOCAL_OPERATOR = {
    "id": None,
    "username": "local",
    "display_name": "Local operator",
    "role": users.ADMIN,
    "is_admin": True,
    "active": True,
    "qlik_directory": "",
    "qlik_user_id": "",
}


class Identity:
    __slots__ = ("user", "acting_as", "token", "ip")

    def __init__(self, user, acting_as=None, token=None, ip=""):
        self.user = user
        self.acting_as = acting_as
        self.token = token
        self.ip = ip

    @property
    def effective(self):
        return self.acting_as or self.user

    @property
    def is_admin(self):
        return bool(self.acting_as is None and self.user.get("is_admin"))

    @property
    def impersonating(self):
        return self.acting_as is not None

    def audited(self, **detail):
        if self.impersonating:
            detail["acting_as"] = self.acting_as.get("username")
            detail["really"] = self.user.get("username")
        return detail

    def summary(self):
        person = self.effective
        return {
            "id": person.get("id"),
            "username": person.get("username"),
            "display_name": person.get("display_name") or person.get("username"),
            "role": person.get("role"),
            "is_admin": self.is_admin,
            "qlik_directory": person.get("qlik_directory"),
            "qlik_user_id": person.get("qlik_user_id"),
            "impersonating": self.impersonating,
            "real_username": self.user.get("username") if self.impersonating else None,
            "auth_enabled": AUTH_ENABLED,
        }


def client_ip(request):
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def bearer_token(request):
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.headers.get("x-api-key", "").strip()


def identify(request):
    if not AUTH_ENABLED:
        return Identity(LOCAL_OPERATOR, ip=client_ip(request))

    ip = client_ip(request)

    try:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            user, acting_as = users.session_user(token)
            if user is not None:
                return Identity(user, acting_as, token, ip)

        api_token = bearer_token(request)
        if api_token:
            user = users.by_token(api_token)
            if user is not None:
                return Identity(user, ip=ip)
    except Exception:
        log.exception("Could not identify the caller")

    return None


def is_public(path):
    if path in PUBLIC_PATHS:
        return True
    return path.startswith("/static/")


def wants_html(request):
    if request.url.path.startswith(("/api/", "/mcp")):
        return False
    return "text/html" in request.headers.get("accept", "")


MCP_IS_SHARED = (
    "The MCP endpoint works on the server's shared Qlik session rather than "
    "your own, so it is limited to administrators. Use the web interface, "
    "which is per user."
)


async def gate(request, call_next):
    identity = identify(request)
    request.state.identity = identity

    if identity is None and not is_public(request.url.path):
        if wants_html(request):
            return RedirectResponse("/login", status_code=303)
        return JSONResponse(
            {"detail": "Sign in to continue."},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )

    if identity is not None and request.url.path.startswith("/mcp"):
        if not identity.user.get("is_admin"):
            return JSONResponse({"detail": MCP_IS_SHARED}, status_code=403)

    return await call_next(request)


def require_user(request: Request) -> Identity:
    identity = getattr(request.state, "identity", None)
    if identity is None:
        raise HTTPException(401, "Sign in to continue.")
    return identity


def require_admin(request: Request) -> Identity:
    identity = require_user(request)
    if not identity.is_admin:
        if identity.impersonating:
            raise HTTPException(
                403,
                "You are acting as another user. Stop first to manage accounts.",
            )
        raise HTTPException(403, "Administrators only.")
    return identity


def qlik_session(request: Request):
    identity = require_user(request)
    person = identity.effective
    if person.get("id") is None:
        return session.system()
    return session.for_user(person)


def sign_in(username, password, ip=""):
    name = users.clean_username_soft(username)
    if not name or not password:
        return None, "credentials"

    existing = users.by_username(name)

    if existing is not None and not existing["active"]:
        return None, "credentials"

    if users.locked(name):
        return None, "locked"

    use_directory = (
        existing["from_directory"] if existing is not None
        else directory.enabled()
    )

    if not use_directory:
        user = users.authenticate(name, password)
        return (user, None) if user else (None, "credentials")

    if not directory.enabled():
        log.warning("%s is a directory account but LDAP_ENABLED is off", name)
        return None, "directory"

    try:
        entry = directory.authenticate(name, password)
    except directory.DirectoryError as e:
        log.error("Directory sign-in failed for %s: %s", name, e)
        return None, "directory"

    if entry is None:
        users.note_failed_login(name)
        return None, "credentials"

    role = None
    if directory.decides_role():
        role = users.ADMIN if directory.is_admin(entry) else users.USER

    return users.from_directory(entry, directory.qlik_identity(entry),
                                role=role), None


def set_cookie(response, token):
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_HOURS * 3600,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        path="/",
    )
    return response


def clear_cookie(response):
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
