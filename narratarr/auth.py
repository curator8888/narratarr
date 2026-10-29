"""Who is making a request, and what they're allowed to do.

Signing in is required. The exceptions are /api/health, the sign-in endpoints themselves,
and, when an admin turns it on, requests from this machine or the home network, which act
as the first admin. Requests that came through a reverse proxy never count as local,
because the proxy hides where they really came from.

Before anyone has an account, the first visitor creates the admin. From this machine or
the home network that's all it takes; from anywhere else it also needs the setup code
printed in the log, so a stranger who finds a fresh install can't claim it.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import secrets
import time

from . import config, users

log = logging.getLogger("narratarr.auth")

COOKIE = "narratarr_session"
PROXY_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded", "cf-connecting-ip", "true-client-ip")
# Paths anyone may use. Everything else under /api/ needs a signed-in user.
OPEN = {"/api/health", "/api/auth/status", "/api/auth/login", "/api/auth/setup"}
# Paths only admins may use (any method), and admin-only method/path pairs.
ADMIN_PREFIXES = ("/api/users", "/api/settings/", "/api/audiobookshelf", "/api/import", "/api/updates/apply")
LOGIN_TRIES = 8  # per address, every LOGIN_WINDOW seconds
LOGIN_WINDOW = 10 * 60

_failures: dict[str, list[float]] = {}
_setup_code = ""


def is_local(request) -> bool:
    """This machine or the home network, reached directly rather than through a proxy."""
    if any(h in request.headers for h in PROXY_HEADERS):
        return False
    host = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_private or ip.is_link_local


def current_user(request) -> dict | None:
    user = users.session_user(request.cookies.get(COOKIE, ""))
    if user:
        return user
    if config.load().auth_local_bypass and is_local(request):
        return users.owner()
    return None


def admin_only(method: str, path: str) -> bool:
    if path.startswith(ADMIN_PREFIXES) or (path == "/api/settings" and method != "GET"):
        return True
    return method == "DELETE" and path.startswith("/api/library/")


# ---- first run --------------------------------------------------------------------------


def setup_code() -> str:
    """The one-time code that lets someone create the first admin from outside the home network."""
    global _setup_code
    if not _setup_code:
        _setup_code = f"{secrets.randbelow(10**4):04d}-{secrets.randbelow(10**4):04d}"
    return _setup_code


def adopt_env_password() -> None:
    """Before 0.5.5 there was one shared password ($NARRATARR_PASSWORD). It becomes the admin account."""
    password = os.environ.get("NARRATARR_PASSWORD", "")
    if not password or users.count():
        return
    try:
        users.create("admin", password, "admin")
    except users.UserError as e:
        log.warning("Couldn't turn NARRATARR_PASSWORD into an account (%s); create one in the browser instead.", e)
        return
    log.warning("Created the admin account 'admin' with your NARRATARR_PASSWORD. Sign in with that, then unset it.")


def announce_setup() -> None:
    if users.count():
        return
    message = (f"No accounts yet. Open Narratarr to create the admin account. From another network "
               f"you'll be asked for this setup code: {setup_code()}  (or run: narratarr users add <name>)")
    print(f"\n  {message}\n", flush=True)
    log.warning(message)


# ---- sign-in attempts -----------------------------------------------------------------------


def too_many_tries(request) -> bool:
    key = request.client.host if request.client else "?"
    now = time.time()
    recent = [t for t in _failures.get(key, []) if now - t < LOGIN_WINDOW]
    _failures[key] = recent
    return len(recent) >= LOGIN_TRIES


def failed_try(request) -> None:
    if len(_failures) > 10_000:  # bounded, however many addresses try
        _failures.clear()
    _failures.setdefault(request.client.host if request.client else "?", []).append(time.time())


def cookie_args(request) -> dict:
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https"
    return {"key": COOKIE, "httponly": True, "samesite": "lax", "secure": secure, "path": "/"}
