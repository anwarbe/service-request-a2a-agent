"""Unit tests for the Service Request A2A Agent tools and endpoint parsing."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

import agent
from main import _parse_endpoint_config


@pytest.fixture(autouse=True)
def isolated_ticket_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect local fallback storage and disable live GCS calls during unit tests."""
    monkeypatch.setattr(agent, "LOCAL_FALLBACK_DIR", tmp_path)
    monkeypatch.setattr(agent, "GCS_BUCKET_NAME", "")
    return tmp_path


def test_create_and_get_trouble_ticket():
    create_msg = agent.create_trouble_ticket(
        summary="Database connection pool exhausted",
        severity="HIGH",
        category="DATABASE",
        user_email="admin@oracle.com",
        description="ORA-12516 TNS listener could not find available handler",
    )
    assert "created successfully!" in create_msg
    assert "Severity: HIGH" in create_msg
    assert "L3 Database & Cloud Ops" in create_msg

    # Extract REQ-... ticket ID
    ticket_id = next(
        token for token in create_msg.split() if token.startswith("REQ-")
    )

    status_msg = agent.get_ticket_status(ticket_id)
    assert f"Ticket {ticket_id}: Status=OPEN" in status_msg
    assert "Severity=HIGH" in status_msg
    assert "Summary='Database connection pool exhausted'" in status_msg


def test_update_and_list_tickets():
    create_msg = agent.create_trouble_ticket(
        summary="SCM shipment sync delay",
        severity="MEDIUM",
        category="SUPPLY_CHAIN",
    )
    ticket_id = next(
        token for token in create_msg.split() if token.startswith("REQ-")
    )

    update_msg = agent.update_ticket_status(
        ticket_id=ticket_id,
        status="RESOLVED",
        resolution_notes="Restarted GoldenGate replication pipeline",
    )
    assert "Status=RESOLVED" in update_msg

    status_msg = agent.get_ticket_status(ticket_id)
    assert "Status=RESOLVED" in status_msg
    assert "Restarted GoldenGate replication pipeline" in status_msg

    listed_json = agent.list_trouble_tickets(status_filter="RESOLVED")
    records = json.loads(listed_json)
    assert len(records) == 1
    assert records[0]["ticket_id"] == ticket_id


def test_parse_endpoint_config():
    assert _parse_endpoint_config("", 8080) == ("localhost", 8080, "http")
    assert _parse_endpoint_config(
        "https://service-request-a2a-agent-xyz.a.run.app/", 8080
    ) == ("service-request-a2a-agent-xyz.a.run.app", 443, "https")
    assert _parse_endpoint_config("http://localhost:9000", 8080) == (
        "localhost",
        9000,
        "http",
    )
