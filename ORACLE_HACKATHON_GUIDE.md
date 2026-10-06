# Oracle AI World — Fusion Hackathon Guide: Connecting Oracle AI Studio to Google Cloud A2A (`service_request_agent`)

Welcome to the **Oracle AI World Fusion Hackathon**! This guide provides everything the Oracle Hackathon organizing team and conference participants need to connect **Oracle AI Studio** (and custom A2A clients) to the live **Google Cloud Run `service_request_agent`** over the open **Agent2Agent (A2A)** protocol.

---

## 1. Quick-Reference Connection Sheet

Use these live endpoints to register and test the remote A2A agent in **Oracle AI Studio**:

| Configuration Field | Live Value |
| :--- | :--- |
| **Agent Name / ID** | `service_request_agent` |
| **A2A JSON-RPC Endpoint (`POST`)** | `https://service-request-a2a-agent-kavsor5jha-uc.a.run.app` |
| **A2A Agent Card Discovery URL (`GET`)** | `https://service-request-a2a-agent-kavsor5jha-uc.a.run.app/.well-known/agent-card.json` |
| **Legacy Agent Card Alias (`GET`)** | `https://service-request-a2a-agent-kavsor5jha-uc.a.run.app/.well-known/agent.json` |
| **Live Command Center & Audit UI (`GET`)** | `https://service-request-a2a-agent-kavsor5jha-uc.a.run.app` |
| **Health & Readiness Probe (`GET`)** | `https://service-request-a2a-agent-kavsor5jha-uc.a.run.app/health` |
| **Protocol Transport & Versions** | `JSONRPC` (Supports **both** A2A `0.3` `message/send` and A2A `1.0` `SendMessage`) |
| **Inbound Authentication** | **Public / Unauthenticated** enabled by default for frictionless hackathon onboarding.<br>*(Optional authenticated mode: `X-API-KEY: oracle-ai-world-2026` or `Authorization: Bearer oracle-ai-world-2026`)* |
| **Optional Team Attribution Headers** | `X-Caller-Id: <your-team-name>`<br>`X-Caller-Role: Oracle AI Studio Hackathon` |
| **Source Code Repository** | [https://github.com/anwarbe/service-request-a2a-agent](https://github.com/anwarbe/service-request-a2a-agent) |

---

## 2. Architecture & Scenario Overview

### Hackathon Scenario
The **`service_request_agent`** is an enterprise IT and Supply Chain Trouble Ticket Agent (modeled after ServiceNow / ITSM workflows) hosted on **Google Cloud Run** in Google Cloud Partner Engineering tenancy. When invoked by an agent in **Oracle AI Studio**, it uses **Google Agent Development Kit (ADK 2.0)** and **Vertex AI (`gemini-3.5-flash`)** to create, query, update, and list trouble tickets persisted as structured JSON records in **Google Cloud Storage (GCS)**.

```mermaid
flowchart LR
    subgraph Oracle["Oracle Cloud / Oracle AI Studio"]
        OAI["Oracle AI Studio Agent\n(Hackathon Participant)"]
    end

    subgraph GCP["Google Cloud Platform (Partner Engineering)"]
        CR["Google Cloud Run\n(ADK 2.0 service_request_agent)"]
        VA["Vertex AI\n(Gemini 3.5 Flash)"]
        GCS[("Google Cloud Storage\ngs://iamtests-315719-oracle-hackathon-tickets")]
        AUD["Live Command Center & Audit Trail\n(/, /api/audit, /api/tickets)"]
    end

    OAI <-->|"1. GET /.well-known/agent-card.json\n2. POST / (A2A JSON-RPC 2.0)"| CR
    CR <-->|"Vertex AI ADC"| VA
    CR <-->|"Read/Write Ticket JSONs\ntickets/REQ-*.json"| GCS
    CR -->|"Tracks Caller & Tool Actions"| AUD
```

### Capabilities & Tools Exposed by `service_request_agent`

| Tool Name | Description | Parameters |
| :--- | :--- | :--- |
| **`create_trouble_ticket`** | Creates a new IT or Supply Chain trouble ticket and stores `tickets/<ticket_id>.json` in GCS. Automatically routes `HIGH`/`CRITICAL` tickets to `L3 Database & Cloud Ops` and `LOW`/`MEDIUM` tickets to `L2 IT & SCM Support Team`. | `summary` *(str)*, `severity` *(`LOW`, `MEDIUM`, `HIGH`, `CRITICAL`)*, `category` *(e.g. `DATABASE`, `SUPPLY_CHAIN`, `ERP_FUSION`, `IT_INFRASTRUCTURE`)*, `user_email` *(str)*, `description` *(str)* |
| **`get_ticket_status`** | Retrieves real-time status, assignee, severity, and resolution notes for a ticket ID from GCS. | `ticket_id` *(str, e.g. `REQ-20261006123055-A68F`)* |
| **`update_ticket_status`** | Updates an existing ticket's status and records resolution notes in GCS. | `ticket_id` *(str)*, `status` *(`OPEN`, `IN_PROGRESS`, `PENDING_CUSTOMER`, `RESOLVED`, `CLOSED`)*, `resolution_notes` *(str)* |
| **`list_trouble_tickets`** | Lists recent trouble tickets stored in GCS, optionally filtered by status. | `status_filter` *(optional str, e.g. `OPEN`, `RESOLVED`)*, `limit` *(int, default `10`)* |

---

## 3. Step-by-Step Guide for Hackathon Participants

### Step 1: Verify the A2A Agent Card Discovery URL
Before registering the agent in Oracle AI Studio, verify that the A2A discovery endpoint returns the agent's metadata and skills:

```bash
curl -s https://service-request-a2a-agent-kavsor5jha-uc.a.run.app/.well-known/agent-card.json | jq .
```

> [!NOTE]
> The server exposes both `/.well-known/agent-card.json` and `/.well-known/agent.json`, and includes both A2A v0.3 top-level fields (`url`, `protocolVersion`, `preferredTransport`) and A2A v1.0 `supportedInterfaces` so any version of Oracle AI Studio's A2A connector validates cleanly.

---

### Step 2: Connect the Remote A2A Agent in Oracle AI Studio

1. Open **Oracle AI Studio** in your Oracle Fusion / AI World hackathon environment.
2. Navigate to **Agents / Remote Agents (A2A Connections)** and click **Add Remote A2A Agent** (or **Register External Agent**).
3. Enter the connection details:
   - **Name**: `service_request_agent` (or `GCP Service Request Agent`)
   - **Agent Card / Discovery URL**:
     ```text
     https://service-request-a2a-agent-kavsor5jha-uc.a.run.app/.well-known/agent-card.json
     ```
   - **A2A Endpoint URL**:
     ```text
     https://service-request-a2a-agent-kavsor5jha-uc.a.run.app
     ```
   - **Authentication**:
     - Select **None / No Authentication** for standard public access, **OR**
     - Select **API Key / Custom Header** with header `X-API-KEY` and value `oracle-ai-world-2026` (if testing authenticated mode).
   - **Optional Custom Headers** *(Recommended for Hackathon Teams)*:
     - `X-Caller-Id`: `team-<your-team-name>` (e.g., `team-alpha-fusion`)
     - `X-Caller-Role`: `Oracle AI Studio Hackathon`
4. Save and test the connection. Your Oracle AI Studio orchestrator agent can now delegate service request and trouble ticket tasks to `service_request_agent`!

---

### Step 3: Try Sample Prompts from Oracle AI Studio

Once connected, send any of the following natural-language prompts through your Oracle AI Studio agent:

1. **Log a Critical Database Incident**:
   > *"Create a CRITICAL severity trouble ticket in category DATABASE for 'ORA-12516 TNS listener connection pool exhausted on ADB-FINANCE-PROD' reported by team1@oracle.com."*
2. **Log an Oracle Fusion Supply Chain Issue**:
   > *"Create a HIGH severity service request in category SUPPLY_CHAIN for 'ASN shipment webhook timeout between Oracle Fusion SCM and Warehouse Management' for scm-ops@oracle.com."*
3. **Check Ticket Status**:
   > *"Check the current status and assigned support group for ticket REQ-20261006123055-A68F."*
4. **Update / Resolve a Ticket**:
   > *"Update ticket REQ-20261006123055-A68F status to RESOLVED with resolution notes: 'Increased Autonomous DB connection pool from 50 to 200'."*
5. **List Open Tickets in Google Cloud Storage**:
   > *"List all OPEN trouble tickets currently stored in Google Cloud Storage."*

---

### Step 4: Watch Your Calls Live in the Interactive Command Center

Open **[https://service-request-a2a-agent-kavsor5jha-uc.a.run.app](https://service-request-a2a-agent-kavsor5jha-uc.a.run.app)** in your browser at any time during prep testing or the hackathon to access:

1. **🛡️ Real-Time Security & Action Audit Log**:
   - Displays **who/what called** the A2A server (`caller_id`, `caller_role`, IP address, and authentication status), **what A2A method and prompt** were received, **what tool action** the agent executed (`create_trouble_ticket`, `get_ticket_status`, `update_ticket_status`, `list_trouble_tickets`), the resulting **`REQ-...` Ticket ID**, and **response latency in milliseconds**.
2. **🎫 Live GCS Trouble Tickets Board**:
   - Inspect all tickets persisted in `gs://iamtests-315719-oracle-hackathon-tickets/tickets/*.json` in real time.
3. **💬 Interactive A2A Playground & Wire Inspector**:
   - Test prompts directly in the browser as different simulated personas and expand any message to inspect the raw **A2A JSON-RPC 2.0 Request & Response** payloads.
4. **▶ Start / ⏸ Pause Live Multi-Agent Automation**:
   - Organizers can click **▶ Start Live Automation** during demos to stream realistic multi-agent A2A traffic into the dashboard and click **⏸ Pause Automation** whenever finished.

---

## 4. Direct A2A JSON-RPC 2.0 Examples (`curl` & Python)

If you want to test the endpoint directly from a terminal or custom script before wiring up Oracle AI Studio, use the verified examples below.

### Option A: `curl` (A2A v0.3 `message/send`)

```bash
curl -s -X POST "https://service-request-a2a-agent-kavsor5jha-uc.a.run.app/" \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 0.3" \
  -H "X-Caller-Id: oracle-hackathon-team-1" \
  -H "X-Caller-Role: Oracle AI Studio Participant" \
  -H "X-API-KEY: oracle-ai-world-2026" \
  -d '{
    "jsonrpc": "2.0",
    "id": "req-001",
    "method": "message/send",
    "params": {
      "message": {
        "messageId": "msg-001",
        "role": "user",
        "parts": [
          {
            "kind": "text",
            "text": "Create a HIGH severity ticket in category SUPPLY_CHAIN for Order Fulfillment sync delay reported by participant@oracle.com"
          }
        ]
      }
    }
  }' | jq .
```

### Option B: Python Client Using Google ADK `RemoteA2aAgent`

Participants building Python agents with **Google ADK** can connect to this A2A server as a remote sub-agent in just a few lines of code:

```python
from google.adk.agents import LlmAgent
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent

# Connect to the remote Cloud Run A2A Service Request Agent via its Agent Card URL
remote_service_request_agent = RemoteA2aAgent(
    name="service_request_agent",
    description="Handles IT and Supply Chain trouble tickets stored in Google Cloud Storage.",
    agent_card="https://service-request-a2a-agent-kavsor5jha-uc.a.run.app/.well-known/agent-card.json",
)

# Attach it as a sub-agent to an orchestrator agent
orchestrator = LlmAgent(
    name="oracle_fusion_orchestrator",
    model="gemini-3.5-flash",
    instruction="Delegate any IT or Supply Chain trouble ticket requests to service_request_agent.",
    sub_agents=[remote_service_request_agent],
)
```

---

## 5. Official Documentation & References (ADK, A2A, and Oracle MCP)

To explore further or extend your hackathon project with Google Cloud ADK, A2A, or Oracle Database@Google Cloud MCP tools, consult these official resources:

1. **Google Agent Development Kit (ADK)**:
   - **ADK Official Documentation**: [https://google.github.io/adk-docs/](https://google.github.io/adk-docs/)
   - **ADK Agent2Agent (A2A) Guide (`to_a2a` & `RemoteA2aAgent`)**: [https://google.github.io/adk-docs/a2a/](https://google.github.io/adk-docs/a2a/)
   - **ADK Python SDK Repository (`google/adk-python`)**: [https://github.com/google/adk-python](https://github.com/google/adk-python)
2. **Agent2Agent (A2A) Open Protocol**:
   - **A2A Protocol Specification & Docs**: [https://a2a-protocol.org/](https://a2a-protocol.org/)
   - **A2A Project & Python SDK (`a2aproject/A2A`)**: [https://github.com/a2aproject/A2A](https://github.com/a2aproject/A2A)
   - **A2A Protocol Inspector (Interactive Debugger)**: [https://github.com/a2aproject/a2a-inspector](https://github.com/a2aproject/a2a-inspector)
3. **Google Cloud Model Context Protocol (MCP) for Oracle Database@Google Cloud**:
   - **Oracle Database@Google Cloud Official MCP Server Reference**: [https://cloud.google.com/oracle/database/docs/reference/mcp](https://cloud.google.com/oracle/database/docs/reference/mcp)
   - **Managed MCP Endpoint**: `https://oracledatabase.googleapis.com/mcp` *(enables ADK and MCP-compatible agents to provision and inspect Autonomous Databases, Exadata Infrastructure, VM Clusters, and ODB Networks on Google Cloud)*.
