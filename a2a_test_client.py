"""A2A Protocol Test Client & Multi-Persona Simulation Agent.

Can be used:
1. Programmatically by the interactive Command Center (`/api/test-client/send`)
2. Directly from the terminal (`python a2a_test_client.py --url https://...`) to test
   discovery, ticket creation, ticket status lookup, resolution, and security auditing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from typing import Any
import uuid

import httpx


def extract_text_from_a2a_response(rpc_response: dict[str, Any]) -> str:
    """Extract human-readable agent reply text from an A2A v0.3 or v1.0 JSON-RPC response."""
    if "error" in rpc_response:
        err = rpc_response["error"]
        if isinstance(err, dict):
            return f"A2A RPC Error ({err.get('code')}): {err.get('message')}"
        return f"A2A Error: {err}"

    raw_result = rpc_response.get("result", {})
    if not isinstance(raw_result, dict):
        return str(raw_result)

    # Unwrap v1.0 SendMessageResponse wrapper ({task: ...} or {message: ...}) if present
    result = (
        raw_result.get("task")
        or raw_result.get("message")
        or raw_result
    )
    if not isinstance(result, dict):
        return str(result)

    texts: list[str] = []

    def _collect_parts(parts: list[Any]) -> None:
        for p in parts:
            if not isinstance(p, dict):
                continue
            if "text" in p and isinstance(p["text"], str):
                texts.append(p["text"])
            elif "root" in p and isinstance(p["root"], dict):
                if "text" in p["root"]:
                    texts.append(str(p["root"]["text"]))

    # 1. Task artifacts (primary output of ADK A2A Executor)
    if isinstance(result.get("artifacts"), list):
        for artifact in result["artifacts"]:
            if isinstance(artifact, dict) and isinstance(artifact.get("parts"), list):
                _collect_parts(artifact["parts"])

    # 2. Task status message (if agent responded in task status)
    status = result.get("status")
    if isinstance(status, dict) and isinstance(status.get("message"), dict):
        msg = status["message"]
        if msg.get("role") in {"agent", "ROLE_AGENT", 2, None} and isinstance(
            msg.get("parts"), list
        ):
            _collect_parts(msg["parts"])

    # 3. Direct Message response
    if not texts and isinstance(result.get("parts"), list):
        _collect_parts(result["parts"])

    # 4. Task history (take last agent message if nothing else matched)
    if not texts and isinstance(result.get("history"), list):
        for hist_msg in reversed(result["history"]):
            if isinstance(hist_msg, dict) and hist_msg.get("role") in {
                "agent",
                "ROLE_AGENT",
                2,
            }:
                if isinstance(hist_msg.get("parts"), list):
                    _collect_parts(hist_msg["parts"])
                if texts:
                    break

    cleaned = list(dict.fromkeys(t.strip() for t in texts if t.strip()))
    return "\n".join(cleaned) if cleaned else json.dumps(raw_result)


async def send_a2a_message(
    *,
    base_url: str,
    prompt: str,
    caller_id: str = "oracle-ai-studio-agent",
    caller_role: str = "Oracle AI Studio Participant",
    api_key: str | None = None,
    simulate_unauthorized: bool = False,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Send a standards-compliant A2A JSON-RPC message/send request to the A2A server."""
    target_url = base_url.rstrip("/") + "/"
    request_id = f"req-{uuid.uuid4().hex[:8]}"
    message_id = f"msg-{uuid.uuid4().hex[:8]}"

    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "message/send",
        "params": {
            "message": {
                "messageId": message_id,
                "role": "user",
                "parts": [{"kind": "text", "text": prompt}],
            },
            "metadata": {
                "caller_id": caller_id,
                "caller_role": caller_role,
            },
        },
    }

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "A2A-Version": "0.3",
        "X-Caller-Id": caller_id,
        "X-Caller-Role": caller_role,
        "User-Agent": f"OracleA2AClient/1.0 ({caller_id})",
    }
    if api_key:
        headers["X-API-KEY"] = api_key
    if simulate_unauthorized:
        headers["X-Simulate-Unauthorized"] = "true"
        headers["X-API-KEY"] = "invalid-unauthorized-key-000"

    t0 = time.perf_counter()
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(target_url, json=payload, headers=headers)
    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)

    try:
        resp_json = response.json()
    except Exception:
        resp_json = {"raw_body": response.text}

    reply_text = (
        extract_text_from_a2a_response(resp_json)
        if response.status_code == 200
        else resp_json.get("message", f"HTTP {response.status_code}: {response.text}")
    )

    return {
        "http_status": response.status_code,
        "latency_ms": elapsed_ms,
        "caller_id": caller_id,
        "caller_role": caller_role,
        "prompt": prompt,
        "reply": reply_text,
        "a2a_request": payload,
        "a2a_response": resp_json,
    }


