"""A2A Server Entry Point wrapping the ADK 2.0 Service Request Agent."""

from __future__ import annotations

import json
import os
from urllib.parse import urlparse

from dotenv import load_dotenv
from google.adk.a2a.utils.agent_to_a2a import to_a2a
from google.adk.artifacts import InMemoryArtifactService
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
import uvicorn

from agent import root_agent

load_dotenv()

PORT = int(os.environ.get("PORT", "8080"))
RAW_CLOUD_RUN_URL = os.environ.get("CLOUD_RUN_URL", "").strip()
A2A_API_KEY = os.environ.get("A2A_API_KEY", "").strip()

# Public discovery paths that never require API key authentication
PUBLIC_PATHS = {
    "/health",
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


class A2ACompatAndAuthMiddleware(BaseHTTPMiddleware):
    """Handles legacy agent.json discovery, clean HTTPS URLs, and optional API key auth."""

    async def dispatch(self, request: Request, call_next):
        # 1. Alias legacy /.well-known/agent.json to /.well-known/agent-card.json
        if request.url.path == "/.well-known/agent.json":
            request.scope["path"] = "/.well-known/agent-card.json"

        # 2. Enforce optional X-API-KEY or Bearer token auth on non-public endpoints
        if A2A_API_KEY and request.url.path not in PUBLIC_PATHS:
            api_key_header = request.headers.get("X-API-KEY") or request.headers.get(
                "x-api-key"
            )
            auth_header = request.headers.get("Authorization", "")
            bearer_token = (
                auth_header.removeprefix("Bearer ").strip()
                if auth_header.startswith("Bearer ")
                else ""
            )

            if api_key_header != A2A_API_KEY and bearer_token != A2A_API_KEY:
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": "Unauthorized",
                        "message": (
                            "Valid X-API-KEY or Authorization: Bearer token required."
                        ),
                    },
                )

        response = await call_next(request)

        # 3. Strip redundant ':443' port from advertised HTTPS URLs in Agent Card JSON
        if (
            request.url.path == "/.well-known/agent-card.json"
            and response.status_code == 200
        ):
            body_chunks = [chunk async for chunk in response.body_iterator]
            raw_body = b"".join(body_chunks).decode("utf-8")
            cleaned_body = raw_body.replace(":443/", "/").replace(":443\"", "\"")
            try:
                card_data = json.loads(cleaned_body)
                return JSONResponse(content=card_data, status_code=200)
            except Exception:
                return Response(
                    content=cleaned_body,
                    status_code=200,
                    media_type="application/json",
                )

        return response


async def health_check(request: Request) -> JSONResponse:
    """Cloud Run health and readiness probe endpoint."""
    return JSONResponse(
        {
            "status": "ok",
            "agent": root_agent.name,
            "model": str(root_agent.model),
            "auth_enabled": bool(A2A_API_KEY),
        }
    )


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
app.add_middleware(A2ACompatAndAuthMiddleware)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
