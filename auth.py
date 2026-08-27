"""Who is asking, on every request.

users.py knows what an account is; this knows how one arrives over HTTP. It
holds the cookie, the dependency the endpoints declare, and the middleware
that closes the door in front of everything - including `/mcp`, which is
mounted as a sub-application and so never sees a route dependency at all.
That mount is why the gate is middleware rather than a `Depends` on each
endpoint: an endpoint-by-endpoint scheme protects the endpoints somebody
remembered, and `/mcp` is precisely the one that was forgotten.

Two ways in, because there are two kinds of caller:

- a **cookie**, for the browser. Server-side sessions rather than a signed
  token, so an administrator disabling an account ends it now rather than
  whenever the token happens to expire.
- a **bearer token**, for MCP clients. Claude Code cannot be asked to hold a
  login cookie, and it should not be handed a password.

Cross-site request forgery is handled by the cookie being `SameSite=Lax`,
which is what stops another origin's page from POSTing as a logged-in user.
Worth revisiting if this ever needs to be embedded in a Qlik mashup, because
that is a cross-site context by definition.
"""

import logging

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

import directory
import session
import users
from config import AUTH_ENABLED, COOKIE_SECURE, SESSION_COOKIE, SESSION_HOURS

log = logging.getLogger(__name__)

# Reachable with no login at all. Everything not listed here needs one -
# a deny-by-default list, so a new endpoint is protected by forgetting
# rather than exposed by it.
PUBLIC_PATHS = frozenset({
    "/login",
    "/api/login",
    "/api/session",
    "/healthz",
    "/favicon.ico",
})

# The one account that exists when AUTH_ENABLED is off: the person sitting
# at the machine. It has no id, so it maps to the system session and files
# its conversations in the history root, exactly as before there were
# accounts at all.
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
    """Who is making this request, and on whose behalf.

    `user` is the account that logged in. `acting_as` is set only while an
    administrator is standing in for someone to reproduce a problem. Work is
    done as `effective`; the audit trail names both, because "the admin did
    it as this user" and "this user did it" are different events and only one
    of them is the user's fault.
    """

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
        # The real account decides. An admin acting as an ordinary user gets
        # that user's powers, which is the point of the feature - otherwise
        # they are not reproducing anything.
        return bool(self.acting_as is None and self.user.get("is_admin"))

    @property
    def impersonating(self):
        return self.acting_as is not None

    def audited(self, **detail):
        """Detail for an audit record, naming the real actor when they differ."""
        if self.impersonating:
            detail["acting_as"] = self.acting_as.get("username")
            detail["really"] = self.user.get("username")
        return detail

    def summary(self):
        """What the browser is told about the person it is showing."""
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
            # Present so the banner can say who you really are while acting
            # as somebody else.
            "real_username": self.user.get("username") if self.impersonating else None,
            "auth_enabled": AUTH_ENABLED,
        }


# ----------------------------------------------------------------------
# Reading a request
# ----------------------------------------------------------------------

def client_ip(request):
    """The caller's address, for the audit trail.

    X-Forwarded-For is trusted only for its first entry and only because
    this is expected to sit behind the institution's own reverse proxy. It
    is an audit field, never an access decision.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def bearer_token(request):
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    # MCP clients that cannot set Authorization can send the token here.
    return request.headers.get("x-api-key", "").strip()


def identify(request):
    """The Identity behind a request, or None.

    Never raises: an unreadable database should turn into "not logged in"
    and a login page, not a 500 on every page of the product.
    """
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
    # The login page's own assets, if it ever grows any.
    return path.startswith("/static/")


def wants_html(request):
    """Whether to redirect to the login page or answer with JSON.

    A browser following a link should land on the login form; a fetch() from
    a page that has been open since before the session expired should get a
    401 it can act on, not an HTML page parsed as JSON.
    """
    if request.url.path.startswith(("/api/", "/mcp")):
        return False
    return "text/html" in request.headers.get("accept", "")


MCP_IS_SHARED = (
    "The MCP endpoint works on the server's shared Qlik session rather than "
    "your own, so it is limited to administrators. Use the web interface, "
    "which is per user."
)


async def gate(request, call_next):
    """Refuse anything that is not public and has no identity behind it.

    Installed as middleware so the `/mcp` mount is covered too. The identity
    is stashed on the request, so the endpoints below read it without a
    second trip to the database.
    """
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

    # MCP is administrators-only, and this is a limitation rather than a
    # policy. Its tool handlers run in tasks the session manager starts from
    # the *lifespan* task group, not from the request - so the identity above
    # cannot reach them, and every tool call lands in the shared system
    # session no matter who authenticated. Serving that to an ordinary user
    # would quietly answer their questions from somebody else's open app and
    # let them rewrite that app's load script. Refusing is the honest
    # version until the tools take a session explicitly.
    if identity is not None and request.url.path.startswith("/mcp"):
        if not identity.user.get("is_admin"):
            return JSONResponse({"detail": MCP_IS_SHARED}, status_code=403)

    return await call_next(request)


# ----------------------------------------------------------------------
# Dependencies
# ----------------------------------------------------------------------

def require_user(request: Request) -> Identity:
    """The Identity, guaranteed. The gate above has already refused None."""
    identity = getattr(request.state, "identity", None)
    if identity is None:
        raise HTTPException(401, "Sign in to continue.")
    return identity


def require_admin(request: Request) -> Identity:
    """An administrator, acting as themselves.

    An admin standing in for someone else is deliberately refused here. They
    are reproducing that person's experience, and that person cannot manage
    users - a borrowed identity that quietly kept its own powers would be a
    trap rather than a feature.
    """
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
    """The Session this request works in.

    Resolved from the request rather than from an ambient variable on
    purpose: the alternative is a thread- or task-local that, when it is
    wrong, silently runs one person's request inside another person's Qlik
    identity.
    """
    identity = require_user(request)
    person = identity.effective
    if person.get("id") is None:
        return session.system()
    return session.for_user(person)


# ----------------------------------------------------------------------
# Logging in and out
# ----------------------------------------------------------------------

def sign_in(username, password, ip=""):
    """Check a password wherever that person's password actually lives.

    Returns (user, reason). `reason` is None on success, and otherwise one of
    "credentials", "locked" or "directory" - three different things that a
    single "login failed" would hide:

    - **credentials** is the person's fault and they should try again.
    - **locked** means stop trying; another attempt cannot succeed.
    - **directory** means the domain controller could not be reached, and
      telling somebody their password is wrong when a DC is down sends them
      to reset a password that was never the problem.

    The order matters more than it looks:

    1. **The lockout is checked first, before anything reaches AD.** Somebody
       hammering this login form must lock the account *here*, not lock the
       person's Windows account across the whole bank. Our rate limit is
       protecting their domain account, not just this app.
    2. **An existing account decides where its password is checked.** An
       account is local or directory and never both, so a directory user
       cannot be signed in with a local password somebody set, and the
       break-glass administrator keeps working when AD is unreachable.
    3. **An unknown username goes to the directory**, if one is configured,
       and the account creates itself from what AD returns.
    """
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
        # The account was made by the directory and the directory is now
        # switched off. Refusing is the honest answer: there is no local
        # password to fall back to, and inventing one would be a way in that
        # AD never authorised.
        log.warning("%s is a directory account but LDAP_ENABLED is off", name)
        return None, "directory"

    try:
        entry = directory.authenticate(name, password)
    except directory.DirectoryError as e:
        log.error("Directory sign-in failed for %s: %s", name, e)
        return None, "directory"

    if entry is None:
        # Counted here because users.authenticate - which normally does the
        # counting - was never reached.
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
