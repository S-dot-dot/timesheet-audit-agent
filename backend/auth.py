"""
auth.py
-------
Minimal shared-passcode gate for the timesheet audit app.

Anyone who knows the passcode (set once, shared with Finish Line staff)
can log in; everyone else is redirected to a login screen. This is
intentionally simple — one shared secret, no per-user accounts — good
enough for an internal tool that isn't meant to be publicly discoverable.

Setup:
1. On Render, add an environment variable:
     ACCESS_PASSCODE = <pick a passcode and share it with your team>
2. Drop this file in backend/ alongside main.py.
3. Wire it into main.py (see the 4 additions in DEPLOY_NOTES below / chat).
"""
from __future__ import annotations

import hmac
import os
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

SESSION_COOKIE = "fl_session"
ACCESS_PASSCODE = os.environ.get("ACCESS_PASSCODE", "")

# In-memory session store. Good enough for a single Render instance/worker.
# If you ever scale to multiple workers/instances, swap this for a signed
# cookie (e.g. itsdangerous) or a shared store (Redis) instead.
_valid_tokens: set[str] = set()


def check_passcode(passcode: str) -> bool:
    """Constant-time compare so we're not leaking timing info about the passcode."""
    if not ACCESS_PASSCODE:
        # No passcode configured on the server — fail closed, not open.
        return False
    return hmac.compare_digest(passcode or "", ACCESS_PASSCODE)


def issue_token() -> str:
    token = secrets.token_urlsafe(32)
    _valid_tokens.add(token)
    return token


def is_valid(token: str | None) -> bool:
    return token is not None and token in _valid_tokens


def revoke(token: str | None) -> None:
    if token:
        _valid_tokens.discard(token)


class AuthMiddleware(BaseHTTPMiddleware):
    """Blocks every request except the login page/endpoint and health checks
    unless the caller has a valid session cookie."""

    PUBLIC_PATHS = {"/login", "/api/login", "/api/health", "/tailwind.css"}

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if path in self.PUBLIC_PATHS or path.startswith("/static") or path.startswith("/assets"):
            return await call_next(request)

        token = request.cookies.get(SESSION_COOKIE)
        if not is_valid(token):
            if path.startswith("/api"):
                return JSONResponse({"detail": "Unauthorized. Please log in."}, status_code=401)
            return RedirectResponse(url="/login")

        return await call_next(request)
