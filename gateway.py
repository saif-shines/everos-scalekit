"""EverOS × Scalekit — identity-scoped memory, in one file.

EverOS scopes memory by ``user_id`` / ``app_id`` / ``project_id``, and takes all
three from the request body. That is deliberate: it binds to loopback, ships no
authentication, and its docs say *"place your own gateway or auth layer in
front."* This is that layer.

Scalekit validates the bearer token. The scope is then derived from the token's
claims and **overwrites whatever the caller asked for**::

    # Bob's token, asking for Alice's memory
    curl -H "Authorization: Bearer $BOB_TOKEN" \
         -H "Content-Type: application/json" \
         localhost:8080/api/v2/memory/search \
         -d '{"user_id": "alice", "query": "allergies"}'

    → {"episodes": []}   # scope was rewritten to Bob before EverOS saw it

Run it::

    everos server start                        # EverOS on :8000
    uvicorn gateway:app --port 8080            # this file in front of it

    # or in front of EverOS Cloud:
    #   EVEROS_URL=https://api.evermind.ai EVEROS_API_KEY=<key> uvicorn ...

See ``demo.py`` for the whole story end to end.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

EVEROS_URL = os.environ.get("EVEROS_URL", "http://127.0.0.1:8000")

# Self-hosted EverOS has no auth (this gateway IS its auth layer). EverOS Cloud
# (https://api.evermind.ai) wants `Authorization: Bearer <api_key>` — supply it
# via env and the gateway attaches it upstream. The Scalekit token in the
# *inbound* Authorization header and this *outbound* key never mix.
EVEROS_API_KEY = os.environ.get("EVEROS_API_KEY")

# Mock mode stubs *only* the identity provider, so the demo runs without a
# Scalekit account. Everything downstream is real: the scope derivation below,
# EverOS's extraction, the markdown on disk, the recall. Opt-in and loud —
# never let this flip on by accident in front of a real EverOS.
MOCK = os.environ.get("SCALEKIT_MOCK") == "1"

# Fallback folder when the token has no project claim. ``SCALEKIT_PROJECT_CLAIM``
# is the *name* of the claim to read (default ``memory_project``), not the value.
PROJECT_ID = os.environ.get("EVEROS_PROJECT_ID", "agent")
PROJECT_CLAIM = os.environ.get("SCALEKIT_PROJECT_CLAIM", "memory_project")

app = FastAPI(title="everos-scalekit-gateway")

if MOCK:
    print(
        "\n  !!  SCALEKIT_MOCK=1 — tokens are NOT verified. Local demo only.\n",
        flush=True,
    )

    def claims_for(token: str) -> Mapping[str, Any]:
        """Decode a fake token of the form ``mock.<sub>.<oid>.<project_id>``."""
        parts = token.split(".")
        if len(parts) != 4 or parts[0] != "mock":
            raise ValueError(f"not a mock token: {token!r}")
        return {"sub": parts[1], "oid": parts[2], "memory_project": parts[3]}

else:
    from scalekit.common.scalekit import TokenValidationOptions

    from scalekit import ScalekitClient

    SK_ENV_URL = os.environ["SCALEKIT_ENV_URL"]
    scalekit = ScalekitClient(
        env_url=SK_ENV_URL,
        client_id=os.environ["SCALEKIT_CLIENT_ID"],
        client_secret=os.environ["SCALEKIT_CLIENT_SECRET"],
    )

    def claims_for(token: str) -> Mapping[str, Any]:
        """Verify signature, expiry and issuer, then return the claims."""
        return scalekit.validate_access_token_and_get_claims(
            token=token,
            options=TokenValidationOptions(issuer=SK_ENV_URL),
        )


def scope_from_claims(claims: Mapping[str, Any]) -> dict[str, str]:
    """Scalekit's identity claims *are* EverOS's scope.

    ``sub`` and ``oid`` map 1:1. ``project_id`` does not: a user JWT's
    ``client_id`` is the Scalekit application (``skc_…``) and is the same
    for every user, so it is not an EverOS folder. Read a custom claim
    (default ``memory_project``) and fall back to ``EVEROS_PROJECT_ID``.

    Both id formats already satisfy EverOS's ScopeId charset
    (``^[a-zA-Z0-9_.-]+$``, 1-128 chars), so no sanitisation is needed.
    EverOS guarantees a query never crosses ``(app_id, project_id)``, so
    mapping the Scalekit organization onto ``app_id`` buys tenant isolation
    with no enforcement code.
    """
    return {
        "user_id": claims["sub"],  # usr_... — who is asking
        "app_id": claims["oid"],  # org_... — which tenant they belong to
        "project_id": str(claims.get(PROJECT_CLAIM) or PROJECT_ID),
    }


def apply_scope(action: str, body: dict[str, Any], scope: Mapping[str, str]) -> None:
    """Force the verified scope onto the body, whatever the caller sent.

    The two EverOS request shapes carry identity differently: ``/search`` and
    ``/get`` name the owner at the top level, while ``/add`` carries it per
    message as ``sender_id``. Both are overwritten.
    """
    body["app_id"] = scope["app_id"]
    body["project_id"] = scope["project_id"]

    if action in {"search", "get"}:
        body["user_id"] = scope["user_id"]
        body.pop("agent_id", None)  # owner is user XOR agent; identity picks user
    elif action == "add":
        for message in body.get("messages", []):
            if message.get("role") == "user":
                message["sender_id"] = scope["user_id"]
    # /flush carries no owner — session_id plus the scope above is the whole key.


@app.post("/api/v2/memory/{action}")
async def proxy(action: str, request: Request) -> JSONResponse:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")

    try:
        claims = claims_for(auth.removeprefix("Bearer ").strip())
    except Exception as exc:
        # Any validation failure — bad signature, expired, wrong issuer — is a 401.
        raise HTTPException(401, "invalid token") from exc

    scope = scope_from_claims(claims)
    body = await request.json()
    asked_for = body.get("user_id")
    apply_scope(action, body, scope)

    upstream_headers = (
        {"Authorization": f"Bearer {EVEROS_API_KEY}"} if EVEROS_API_KEY else {}
    )
    async with httpx.AsyncClient(timeout=180.0) as client:
        upstream = await client.post(
            f"{EVEROS_URL}/api/v2/memory/{action}",
            json=body,
            headers=upstream_headers,
        )

    payload = upstream.json()
    # Make the rewrite observable — this is the whole point of the layer.
    if isinstance(payload, dict):
        payload["_scope"] = {**scope, "asked_for_user_id": asked_for}
    return JSONResponse(payload, status_code=upstream.status_code)
