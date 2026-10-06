"""A2A Server Entry Point with Security, Monitoring, Auditing, and Live Command Center."""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
from typing import Any
from urllib.parse import urlparse
import uuid

from dotenv import load_dotenv
from google.adk.a2a.utils.agent_to_a2a import to_a2a
from google.adk.artifacts import InMemoryArtifactService
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
import uvicorn

from a2a_test_client import send_a2a_message
from agent import GCS_BUCKET_NAME, get_all_tickets_list, root_agent
from audit import current_caller_ctx, get_audit_summary, record_audit_event

load_dotenv()

PORT = int(os.environ.get("PORT", "8080"))
RAW_CLOUD_RUN_URL = os.environ.get("CLOUD_RUN_URL", "").strip()
# Default demo API key for verified callers; strict enforcement can be toggled via A2A_ENFORCE_STRICT_AUTH
A2A_API_KEY = os.environ.get("A2A_API_KEY", "oracle-ai-world-2026").strip()
A2A_ENFORCE_STRICT_AUTH = os.environ.get(
    "A2A_ENFORCE_STRICT_AUTH", "false"
).lower() in ("true", "1", "yes")

STATIC_INDEX_PATH = Path(__file__).parent / "static" / "index.html"

PUBLIC_PATHS = {
    "/health",
    "/console",
    "/api/audit",
    "/api/tickets",
    "/api/test-client/send",
    "/.well-known/agent.json",
    "/.well-known/agent-card.json",
}


def _parse_endpoint_config(raw_url: str, default_port: int) -> tuple[str, int, str]:
    """Extract (host, port, protocol) from CLOUD_RUN_URL or fall back to localhost."""
    if not raw_url:
        return "localhost", default_port, "http"

    if "://" not in raw_url:
        raw_url = f"https://{raw_url}" if "run.app" in raw_url else f"http://{raw_url}"

    parsed = urlparse(raw_url)
    protocol = parsed.scheme or ("https" if "run.app" in raw_url else "http")
    host = parsed.hostname or "localhost"
    if parsed.port:
        port = parsed.port
    else:
        port = 443 if protocol == "https" else default_port
    return host, port, protocol


def _extract_a2a_rpc_info(raw_body: bytes) -> tuple[str, str, str, str]:
    """Extract (method, request_id, prompt_preview, metadata_caller) from JSON-RPC body."""
    if not raw_body:
        return "unknown", "", "", ""
    try:
        data = json.loads(raw_body.decode("utf-8"))
        if not isinstance(data, dict):
            return "unknown", "", "", ""
        method = str(data.get("method") or "jsonrpc")
        req_id = str(data.get("id") or "")
        params = data.get("params") or {}
        if not isinstance(params, dict):
            return method, req_id, "", ""

        meta = params.get("metadata") or {}
        meta_caller = (
            str(meta.get("caller_id") or "") if isinstance(meta, dict) else ""
        )

        msg = params.get("message") or {}
        prompt_texts: list[str] = []
        if isinstance(msg, dict) and isinstance(msg.get("parts"), list):
            for part in msg["parts"]:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    prompt_texts.append(part["text"])
        prompt_preview = " ".join(prompt_texts).strip()[:140]
        return method, req_id, prompt_preview, meta_caller
    except Exception:
        return "unknown", "", "", ""