async def run_cli_test_suite(base_url: str, api_key: str) -> None:
    """Run an end-to-end automated CLI verification suite against the A2A server."""
    print("============================================================")
    print("🚀 Running A2A End-to-End Verification Suite")
    print(f"   Target A2A Endpoint: {base_url}")
    print("============================================================\n")

    async with httpx.AsyncClient(timeout=30.0) as client:
        card_resp = await client.get(
            f"{base_url.rstrip('/')}/.well-known/agent-card.json"
        )
        print(
            f"1. [Discovery] GET /.well-known/agent-card.json -> HTTP {card_resp.status_code}"
        )
        card = card_resp.json()
        print(f"   Agent Name: {card.get('name')} | Version: {card.get('version')}")
        print(
            f"   Skills Discovered: {[s.get('name') for s in card.get('skills', [])]}\n"
        )

    scenarios = [
        {
            "caller_id": "oracle-autonomous-db-monitor",
            "caller_role": "Oracle Autonomous DB Alert Agent",
            "prompt": (
                "Create a CRITICAL severity trouble ticket in category DATABASE for "
                "'ORA-12516 TNS listener connection pool exhausted on ADB-PROD-01' "
                "reported by dba-alerts@oracle.com."
            ),
            "api_key": api_key,
            "simulate_unauthorized": False,
        },
        {
            "caller_id": "oracle-fusion-scm-agent",
            "caller_role": "Oracle Fusion Supply Chain Workflow",
            "prompt": "List all OPEN trouble tickets and summarize their ticket IDs and severities.",
            "api_key": api_key,
            "simulate_unauthorized": False,
        },
        {
            "caller_id": "unknown-external-bot",
            "caller_role": "Unverified External Probe",
            "prompt": "Attempt to list all trouble tickets without valid security token.",
            "api_key": None,
            "simulate_unauthorized": True,
        },
    ]

    for idx, sc in enumerate(scenarios, start=2):
        print(
            f"{idx}. [A2A Call] Caller={sc['caller_id']} | Prompt='{sc['prompt'][:70]}...'"
        )
        res = await send_a2a_message(
            base_url=base_url,
            prompt=sc["prompt"],
            caller_id=sc["caller_id"],
            caller_role=sc["caller_role"],
            api_key=sc["api_key"],
            simulate_unauthorized=sc["simulate_unauthorized"],
        )
        print(f"   HTTP {res['http_status']} ({res['latency_ms']} ms)")
        print(f"   Agent Reply: {res['reply']}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test the Service Request A2A Server")
    parser.add_argument(
        "--url",
        default=os.environ.get("CLOUD_RUN_URL", "http://localhost:8080"),
        help="Base URL of the deployed A2A server",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("A2A_API_KEY", "oracle-ai-world-2026"),
        help="Optional X-API-KEY for authenticated A2A calls",
    )
    args = parser.parse_args()
    asyncio.run(run_cli_test_suite(args.url, args.api_key))
