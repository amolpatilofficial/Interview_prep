"""HTTP Basic auth for the console.

This app holds a resume, contact details, and the ability to submit
applications in someone's name. On anything reachable from the internet a
password is mandatory — the middleware refuses to serve at all if the request
did not arrive over loopback and no password is configured, rather than
silently exposing the UI.

Basic auth is enough here because the browser attaches the cached credential to
every same-origin request, including the `EventSource` stream, which cannot
carry a custom header of its own.
"""
from __future__ import annotations

import base64
import binascii
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .settings import settings

LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}
OPEN_PATHS = {"/healthz"}

CHALLENGE = {"WWW-Authenticate": 'Basic realm="Job Application Agent", charset="UTF-8"'}


def _is_local(request: Request) -> bool:
    client = request.client
    return bool(client) and client.host in LOOPBACK


def _credentials_ok(header: str | None) -> bool:
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, IndexError):
        return False
    user, _, password = decoded.partition(":")
    # compare_digest on both halves so neither is short-circuited
    user_ok = secrets.compare_digest(user, settings.auth_user)
    pass_ok = secrets.compare_digest(password, settings.auth_password or "")
    return user_ok and pass_ok


class BasicAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in OPEN_PATHS:
            return await call_next(request)

        if settings.auth_password is None:
            if _is_local(request):
                return await call_next(request)
            return JSONResponse(
                {
                    "detail": (
                        "Refusing to serve a remote request with no password set. "
                        "Set JAA_AUTH_PASSWORD in the environment and restart."
                    )
                },
                status_code=503,
            )

        if _credentials_ok(request.headers.get("authorization")):
            return await call_next(request)

        return Response(status_code=401, headers=CHALLENGE, content="Authentication required")