class A2ASecurityAndAuditMiddleware(BaseHTTPMiddleware):
    """Handles UI routing, A2A discovery normalization, security checks, and full request auditing."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # 1. Serve Interactive Command Center on GET / or GET /console
        if request.method == "GET" and path in {"/", "/console"}:
            if STATIC_INDEX_PATH.exists():
                return HTMLResponse(
                    STATIC_INDEX_PATH.read_text(encoding="utf-8"), status_code=200
                )

        # 2. Alias legacy /.well-known/agent.json to /.well-known/agent-card.json
        if path == "/.well-known/agent.json":
            request.scope["path"] = "/.well-known/agent-card.json"
            path = "/.well-known/agent-card.json"

        # 3. Strip redundant ':443' port and ensure dual A2A v0.3 + v1.0 card compatibility
        if path == "/.well-known/agent-card.json":
            response = await call_next(request)
            if response.status_code == 200:
                body_chunks = [chunk async for chunk in response.body_iterator]
                raw_body = b"".join(body_chunks).decode("utf-8")
                cleaned_body = raw_body.replace(":443/", "/").replace(':443"', '"')
                try:
                    card_data = json.loads(cleaned_body)
                    public_url = ""
                    if isinstance(card_data.get("supportedInterfaces"), list) and card_data["supportedInterfaces"]:
                        public_url = card_data["supportedInterfaces"][0].get("url", "").rstrip("/")
                        has_v03 = any(
                            i.get("protocolVersion") in ("0.3", "0.3.0")
                            for i in card_data["supportedInterfaces"]
                            if isinstance(i, dict)
                        )
                        if not has_v03 and public_url:
                            card_data["supportedInterfaces"].append(
                                {
                                    "url": public_url,
                                    "protocolBinding": "JSONRPC",
                                    "protocolVersion": "0.3",
                                }
                            )
                    if public_url:
                        card_data.setdefault("url", public_url)
                    card_data.setdefault("protocolVersion", "0.3.0")
                    card_data.setdefault("preferredTransport", "JSONRPC")
                    card_data["version"] = "1.0.0"
                    return JSONResponse(content=card_data, status_code=200)
                except Exception:
                    return Response(
                        content=cleaned_body,
                        status_code=200,
                        media_type="application/json",
                    )
            return response

        # Skip A2A RPC auditing for internal dashboard polling routes
        if path in PUBLIC_PATHS:
            return await call_next(request)

        # 4. Inspect inbound A2A JSON-RPC call (POST /)
        t0 = time.perf_counter()
        raw_body = await request.body()
        a2a_method, req_id, prompt_preview, meta_caller = _extract_a2a_rpc_info(raw_body)

        # Ensure A2A-Version header is set for v0.3 methods (e.g. message/send) if stripped by proxies
        existing_ver = request.headers.get("a2a-version") or request.headers.get("x-a2a-version")
        if not existing_ver and "/" in a2a_method:
            request.scope["headers"] = list(request.scope.get("headers", [])) + [
                (b"a2a-version", b"0.3")
            ]

        caller_ip = (
            request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or (request.client.host if request.client else "unknown")
        )
        caller_id = (
            request.headers.get("X-Caller-Id")
            or request.headers.get("X-Oracle-Agent")
            or meta_caller
            or "external-a2a-client"
        )
        caller_role = (
            request.headers.get("X-Caller-Role") or "External A2A Integration"
        )
        user_agent = request.headers.get("User-Agent", "unknown")
        trace_header = request.headers.get("X-Cloud-Trace-Context", "")
        trace_id = (
            trace_header.split("/")[0]
            if trace_header
            else f"tr-{uuid.uuid4().hex[:10]}"
        )

        # Evaluate authentication credentials
        api_key_header = (
            request.headers.get("X-API-KEY")
            or request.headers.get("x-api-key")
            or ""
        ).strip()
        auth_header = request.headers.get("Authorization", "")
        bearer_token = (
            auth_header.removeprefix("Bearer ").strip()
            if auth_header.startswith("Bearer ")
            else ""
        )
        simulate_unauth = (
            request.headers.get("X-Simulate-Unauthorized", "").lower() == "true"
        )

        has_provided_credential = bool(api_key_header or bearer_token)
        credential_matches = bool(
            A2A_API_KEY
            and (api_key_header == A2A_API_KEY or bearer_token == A2A_API_KEY)
        )

        if simulate_unauth or (has_provided_credential and not credential_matches) or (
            A2A_ENFORCE_STRICT_AUTH and not credential_matches
        ):
            auth_status = "REJECTED_INVALID_KEY"
            ctx_token = current_caller_ctx.set(
                {
                    "trace_id": trace_id,
                    "caller_id": caller_id,
                    "caller_role": caller_role,
                    "caller_ip": caller_ip,
                    "user_agent": user_agent,
                    "auth_status": auth_status,
                    "a2a_method": a2a_method,
                    "a2a_request_id": req_id,
                    "prompt_preview": prompt_preview,
                    "tools_invoked": [],
                }
            )
            try:
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                record_audit_event(
                    event_type="SECURITY_BLOCK",
                    status="DENIED",
                    action=f"Blocked unauthorized A2A {a2a_method} request",
                    outcome="HTTP 401 Unauthorized — Invalid or missing X-API-KEY / Bearer token",
                    latency_ms=elapsed_ms,
                )
            finally:
                current_caller_ctx.reset(ctx_token)

            return JSONResponse(
                status_code=401,
                content={
                    "error": "Unauthorized",
                    "message": (
                        f"Access denied for caller '{caller_id}'. "
                        "A valid X-API-KEY or Authorization: Bearer token is required."
                    ),
                },
            )

        auth_status = (
            "VERIFIED_API_KEY" if credential_matches else "PUBLIC_ALLOWED"
        )
        ctx_dict: dict[str, Any] = {
            "trace_id": trace_id,
            "caller_id": caller_id,
            "caller_role": caller_role,
            "caller_ip": caller_ip,
            "user_agent": user_agent,
            "auth_status": auth_status,
            "a2a_method": a2a_method,
            "a2a_request_id": req_id,
            "prompt_preview": prompt_preview,
            "tools_invoked": [],
        }
        ctx_token = current_caller_ctx.set(ctx_dict)
        try:
            response = await call_next(request)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            tools_used = ctx_dict.get("tools_invoked") or []
            tools_summary = (
                f"Tools executed: {', '.join(tools_used)}"
                if tools_used
                else "Direct LLM response (no tool call)"
            )
            record_audit_event(
                event_type="A2A_REQUEST",
                status="SUCCESS" if response.status_code < 400 else "ERROR",
                action=f"A2A {a2a_method}",
                outcome=f"HTTP {response.status_code} | {tools_summary}",
                latency_ms=elapsed_ms,
            )
            return response
        finally:
            current_caller_ctx.reset(ctx_token)


async def health_check(request: Request) -> JSONResponse:
    """Cloud Run health and readiness probe endpoint."""
    return JSONResponse(
        {
            "status": "ok",
            "agent": root_agent.name,
            "model": str(root_agent.model),
            "gcs_bucket": GCS_BUCKET_NAME,
            "strict_auth_enforced": A2A_ENFORCE_STRICT_AUTH,
        }
    )


async def api_get_audit(request: Request) -> JSONResponse:
    """Return live security, monitoring, and action audit summary."""
    return JSONResponse(get_audit_summary(limit=60))


async def api_get_tickets(request: Request) -> JSONResponse:
    """Return all trouble tickets from GCS + local fallback."""
    tickets = get_all_tickets_list(limit=50)
    return JSONResponse({"tickets": tickets, "count": len(tickets)})


async def api_test_client_send(request: Request) -> JSONResponse:
    """Execute a full-loop A2A JSON-RPC test call via the A2A Test Client."""
    body = await request.json()
    prompt = str(body.get("prompt") or "").strip()
    caller_id = str(body.get("caller_id") or "oracle-ai-studio-agent").strip()
    caller_role = str(body.get("caller_role") or "Oracle AI Studio Agent").strip()
    auth_mode = str(body.get("auth_mode") or "verified").strip()

    api_key: str | None = None
    simulate_unauthorized = False
    if auth_mode == "verified":
        api_key = A2A_API_KEY
    elif auth_mode == "invalid":
        simulate_unauthorized = True

    local_base_url = f"http://127.0.0.1:{PORT}"
    result = await send_a2a_message(
        base_url=local_base_url,
        prompt=prompt,
        caller_id=caller_id,
        caller_role=caller_role,
        api_key=api_key,
        simulate_unauthorized=simulate_unauthorized,
    )
    return JSONResponse(result)


host_domain, advertised_port, protocol = _parse_endpoint_config(RAW_CLOUD_RUN_URL, PORT)

runner = Runner(
    agent=root_agent,
    app_name="ServiceRequestA2AAgent",
    session_service=InMemorySessionService(),
    artifact_service=InMemoryArtifactService(),
)

# Wraps ADK agent into an A2A protocol ASGI application
app = to_a2a(
    agent=root_agent,
    host=host_domain,
    port=advertised_port,
    protocol=protocol,
    runner=runner,
)

app.add_route("/health", health_check, methods=["GET"])
app.add_route("/api/audit", api_get_audit, methods=["GET"])
app.add_route("/api/tickets", api_get_tickets, methods=["GET"])
app.add_route("/api/test-client/send", api_test_client_send, methods=["POST"])
app.add_middleware(A2ASecurityAndAuditMiddleware)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
