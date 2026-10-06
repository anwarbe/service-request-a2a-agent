"""ADK 2.0 Service Request & Trouble Ticket Agent with GCS, Local Fallback, and Action Auditing."""

from __future__ import annotations

import datetime
import json
import logging
import os
from pathlib import Path
import time
from typing import Any
import uuid

from dotenv import load_dotenv
from google.adk.agents import LlmAgent
from google.cloud import storage

from audit import current_caller_ctx, record_audit_event

load_dotenv()

logger = logging.getLogger(__name__)

GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME", "oracle-hackathon-tickets")
LOCAL_FALLBACK_DIR = Path(
    os.environ.get("LOCAL_TICKETS_DIR", "/tmp/service_request_tickets")
)

VALID_SEVERITIES = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
VALID_STATUSES = {"OPEN", "IN_PROGRESS", "PENDING_CUSTOMER", "RESOLVED", "CLOSED"}


def _save_ticket_record(ticket_id: str, ticket_data: dict[str, Any]) -> str:
    """Persist ticket JSON to Google Cloud Storage with local filesystem fallback."""
    payload = json.dumps(ticket_data, indent=2)

    # Always write to local fallback cache so local testing and simulated mode
    # remain stateful across requests within the same container instance.
    try:
        LOCAL_FALLBACK_DIR.mkdir(parents=True, exist_ok=True)
        (LOCAL_FALLBACK_DIR / f"{ticket_id}.json").write_text(payload, encoding="utf-8")
    except Exception as local_err:
        logger.debug("Local ticket cache write skipped: %s", local_err)

    if GCS_BUCKET_NAME:
        try:
            client = storage.Client()
            bucket = client.bucket(GCS_BUCKET_NAME)
            blob = bucket.blob(f"tickets/{ticket_id}.json")
            blob.upload_from_string(payload, content_type="application/json")
            return f"Saved to GCS bucket gs://{GCS_BUCKET_NAME}/tickets/{ticket_id}.json"
        except Exception as gcs_err:
            logger.warning("GCS write skipped for %s: %s", ticket_id, gcs_err)
            return (
                f"Persisted to local fallback store "
                f"(GCS write skipped: {gcs_err.__class__.__name__})"
            )

    return "Persisted to local fallback store (GCS_BUCKET_NAME not configured)"


def _load_ticket_record(ticket_id: str) -> dict[str, Any] | None:
    """Retrieve a ticket JSON record from GCS or the local fallback store."""
    normalized_id = ticket_id.strip()

    if GCS_BUCKET_NAME:
        try:
            client = storage.Client()
            bucket = client.bucket(GCS_BUCKET_NAME)
            blob = bucket.blob(f"tickets/{normalized_id}.json")
            if blob.exists():
                return json.loads(blob.download_as_text())
        except Exception as gcs_err:
            logger.debug("GCS read fallback for %s: %s", normalized_id, gcs_err)

    local_file = LOCAL_FALLBACK_DIR / f"{normalized_id}.json"
    if local_file.exists():
        try:
            return json.loads(local_file.read_text(encoding="utf-8"))
        except Exception as local_err:
            logger.warning("Failed reading local ticket %s: %s", normalized_id, local_err)

    return None


def get_all_tickets_list(status_filter: str = "", limit: int = 50) -> list[dict[str, Any]]:
    """Return structured list of tickets from GCS + local fallback."""
    tickets_by_id: dict[str, dict[str, Any]] = {}
    normalized_filter = status_filter.strip().upper()

    if GCS_BUCKET_NAME:
        try:
            client = storage.Client()
            bucket = client.bucket(GCS_BUCKET_NAME)
            for blob in bucket.list_blobs(prefix="tickets/", max_results=50):
                if blob.name.endswith(".json"):
                    record = json.loads(blob.download_as_text())
                    tid = record.get("ticket_id")
                    if tid:
                        tickets_by_id[tid] = record
        except Exception as gcs_err:
            logger.debug("GCS list fallback: %s", gcs_err)

    if LOCAL_FALLBACK_DIR.exists():
        for local_file in LOCAL_FALLBACK_DIR.glob("REQ-*.json"):
            try:
                record = json.loads(local_file.read_text(encoding="utf-8"))
                tid = record.get("ticket_id")
                if tid and tid not in tickets_by_id:
                    tickets_by_id[tid] = record
            except Exception:
                continue

    records = list(tickets_by_id.values())
    if normalized_filter:
        records = [
            r for r in records if str(r.get("status", "")).upper() == normalized_filter
        ]

    records.sort(
        key=lambda r: str(r.get("updated_at") or r.get("created_at") or ""),
        reverse=True,
    )
    return records[: max(1, min(limit, 50))]


