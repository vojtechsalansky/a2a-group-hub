# A2A Group Chat Hub

Broadcasting hub that adds **group chat** to the [A2A (Agent-to-Agent) protocol](https://github.com/a2aproject/A2A).

A2A is a Google-led open protocol for agent-to-agent communication, but it is **point-to-point by design** — one client talks to one server. There is no native way for a group of agents to hold a shared conversation. This hub fills that gap.

---

## Table of Contents

- [The Problem](#the-problem)
- [How It Works](#how-it-works)
- [Architecture](#architecture)
- [Quickstart](#quickstart)
- [Creating Channels and Adding Agents](#creating-channels-and-adding-agents)
- [Sending Group Messages](#sending-group-messages)
- [Roles and Permissions](#roles-and-permissions)
- [Aggregation Strategies](#aggregation-strategies)
- [Agents](#agents)
- [REST Management API](#rest-management-api)
- [Webhooks](#webhooks)
- [Metrics](#metrics)
- [Storage Backends](#storage-backends)
- [Deployment Modes](#deployment-modes)
- [Configuration](#configuration)
- [Testing](#testing)
- [How This Relates to the A2A Protocol](#how-this-relates-to-the-a2a-protocol)
- [Project Structure](#project-structure)
- [Contributing](#contributing)
- [License](#license)

---

## The Problem

The [A2A protocol (v1.0)](https://github.com/a2aproject/A2A/blob/main/specification/a2a.proto) defines `SendMessage` as a bilateral client-to-server operation. If you want Agent A to ask a question and have Agents B, C, and D all respond, you need something in between.

There are [active discussions](https://github.com/a2aproject/A2A/issues/1029) in the A2A community about adding pub/sub and broadcast capabilities, but nothing is standardized yet. This hub provides a working solution today.

## How It Works

```
Agent A ──SendMessage──> ┌─────────────────┐ ──SendMessage──> Agent B
                         │  A2A Group Hub   │ ──SendMessage──> Agent C
                         │  (channel: dev)  │ ──SendMessage──> Agent D
Agent A <──aggregated────┤                  │ <──responses────┘
           response      └────────┬─────────┘
                                  │
                                  v
                           Storage Layer
                         (message history)
```

**The Hub is simultaneously:**
- **An A2A Server** — agents send messages TO the hub using standard A2A `SendMessage`
- **An A2A Client** — the hub forwards messages to all peer agents using standard A2A `SendMessage`

All messages within a channel share a single `contextId`, so every agent sees the same conversation thread — exactly how A2A intends multi-turn interactions to work.

**Step by step:**
1. Agent A sends a message to the Hub, targeting a channel (e.g. `"dev-team"`)
2. The Hub checks that Agent A has permission to send in that channel
3. The Hub saves the message to storage (for history and search)
4. The Hub sends the message to every other member of the channel (fan-out)
5. Each agent processes the message and responds independently
6. The Hub collects all responses and combines them into a single result (aggregation)
7. The Hub returns the aggregated result to Agent A

Observers (read-only members like auditors) receive the message but their responses are not waited for — fire-and-forget.

## Architecture

```
src/
├── hub/
│   ├── handler.py      # Core A2A RequestHandler — receives messages, orchestrates everything
│   ├── fanout.py        # Sends messages to all channel peers in parallel (asyncio.gather)
│   ├── aggregator.py    # Combines responses using configurable strategies
│   └── server.py        # Starlette web server — REST API + A2A protocol endpoint
│
├── channels/
│   ├── models.py        # Channel, ChannelMember, MemberRole (owner/member/observer)
│   ├── permissions.py   # Who can send, who can manage — role-based checks
│   └── registry.py      # Channel CRUD, backed by a storage backend
│
├── storage/
│   ├── base.py          # StorageBackend ABC — the interface all backends implement
│   ├── memory.py        # InMemoryBackend — dict-based, zero dependencies, for dev/testing
│   ├── qdrant.py        # (planned) QdrantBackend — message vectors + semantic search
│   ├── neo4j.py         # (planned) Neo4jBackend — channel graph + member relationships
│   └── composite.py     # (planned) Qdrant + Neo4j combined for production
│
├── notifications/
│   └── webhooks.py      # Fire-and-forget HTTP POST when events happen (new message, etc.)
│
└── observability/
    └── metrics.py       # Prometheus-compatible counters (/api/metrics endpoint)
```

The hub itself is **stateless** — all persistence is delegated to the storage backend. The default `InMemoryBackend` keeps everything in Python dicts (lost on restart). For production, use Qdrant (message history + semantic search) and Neo4j (channel/member graph).

## Quickstart

### Option 1: Local Python (zero dependencies)

```bash
# Clone and install
git clone https://github.com/filipzapletalcs/a2a-group-hub.git
cd a2a-group-hub
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Start the hub (in-memory storage, no external services needed)
python -m uvicorn src.hub.server:create_app --factory --host 0.0.0.0 --port 8000
```

In separate terminals, start some demo agents:

```bash
# Terminal 2: Echo agent (no LLM needed)
python -m agents.run_agent --role echo --port 9001

# Terminal 3: Another echo agent
python -m agents.run_agent --role echo --port 9002 --name "Agent Beta"

# Terminal 4: Yet another
python -m agents.run_agent --role echo --port 9003 --name "Agent Gamma"
```

### Option 2: Docker (full stack with Qdrant + Neo4j)

```bash
# Optional: set your API key for LLM-powered agents
export ANTHROPIC_API_KEY=sk-ant-...

# Start everything — hub, databases, and 3 LLM agents
docker compose up

# Hub is at localhost:8000
# Agents at localhost:9001 (researcher), 9002 (engineer), 9003 (critic)
```

### Verify it works

```bash
# Check hub status
curl http://localhost:8000/api/status

# Run the test suite (164 tests, including E2E with real agents)
python -m pytest -v
```

## Creating Channels and Adding Agents

A **channel** is a named group of A2A agents. All messages in a channel share a `contextId`, so agents maintain conversation context across multiple exchanges.

### Create a channel

```bash
curl -X POST http://localhost:8000/api/channels \
  -H "Content-Type: application/json" \
  -d '{
    "name": "dev-team",
    "channel_id": "dev",
    "default_aggregation": "all",
    "agent_timeout": 60
  }'
```

- `name` — human-readable name (required)
- `channel_id` — unique ID (optional, auto-generated if omitted)
- `default_aggregation` — how to combine responses: `all`, `first`, `voting`, `consensus`, `best_of_n` (default: `all`)
- `agent_timeout` — seconds to wait for each agent to respond (default: 60)

### Add agents to the channel

Each agent needs an A2A server URL — the address where the hub can reach it via `SendMessage`.

```bash
# Add a researcher agent as owner
curl -X POST http://localhost:8000/api/channels/dev/members \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "researcher",
    "name": "Research Agent",
    "url": "http://localhost:9001",
    "role": "owner"
  }'

# Add an engineer as regular member
curl -X POST http://localhost:8000/api/channels/dev/members \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "engineer",
    "name": "Engineer Agent",
    "url": "http://localhost:9002",
    "role": "member"
  }'

# Add an auditor as observer (receives messages, cannot send)
curl -X POST http://localhost:8000/api/channels/dev/members \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "auditor",
    "name": "Audit Agent",
    "url": "http://localhost:9003",
    "role": "observer"
  }'
```

### Verify the channel

```bash
curl http://localhost:8000/api/channels/dev
```

## Sending Group Messages

### From any A2A client

Any standard A2A client can send a message to the hub. Include `channel_id` in message metadata to target a channel:

```python
from a2a.client import A2AClient
from a2a.types import (
    Message, MessageSendParams, Part, Role,
    SendMessageRequest, TextPart,
)
import httpx

async with httpx.AsyncClient() as http:
    client = A2AClient(httpx_client=http, url="http://localhost:8000")

    request = SendMessageRequest(
        id="1",
        params=MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(root=TextPart(text="Team, what do you think about using Redis for caching?"))],
                message_id="msg-1",
                metadata={
                    "channel_id": "dev",         # which channel to broadcast to
                    "sender_id": "orchestrator",  # your ID (excluded from fan-out)
                },
            ),
        ),
    )

    response = await client.send_message(request)
    # response.root.result is a Task with one Artifact per responding agent
    task = response.root.result
    for artifact in task.artifacts:
        print(f"{artifact.name}: {artifact.parts[0].root.text}")
```

**What you get back:** A `Task` object containing one `Artifact` per responding agent. Each artifact has the agent's name and their response text.

### Streaming mode

Use streaming to get responses as each agent replies (first-come, first-served):

```python
async for event in client.send_message_streaming(request):
    if hasattr(event, 'artifact'):
        # An agent just responded
        print(f"{event.artifact.name}: {event.artifact.parts[0].root.text}")
    elif hasattr(event, 'status'):
        # Status update (working, completed)
        print(f"Status: {event.status.state}")
```

### Using curl (for testing)

```bash
# Send a JSON-RPC request directly
curl -X POST http://localhost:8000/ \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": "1",
    "method": "message/send",
    "params": {
      "message": {
        "role": "user",
        "messageId": "test-1",
        "parts": [{"kind": "text", "text": "Hello team!"}],
        "metadata": {"channel_id": "dev", "sender_id": "tester"}
      }
    }
  }'
```

## Roles and Permissions

Every channel member has a role that determines what they can do:

| Role | Send messages | Receive messages | Add/remove members | Delete channel |
|------|:---:|:---:|:---:|:---:|
| **owner** | yes | yes | yes | yes |
| **member** | yes | yes | no | no |
| **observer** | no | yes (fire-and-forget) | no | no |

**How observers work:** When the hub fans out a message, observers receive it too — but the hub does not wait for their response. This is useful for monitoring/auditing agents that need to see all communication without affecting the conversation flow.

**Example use case:** In a team of AI agents, the team lead is the `owner`, team members are `member`, and a quality auditor is an `observer` who silently monitors all conversations for compliance.

## Aggregation Strategies

When multiple agents respond, the hub needs to combine their answers. You choose how:

| Strategy | What it does | Waits for | Needs LLM on hub | Best for |
|----------|-------------|-----------|:-:|----------|
| **`all`** | Returns every agent's response as a separate artifact | All agents | No | Brainstorming, collecting diverse perspectives |
| **`first`** | Returns only the first agent to respond | First one | No | Quick answers, latency-sensitive queries |
| **`voting`** | Agents vote (their response text = their vote), majority wins | All agents | No | Binary decisions (approve/reject), polls |
| **`consensus`** | Compares responses semantically, returns the consensus + outliers | All agents | Yes | Fact-checking, verification |
| **`best_of_n`** | An LLM judge picks the best response | All agents | Yes | Getting the single highest-quality answer |

**Set per-channel** (default for all messages):
```bash
curl -X PATCH http://localhost:8000/api/channels/dev \
  -H "Content-Type: application/json" \
  -d '{"default_aggregation": "voting"}'
```

**Set per-message** (overrides channel default):
```json
{
  "metadata": {
    "channel_id": "dev",
    "aggregation": "first"
  }
}
```

**Note:** `consensus` and `best_of_n` require an `ANTHROPIC_API_KEY` on the hub itself (not just on agents). Without it, they gracefully fall back to `all` with a log warning.

## Agents

The hub doesn't care what kind of agent responds — any A2A-compliant server works. We include some pre-built agents for convenience:

### Demo Echo Agent

A simple agent that echoes back messages with a name tag. No LLM, no API key needed. Great for testing.

```bash
python -m agents.run_agent --role echo --port 9001
```

### LLM-Powered Agents (Claude)

Real AI agents backed by Claude via the Anthropic API. Each agent has a role, a system prompt, and maintains **conversation history per channel** (via `contextId`).

```bash
# Requires ANTHROPIC_API_KEY env var
export ANTHROPIC_API_KEY=sk-ant-...

python -m agents.run_agent --role researcher --port 9001
python -m agents.run_agent --role engineer --port 9002
python -m agents.run_agent --role critic --port 9003
```

**Pre-built roles:**

| Role | Temperature | What it does |
|------|:-----------:|-------------|
| `researcher` | 0.7 | Provides factual info, cites sources, identifies knowledge gaps |
| `engineer` | 0.5 | Technical solutions, code, architecture advice, pragmatic |
| `critic` | 0.8 | Finds flaws, identifies risks, plays devil's advocate, suggests fixes |

**Custom agents:**

```bash
python -m agents.run_agent \
  --name "Financial Analyst" \
  --port 9010 \
  --system-prompt "You are a financial analyst. Focus on ROI and risk." \
  --temperature 0.3
```

**Without an API key**, LLM agents automatically fall back to echo behavior — so you can test the full flow without spending any tokens.

### Using your own agents

Any A2A-compliant server works with this hub. The hub just sends standard `SendMessage` requests and expects standard responses. You can use:

- [LangGraph agents](https://github.com/a2aproject/A2A/tree/main/samples/python/agents/langgraph)
- [CrewAI agents](https://github.com/a2aproject/A2A/tree/main/samples/python/agents/crewai)
- [Google ADK agents](https://github.com/a2aproject/A2A/tree/main/samples/python/agents/google_adk)
- Any custom A2A server in Python, Go, Java, JS, or .NET

## REST Management API

Full CRUD for channels, members, and webhooks.

| Method | Endpoint | Description |
|--------|----------|-------------|
| **Channels** | | |
| `GET` | `/api/channels` | List all channels |
| `POST` | `/api/channels` | Create a channel |
| `GET` | `/api/channels/{id}` | Get channel details + members |
| `PATCH` | `/api/channels/{id}` | Update channel settings |
| `DELETE` | `/api/channels/{id}` | Delete a channel |
| **Members** | | |
| `POST` | `/api/channels/{id}/members` | Add a member to a channel |
| `PATCH` | `/api/channels/{id}/members/{agent_id}` | Change a member's role |
| `DELETE` | `/api/channels/{id}/members/{agent_id}` | Remove a member |
| **Webhooks** | | |
| `POST` | `/api/channels/{id}/webhooks` | Register a webhook |
| `GET` | `/api/channels/{id}/webhooks` | List webhooks |
| `DELETE` | `/api/channels/{id}/webhooks/{webhook_id}` | Delete a webhook |
| **System** | | |
| `GET` | `/api/status` | Hub status (channels, agents, messages) |
| `GET` | `/api/metrics` | Prometheus-compatible metrics |

See [docs/api-reference.md](docs/api-reference.md) for full request/response examples.

## Webhooks

Get notified when things happen in a channel:

```bash
# Register a webhook
curl -X POST http://localhost:8000/api/channels/dev/webhooks \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://your-server.com/webhook",
    "events": ["message", "member_join", "member_leave"]
  }'
```

**Events:** `message`, `member_join`, `member_leave`, `channel_created`

**Webhook payload:**
```json
{
  "event": "message",
  "channel_id": "dev",
  "timestamp": "2026-03-28T14:30:00Z",
  "data": {
    "sender_id": "researcher",
    "text": "I found three relevant papers...",
    "response_count": 3
  }
}
```

Webhooks are fire-and-forget — the hub POSTs and doesn't wait for a response. Failed deliveries are logged and counted in metrics.

**Use case:** Connect a Telegram bot that posts channel messages to a Telegram group, or trigger CI/CD pipelines when agents reach consensus.

## Metrics

Prometheus-compatible metrics at `/api/metrics`:

```
# TYPE a2a_hub_channels_total gauge
a2a_hub_channels_total 5

# TYPE a2a_hub_messages_total counter
a2a_hub_messages_total 1247

# TYPE a2a_hub_fanout_duration_seconds summary
a2a_hub_fanout_duration_seconds{quantile="0.95"} 2.3

# TYPE a2a_hub_agent_errors_total counter
a2a_hub_agent_errors_total{agent="nova"} 2

# TYPE a2a_hub_webhook_deliveries_total counter
a2a_hub_webhook_deliveries_total{status="success"} 412
```

## Storage Backends

| Backend | Dependencies | Persistence | Semantic Search | Use case |
|---------|:---:|:---:|:---:|---------|
| **InMemoryBackend** (default) | None | No (lost on restart) | Substring only | Development, testing, demos |
| **CompositeBackend** (planned) | Qdrant + Neo4j | Yes | Yes (FastEmbed vectors) | Production |

The `StorageBackend` interface is defined in `src/storage/base.py`. To add your own backend (Redis, PostgreSQL, etc.), implement the abstract methods and pass it to the hub.

**Switching backends:**
```bash
# In-memory (default)
STORAGE_BACKEND=memory python -m uvicorn src.hub.server:create_app --factory

# Composite (when implemented)
STORAGE_BACKEND=composite \
  QDRANT_URL=http://localhost:6333 \
  NEO4J_URL=bolt://localhost:7687 \
  python -m uvicorn src.hub.server:create_app --factory
```

## Deployment Modes

### 1. Full stack (Docker)

Everything in one command — hub, databases, and three LLM agents:

```bash
docker compose up
```

### 2. Hub + storage only

Bring your own agents:

```bash
docker compose up hub qdrant neo4j
```

### 3. Zero dependencies

Just the hub with in-memory storage — no Docker, no databases:

```bash
pip install -e .
STORAGE_BACKEND=memory python -m uvicorn src.hub.server:create_app --factory
```

## Configuration

Copy `.env.example` to `.env` and adjust:

| Variable | Default | Description |
|----------|---------|-------------|
| `STORAGE_BACKEND` | `memory` | `memory` or `composite` |
| `QDRANT_URL` | `http://localhost:6333` | Qdrant vector database endpoint |
| `NEO4J_URL` | `bolt://localhost:7687` | Neo4j graph database endpoint |
| `NEO4J_USER` | `neo4j` | Neo4j username |
| `NEO4J_PASSWORD` | `password` | Neo4j password |
| `ANTHROPIC_API_KEY` | — | Required for LLM agents and `consensus`/`best_of_n` strategies |
| `HUB_HOST` | `0.0.0.0` | Hub bind address |
| `HUB_PORT` | `8000` | Hub port |

## Testing

```bash
pip install -e ".[dev]"
python -m pytest -v
```

**164 tests** covering:
- Channel models, permissions, and registry
- Storage backend interface (InMemory)
- Fan-out engine with observer handling
- All 5 aggregation strategies (including edge cases)
- Hub handler (blocking + streaming)
- REST API endpoints
- Webhook dispatcher
- Metrics collector
- **End-to-end tests** with real agent subprocesses making real HTTP calls

## How This Relates to the A2A Protocol

This project builds on top of the [A2A protocol](https://github.com/a2aproject/A2A) without modifying it. Here's how standard A2A primitives are used:

| A2A Concept | How the Hub Uses It |
|---|---|
| [`SendMessage`](https://github.com/a2aproject/A2A/blob/main/specification/a2a.proto) | Inbound from sender, outbound fan-out to all peers |
| `contextId` | Shared across the entire channel — all agents see the same conversation |
| `Task` + `Artifact` | Each peer's response becomes a separate Artifact on the aggregated Task |
| [`Agent Card`](https://github.com/a2aproject/A2A/blob/main/docs/topics/agent_discovery.md) | Hub publishes its own card at `/.well-known/agent-card.json` |
| `SSE Streaming` | `on_message_send_stream` yields artifacts as each peer responds |
| `metadata` | `channel_id` and `sender_id` in message metadata route to the right channel |

**What A2A doesn't have (and this hub adds):**
- **Multicast/fan-out** — A2A has no way to send to a group
- **Channel/room primitive** — A2A has no concept of named groups
- **Response aggregation** — A2A returns one Task per SendMessage; the hub merges N responses
- **Role-based access** — A2A has no built-in permissions for group communication

**Related A2A discussions:**
- [Issue #1029: Publish/Subscribe Methods](https://github.com/a2aproject/A2A/issues/1029) — community proposal for pub/sub
- [Issue #370: Task Broadcast Capability](https://github.com/a2aproject/A2A/issues/370) — request for multicast
- [Issue #1593: Built-in Pub/Sub Support](https://github.com/a2aproject/A2A/issues/1593) — feature request for native pub/sub

## Project Structure

```
a2a-group-hub/
├── src/
│   ├── hub/                    # Core hub logic
│   │   ├── handler.py          # A2A RequestHandler — the brain
│   │   ├── fanout.py           # Parallel message dispatch
│   │   ├── aggregator.py       # Response combination strategies
│   │   └── server.py           # Starlette web app
│   ├── channels/               # Channel management
│   │   ├── models.py           # Data models + roles
│   │   ├── permissions.py      # Role-based access checks
│   │   └── registry.py         # CRUD operations
│   ├── storage/                # Pluggable persistence
│   │   ├── base.py             # Abstract interface
│   │   └── memory.py           # In-memory implementation
│   ├── notifications/          # Event notifications
│   │   └── webhooks.py         # Webhook dispatcher
│   └── observability/          # Monitoring
│       └── metrics.py          # Prometheus metrics
├── agents/                     # Pre-built A2A agent servers
│   ├── demo_agent.py           # Echo agent (no LLM)
│   ├── llm_agent.py            # Claude-powered agent
│   ├── agents_config.py        # Role definitions
│   └── run_agent.py            # CLI launcher
├── tests/                      # 164 tests
├── docs/
│   ├── architecture.md         # Detailed design
│   └── api-reference.md        # Full API docs
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
└── .env.example
```

## Contributing

1. Fork the repo
2. Create a feature branch
3. Write tests first (TDD)
4. Run `python -m pytest -v` and `ruff check .`
5. Submit a PR

## License

MIT
