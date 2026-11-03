"""Node agent API under ``/agent/v1`` (not ``/api``): register, renew, desired, report.

Trust model (details in :mod:`proxyhub.agent_api`): every request must carry the Caddy-to-API
shared secret; ``desired``, ``report`` and ``renew`` additionally need the fingerprint of a client
certificate that Caddy verified and that we issued (unrevoked, in validity). ``register`` is
authenticated by a one-time token instead, because the node has no certificate yet.

Responses never contain a token, key or secret except the desired state itself, which carries the
node's own Telemt secrets to that node's authenticated agent only (``Cache-Control: no-store``).
Error bodies are short codes; request input is never echoed back.
"""

from __future__ import annotations

import hmac
import json
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import Counter
from pydantic import BaseModel, ValidationError
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from proxyhub.agent_api.desired import build_desired_state, canonical_json, etag_for
from proxyhub.agent_api.identity import (
    AgentIdentity,
    AgentRefused,
    audit,
    authenticate,
    normalize_fingerprint,
    register_node,
    renew_certificate,
)
from proxyhub.agent_api.reports import ReportRejected, parse_report, store_report
from proxyhub.agent_api.runtime import AgentRuntime, get_agent_runtime
from proxyhub.agent_api.schemas import RegisterRequest, RenewRequest
from proxyhub.api.deps import db_session
from proxyhub.db.models_agent import AgentNode

PROXY_SECRET_HEADER = "X-ProxyHub-Agent-Proxy-Secret"  # noqa: S105 - a header name, not a secret
CLIENT_FINGERPRINT_HEADER = "X-ProxyHub-Client-Cert-Sha256"
MAX_BODY_BYTES = 512 * 1024
NO_STORE = {"Cache-Control": "no-store"}

REQUESTS = Counter(
    "proxyhub_agent_api_requests", "Agent API requests by endpoint and outcome", ["endpoint", "outcome"]
)

router = APIRouter(prefix="/agent/v1", include_in_schema=False)
Session = Annotated[AsyncSession, Depends(db_session)]


async def require_proxy(
    request: Request, runtime: Annotated[AgentRuntime, Depends(get_agent_runtime)]
) -> AgentRuntime:
    """The request came through the agent Caddy site (constant-time shared-secret check)."""
    supplied = request.headers.get(PROXY_SECRET_HEADER, "").encode("utf-8", "replace")
    if not hmac.compare_digest(supplied, runtime.proxy_secret):
        REQUESTS.labels("any", "no_proxy_secret").inc()
        raise HTTPException(status_code=403, detail="forbidden")
    return runtime


Runtime = Annotated[AgentRuntime, Depends(require_proxy)]


async def require_agent(request: Request, runtime: Runtime, session: Session) -> AgentIdentity:
    """The client certificate Caddy verified is one we issued, unrevoked and valid now."""
    fingerprint = normalize_fingerprint(request.headers.get(CLIENT_FINGERPRINT_HEADER, ""))
    identity = await authenticate(session, fingerprint, datetime.now(UTC)) if fingerprint else None
    if identity is None:
        REQUESTS.labels("any", "no_identity").inc()
        raise HTTPException(status_code=401, detail="unauthenticated")
    await session.commit()
    return identity


Identity = Annotated[AgentIdentity, Depends(require_agent)]


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status, headers=NO_STORE)


async def _read_json(request: Request) -> Any:
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY_BYTES):
        raise HTTPException(status_code=413, detail="too_large")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="too_large")
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="invalid_json") from None


def _validate[M: BaseModel](model: type[M], data: Any) -> M:
    try:
        return model.model_validate(data)
    except ValidationError:
        raise HTTPException(status_code=422, detail="schema") from None


@router.post("/register")
async def register(request: Request, runtime: Runtime, session: Session) -> Response:
    req = _validate(RegisterRequest, await _read_json(request))
    try:
        bundle = await register_node(
            session,
            runtime,
            token=req.token,
            csr_pem=req.csr_pem,
            agent_version=req.agent_version,
            telemt_version=req.telemt_version,
            now=datetime.now(UTC),
        )
        await session.commit()
    except AgentRefused as exc:
        await session.rollback()  # also leaves the token unused when only the CSR was wrong
        REQUESTS.labels("register", exc.code).inc()
        if exc.node_id is not None:  # a valid token: worth a dashboard event; random noise is not
            await audit(session, "agent_register", str(exc.node_id), False, {"code": exc.code})
            await session.commit()
        return _error(exc.status, exc.code)
    REQUESTS.labels("register", "ok").inc()
    return JSONResponse({"cert_pem": bundle.cert_pem, "ca_pem": bundle.ca_pem}, headers=NO_STORE)


@router.post("/renew")
async def renew(request: Request, identity: Identity, runtime: Runtime, session: Session) -> Response:
    req = _validate(RenewRequest, await _read_json(request))
    try:
        bundle = await renew_certificate(session, runtime, identity, req.csr_pem, datetime.now(UTC))
        await session.commit()
    except AgentRefused as exc:
        await session.rollback()
        REQUESTS.labels("renew", exc.code).inc()
        await audit(session, "agent_renew", str(identity.node_id), False, {"code": exc.code})
        await session.commit()
        return _error(exc.status, exc.code)
    REQUESTS.labels("renew", "ok").inc()
    return JSONResponse({"cert_pem": bundle.cert_pem, "ca_pem": bundle.ca_pem}, headers=NO_STORE)


def _etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    candidates = {part.strip().removeprefix("W/") for part in header.split(",")}
    return etag in candidates or "*" in candidates


@router.get("/desired")
async def desired(request: Request, identity: Identity, runtime: Runtime, session: Session) -> Response:
    body = canonical_json(await build_desired_state(session, runtime.seal_key, identity.node_id))
    etag = etag_for(body)
    await session.execute(
        update(AgentNode).where(AgentNode.node_id == identity.node_id).values(last_served_etag=etag)
    )
    await session.commit()
    headers = {"ETag": etag, **NO_STORE}
    if _etag_matches(request.headers.get("if-none-match"), etag):
        REQUESTS.labels("desired", "not_modified").inc()
        return Response(status_code=304, headers=headers)
    REQUESTS.labels("desired", "ok").inc()
    return Response(content=body, media_type="application/json", headers=headers)


@router.post("/report", status_code=204)
async def report(request: Request, identity: Identity, session: Session) -> Response:
    try:
        parsed = parse_report(await _read_json(request))
    except ReportRejected as exc:
        REQUESTS.labels("report", exc.code).inc()
        return _error(422, exc.code)
    await store_report(session, identity.node_id, parsed, datetime.now(UTC))
    await session.commit()
    REQUESTS.labels("report", "ok").inc()
    return Response(status_code=204)