def create_trouble_ticket(
    summary: str,
    severity: str = "MEDIUM",
    category: str = "IT_INFRASTRUCTURE",
    user_email: str = "participant@oracle.com",
    description: str = "",
) -> str:
    """Creates a new IT or Supply Chain service request / trouble ticket and persists it to Google Cloud Storage.

    Args:
        summary: Short title summarizing the issue or request.
        severity: Ticket priority level (LOW, MEDIUM, HIGH, CRITICAL). Defaults to MEDIUM.
        category: Issue category (e.g., IT_INFRASTRUCTURE, DATABASE, SUPPLY_CHAIN, ERP_FUSION).
        user_email: Email address of the requester. Defaults to participant@oracle.com.
        description: Optional detailed description, error logs, or context.

    Returns:
        Confirmation message containing the generated ticket ID, status, and storage location.
    """
    t0 = time.perf_counter()
    now = datetime.datetime.now(datetime.timezone.utc)
    short_uid = uuid.uuid4().hex[:4].upper()
    ticket_id = f"REQ-{now.strftime('%Y%m%d%H%M%S')}-{short_uid}"

    normalized_severity = severity.strip().upper()
    if normalized_severity not in VALID_SEVERITIES:
        normalized_severity = "MEDIUM"

    ctx = current_caller_ctx.get()
    caller_id = ctx.get("caller_id", "anonymous")

    iso_now = now.isoformat()
    ticket_data: dict[str, Any] = {
        "ticket_id": ticket_id,
        "summary": summary.strip(),
        "description": description.strip() or summary.strip(),
        "category": category.strip().upper(),
        "severity": normalized_severity,
        "status": "OPEN",
        "assigned_group": (
            "L3 Database & Cloud Ops"
            if normalized_severity in {"HIGH", "CRITICAL"}
            else "L2 IT & SCM Support Team"
        ),
        "created_by": user_email.strip(),
        "caller_agent": caller_id,
        "created_at": iso_now,
        "updated_at": iso_now,
        "resolution_notes": "",
    }

    storage_msg = _save_ticket_record(ticket_id, ticket_data)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    record_audit_event(
        event_type="TOOL_EXECUTION",
        action="Created Trouble Ticket",
        tool_name="create_trouble_ticket",
        tool_args={
            "summary": summary.strip(),
            "severity": normalized_severity,
            "category": category.strip().upper(),
            "user_email": user_email.strip(),
        },
        ticket_id=ticket_id,
        outcome=f"Created {ticket_id} ({normalized_severity}) -> {ticket_data['assigned_group']}",
        latency_ms=elapsed_ms,
    )
    if isinstance(ctx.get("tools_invoked"), list):
        ctx["tools_invoked"].append(f"create_trouble_ticket({ticket_id})")

    return (
        f"Service Request {ticket_id} created successfully! "
        f"Status: OPEN | Severity: {normalized_severity} | "
        f"Assigned: {ticket_data['assigned_group']}. {storage_msg}"
    )


def get_ticket_status(ticket_id: str) -> str:
    """Retrieves the current status and metadata of a service request ticket by ID.

    Args:
        ticket_id: The unique ticket identifier (e.g., REQ-20261001120000-A1B2).

    Returns:
        Formatted ticket details including status, summary, severity, assignee, and timestamps.
    """
    t0 = time.perf_counter()
    normalized_id = ticket_id.strip()
    data = _load_ticket_record(normalized_id)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    ctx = current_caller_ctx.get()

    if data:
        notes = (
            f" | Resolution Notes: '{data.get('resolution_notes')}'"
            if data.get("resolution_notes")
            else ""
        )
        record_audit_event(
            event_type="TOOL_EXECUTION",
            action="Checked Ticket Status",
            tool_name="get_ticket_status",
            tool_args={"ticket_id": normalized_id},
            ticket_id=normalized_id,
            outcome=f"Found {normalized_id}: Status={data.get('status')}, Severity={data.get('severity')}",
            latency_ms=elapsed_ms,
        )
        if isinstance(ctx.get("tools_invoked"), list):
            ctx["tools_invoked"].append(f"get_ticket_status({normalized_id})")

        return (
            f"Ticket {normalized_id}: Status={data.get('status')}, "
            f"Severity={data.get('severity')}, "
            f"Category={data.get('category', 'GENERAL')}, "
            f"Summary='{data.get('summary')}', "
            f"Assigned='{data.get('assigned_group', 'L2 IT Support Team')}', "
            f"CreatedBy={data.get('created_by')}, "
            f"UpdatedAt={data.get('updated_at', data.get('timestamp'))}{notes}"
        )

    record_audit_event(
        event_type="TOOL_EXECUTION",
        action="Checked Ticket Status (External/Simulated)",
        tool_name="get_ticket_status",
        tool_args={"ticket_id": normalized_id},
        ticket_id=normalized_id,
        outcome=f"Simulated lookup for {normalized_id} -> IN_PROGRESS",
        latency_ms=elapsed_ms,
    )
    if isinstance(ctx.get("tools_invoked"), list):
        ctx["tools_invoked"].append(f"get_ticket_status({normalized_id})")

    return (
        f"Ticket {normalized_id}: Status=IN_PROGRESS "
        f"(Assigned to L2 IT Support Team - simulated lookup for external ticket ID)."
    )


