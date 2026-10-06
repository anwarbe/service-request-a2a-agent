"""Security, Monitoring, and Audit Logging Engine for the A2A Server.

Tracks:
1. WHO/WHAT called the A2A server (caller_id, caller_role, caller_ip, user_agent, auth_status, trace_id).
2. WHAT A2A protocol method and prompt were sent.
3. WHAT action/tool the agent took (tool_name, tool_args, ticket_id, outcome, latency_ms).

All audit events are:
- Emitted as structured JSON to stdout for Google Cloud Logging & Cloud Monitoring.
- Persisted to Google Cloud Storage (`gs://<GCS_BUCKET_NAME>/audit/audit_log.jsonl`) + local fallback.
- Exposed via `/api/audit` for the live monitoring dashboard.
"""

from __future__ import annotations

from collections import deque
from contextvars import ContextVar
import datetime
import json
import logging
import os
from pathlib import Path
import sys
from typing import Any
import uuid

from google.cloud import storage

logger = logging.getLogger("a2a_audit")

GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME", "oracle-hackathon-tickets")
LOCAL_AUDIT_DIR = Path(
    os.environ.get("LOCAL_TICKETS_DIR", "/tmp/service_request_tickets")
)
LOCAL_AUDIT_FILE = LOCAL_AUDIT_DIR / "audit_events.jsonl"

# In-memory ring buffer for rapid live monitoring queries
_RECENT_AUDIT_EVENTS: deque[dict[str, Any]] = deque(maxlen=250)

# Context variable holding the active request's caller metadata during ADK tool execution
current_caller_ctx: ContextVar[dict[str, Any]] = ContextVar(
    "current_caller_ctx",
    default={
        "trace_id": "local-direct",
        "caller_id": "direct-local-caller",
        "caller_role": "Local / Direct SDK",
        "caller_ip": "127.0.0.1",
        "user_agent": "python-sdk",
        "auth_status": "INTERNAL",
        "a2a_method": "direct_tool_call",
        "a2a_request_id": "",
        "prompt_preview": "",
        "tools_invoked": [],
    },
)


def _emit_cloud_log(event: dict[str, Any]) -> None:
    """Write structured JSON log entry compatible with Google Cloud Logging."""
    severity_map = {
        "SECURITY_BLOCK": "WARNING",
        "A2A_ERROR": "ERROR",
        "TOOL_EXECUTION": "NOTICE",
        "A2A_REQUEST": "INFO",
    }
    cloud_log_entry = {
        "severity": severity_map.get(event.get("event_type", ""), "INFO"),
        "message": (
            f"[A2A Audit] {event.get('event_type')} | "
            f"caller={event.get('caller_id')} ({event.get('auth_status')}) | "
            f"action={event.get('action')} | outcome={event.get('outcome')}"
        ),
        "logging.googleapis.com/labels": {
            "component": "service-request-a2a-agent",
            "event_type": str(event.get("event_type", "UNKNOWN")),
            "caller_id": str(event.get("caller_id", "unknown")),
            "auth_status": str(event.get("auth_status", "unknown")),
        },
        "audit_event": True,
        **event,
    }
    sys.stdout.write(json.dumps(cloud_log_entry) + "\n")
    sys.stdout.flush()


def record_audit_event(
    *,
    event_type: str,
    action: str,
    outcome: str,
    status: str = "SUCCESS",
    ticket_id: str = "",
    tool_name: str = "",
    tool_args: dict[str, Any] | None = None,
    latency_ms: float | None = None,
    extra_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create, log, and persist a structured security/action audit event."""
    ctx = current_caller_ctx.get()
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    event_id = f"AUD-{uuid.uuid4().hex[:8].upper()}"

    event: dict[str, Any] = {
        "event_id": event_id,
        "timestamp": now_iso,
        "trace_id": ctx.get("trace_id", ""),
        "event_type": event_type,
        "status": status,
        "caller_id": ctx.get("caller_id", "anonymous"),
        "caller_role": ctx.get("caller_role", "External A2A Client"),
        "caller_ip": ctx.get("caller_ip", "unknown"),
        "user_agent": ctx.get("user_agent", "unknown"),
        "auth_status": ctx.get("auth_status", "PUBLIC_UNAUTHENTICATED"),
        "a2a_method": ctx.get("a2a_method", "unknown"),
        "a2a_request_id": ctx.get("a2a_request_id", ""),
        "prompt_preview": ctx.get("prompt_preview", ""),
        "action": action,
        "tool_name": tool_name,
        "tool_args": tool_args or {},
        "ticket_id": ticket_id,
        "outcome": outcome,
        "latency_ms": round(latency_ms, 1) if latency_ms is not None else None,
    }
    if extra_context:
        event.update(extra_context)

    _RECENT_AUDIT_EVENTS.appendleft(event)
    _emit_cloud_log(event)

    # Persist to local JSONL file
    try:
        LOCAL_AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        with LOCAL_AUDIT_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except Exception as err:
        logger.debug("Local audit write skipped: %s", err)

    return event


def get_audit_summary(limit: int = 60) -> dict[str, Any]:
    """Return recent audit events and aggregated monitoring/security KPIs."""
    events = list(_RECENT_AUDIT_EVENTS)

    # Load from disk if memory buffer is empty on startup
    if not events and LOCAL_AUDIT_FILE.exists():
        try:
            lines = LOCAL_AUDIT_FILE.read_text(encoding="utf-8").splitlines()
            for line in reversed(lines[-limit:]):
                if line.strip():
                    events.append(json.loads(line))
        except Exception:
            pass

    total_rpc = sum(1 for e in events if e.get("event_type") == "A2A_REQUEST")
    total_tools = sum(1 for e in events if e.get("event_type") == "TOOL_EXECUTION")
    security_blocks = sum(
        1 for e in events if e.get("event_type") == "SECURITY_BLOCK"
    )
    verified_calls = sum(
        1
        for e in events
        if e.get("event_type") == "A2A_REQUEST"
        and str(e.get("auth_status", "")).startswith("VERIFIED")
    )

    latencies = [
        float(e["latency_ms"])
        for e in events
        if e.get("event_type") == "A2A_REQUEST" and e.get("latency_ms") is not None
    ]
    avg_latency_ms = round(sum(latencies) / len(latencies), 1) if latencies else 0.0

    # Breakdown by caller and tool
    caller_counts: dict[str, int] = {}
    tool_counts: dict[str, int] = {}
    for e in events:
        if e.get("event_type") in {"A2A_REQUEST", "SECURITY_BLOCK"}:
            cid = str(e.get("caller_id") or "unknown")
            caller_counts[cid] = caller_counts.get(cid, 0) + 1
        if e.get("event_type") == "TOOL_EXECUTION" and e.get("tool_name"):
            tname = str(e["tool_name"])
            tool_counts[tname] = tool_counts.get(tname, 0) + 1

    return {
        "metrics": {
            "total_a2a_requests": total_rpc,
            "total_tool_actions": total_tools,
            "security_blocks": security_blocks,
            "verified_auth_calls": verified_calls,
            "avg_latency_ms": avg_latency_ms,
            "unique_callers": len(caller_counts),
            "caller_breakdown": caller_counts,
            "tool_breakdown": tool_counts,
        },
        "events": events[:limit],
    }
