"""``BearerTokenMiddleware``: the single auth choke point for the HTTP surface."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from src.auth import api_tokens
from src.auth.api_tokens import constant_time_equals
from src.middleware import trust
from src.middleware.credentials import credential_required, presented_credential
from src.middleware.routes import (
    AUTH_EXEMPT_EXACT,
    AUTH_EXEMPT_PREFIXES,
    is_webhook_hook_path,
)
from src.middleware.terminal_gate import terminal_http_gate


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Require Authorization: Bearer <token> on API endpoints (non-loopback only).

    Two credential classes: the legacy ``auth_token`` (full
    access, unchanged) and minted ``api_tokens`` records — full-scope
    (``"*"``) tokens behave like the legacy one; job-scoped tokens may
    only call ``POST /api/jobs/<id>/run`` on their allowed jobs. A
    matched minted token's identity is surfaced on ``request.state``
    (``token_id`` / ``token_label``) for run-record provenance.
    """

    def __init__(self, app, get_config):
        super().__init__(app)
        self._get_config = get_config

    async def dispatch(self, request: Request, call_next):
        client_host = request.client.host if request.client else ""
        is_loopback = trust.is_pc_itself(client_host, request.headers)
        path = request.url.path
        cfg = self._get_config()

        # Terminal endpoints are Tailscale-only (+ passkey for the
        # interactive ones). Enforced even when no bearer token is
        # configured. The PC itself (loopback) is trusted and skips it.
        if not is_loopback:
            gate_err = terminal_http_gate(request)
            if gate_err is not None:
                return gate_err

        if is_webhook_hook_path(path):
            return await call_next(request)

        token = (getattr(cfg, "auth_token", "") or "").strip()
        minted = getattr(cfg, "api_tokens", None) or []
        # Auth is enforced when EITHER credential class is configured —
        # a config with only minted tokens must not be an open gate.
        # The accept rule below is the reporting form of
        # `credential_accepted`, which the WS gate uses; changing one means
        # changing both.
        if not credential_required(cfg) or is_loopback:
            return await call_next(request)

        if path in AUTH_EXEMPT_EXACT or any(
            path.startswith(p) for p in AUTH_EXEMPT_PREFIXES
        ):
            return await call_next(request)

        presented = presented_credential(request)
        if presented and token and constant_time_equals(presented, token):
            return await call_next(request)

        match = api_tokens.find_match(presented, minted)
        if match is not None:
            rejection = api_tokens.scope_rejection(match, request.method, path)
            if rejection is not None:
                return JSONResponse(status_code=403, content={"detail": rejection})
            request.state.token_id = match.id
            request.state.token_label = match.label
            # In-memory freshness stamp; persisted opportunistically on
            # the next mint/revoke save (see src.auth.api_tokens).
            api_tokens.touch_last_used(minted, match.id)
            return await call_next(request)

        return JSONResponse(
            status_code=401,
            content={"detail": "missing or invalid bearer token"},
            headers={"WWW-Authenticate": 'Bearer realm="launcher"'},
        )