def update_ticket_status(
    ticket_id: str,
    status: str,
    resolution_notes: str = "",
) -> str:
    """Updates the status or resolution notes of an existing trouble ticket.

    Args:
        ticket_id: The unique ticket identifier to update.
        status: New status (OPEN, IN_PROGRESS, PENDING_CUSTOMER, RESOLVED, CLOSED).
        resolution_notes: Optional notes explaining the update or resolution.

    Returns:
        Confirmation message with the updated ticket state.
    """
    t0 = time.perf_counter()
    normalized_id = ticket_id.strip()
    normalized_status = status.strip().upper()
    if normalized_status not in VALID_STATUSES:
        return (
            f"Invalid status '{status}'. Must be one of: "
            f"{', '.join(sorted(VALID_STATUSES))}."
        )

    ctx = current_caller_ctx.get()
    data = _load_ticket_record(normalized_id)
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if not data:
        data = {
            "ticket_id": normalized_id,
            "summary": "External / Ad-hoc Service Request",
            "category": "IT_INFRASTRUCTURE",
            "severity": "MEDIUM",
            "assigned_group": "L2 IT & SCM Support Team",
            "created_by": "participant@oracle.com",
            "caller_agent": ctx.get("caller_id", "anonymous"),
            "created_at": now_iso,
        }

    data["status"] = normalized_status
    data["updated_at"] = now_iso
    data["last_updated_by_caller"] = ctx.get("caller_id", "anonymous")
    if resolution_notes.strip():
        data["resolution_notes"] = resolution_notes.strip()

    storage_msg = _save_ticket_record(normalized_id, data)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    record_audit_event(
        event_type="TOOL_EXECUTION",
        action=f"Updated Ticket Status -> {normalized_status}",
        tool_name="update_ticket_status",
        tool_args={
            "ticket_id": normalized_id,
            "status": normalized_status,
            "resolution_notes": resolution_notes.strip(),
        },
        ticket_id=normalized_id,
        outcome=f"Updated {normalized_id} to {normalized_status}",
        latency_ms=elapsed_ms,
    )
    if isinstance(ctx.get("tools_invoked"), list):
        ctx["tools_invoked"].append(
            f"update_ticket_status({normalized_id}->{normalized_status})"
        )

    return (
        f"Ticket {normalized_id} updated to Status={normalized_status}. "
        f"{storage_msg}"
    )


def list_trouble_tickets(status_filter: str = "", limit: int = 10) -> str:
    """Lists recent service request tickets from storage, optionally filtered by status.

    Args:
        status_filter: Optional status filter (e.g., OPEN, IN_PROGRESS, RESOLVED).
        limit: Maximum number of tickets to return (default 10).

    Returns:
        JSON-formatted summary list of matching tickets.
    """
    t0 = time.perf_counter()
    records = get_all_tickets_list(status_filter=status_filter, limit=limit)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    ctx = current_caller_ctx.get()

    record_audit_event(
        event_type="TOOL_EXECUTION",
        action="Listed Trouble Tickets",
        tool_name="list_trouble_tickets",
        tool_args={"status_filter": status_filter, "limit": limit},
        outcome=f"Returned {len(records)} ticket(s) (filter='{status_filter or 'ALL'}')",
        latency_ms=elapsed_ms,
    )
    if isinstance(ctx.get("tools_invoked"), list):
        ctx["tools_invoked"].append(f"list_trouble_tickets({len(records)})")

    if not records:
        return "No matching service request tickets found."

    return json.dumps(records, indent=2)


# Define ADK 2.0 Root Agent
root_agent = LlmAgent(
    name="service_request_agent",
    model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
    description=(
        "ServiceNow-style IT and Supply Chain Trouble Ticket & Service Request "
        "A2A Agent hosted on Google Cloud Run for Oracle AI Studio and Oracle Fusion."
    ),
    instruction="""You are an automated Enterprise IT and Supply Chain Service Request Agent.
Your role is to assist Oracle AI Studio and Oracle Fusion users in:
1. Logging new trouble tickets and service requests (`create_trouble_ticket`).
2. Checking the real-time status of existing tickets (`get_ticket_status`).
3. Updating ticket statuses or adding resolution notes (`update_ticket_status`).
4. Listing recent tickets filtered by status (`list_trouble_tickets`).

All ticket records are persisted to Google Cloud Storage (GCS) with automatic local fallback and full audit logging.
Always respond in a concise, structured, and professional manner, clearly stating the Ticket ID, Severity, Status, and Assigned Support Group.""",
    tools=[
        create_trouble_ticket,
        get_ticket_status,
        update_ticket_status,
        list_trouble_tickets,
    ],
)
