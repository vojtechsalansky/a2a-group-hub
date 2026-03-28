# tests/test_e2e.py
"""End-to-end integration tests.

Two test classes:
  - TestE2EInProcess: fast tests using ASGI transport (no real servers)
  - TestE2ELiveAgents: start real demo agent servers as subprocesses,
    hub uses ASGI transport but makes real HTTP calls to agents via fan-out.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

import pytest
import httpx
from httpx import ASGITransport, AsyncClient

from a2a.types import (
    Message, MessageSendParams, Part, Role, Task, TextPart,
    TaskStatusUpdateEvent, TaskArtifactUpdateEvent,
)

from src.hub.fanout import FanOutResult
from src.hub.server import create_app


# ── In-process tests (ASGI transport, no real servers) ─────────────────────


@pytest.fixture
async def asgi_client():
    app = create_app(storage_backend="memory")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, app


class TestE2EInProcess:
    """Fast E2E tests using ASGI transport (no real network)."""

    async def test_full_group_chat_flow(self, asgi_client):
        c, app = asgi_client
        hub = app.state.hub

        # Create channel + add 3 members
        resp = await c.post("/api/channels", json={"name": "dev-team", "channel_id": "dev"})
        assert resp.status_code == 201

        for m in [
            {"agent_id": "apollo", "name": "Apollo", "url": "http://localhost:9001", "role": "owner"},
            {"agent_id": "rex", "name": "Rex", "url": "http://localhost:9002", "role": "member"},
            {"agent_id": "pixel", "name": "Pixel", "url": "http://localhost:9003", "role": "member"},
        ]:
            assert (await c.post("/api/channels/dev/members", json=m)).status_code == 201

        # Verify 3 members
        resp = await c.get("/api/channels/dev")
        assert len(resp.json()["members"]) == 3

        # Mock fan-out and send message
        from unittest.mock import AsyncMock
        hub._fanout_engine.fan_out = AsyncMock(return_value=[
            FanOutResult(agent_id="rex", agent_name="Rex", response_text="[echo] Hello team"),
            FanOutResult(agent_id="pixel", agent_name="Pixel", response_text="[echo] Hello team"),
        ])

        params = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(root=TextPart(text="Hello team"))],
                message_id="msg-1",
                metadata={"channel_id": "dev", "sender_id": "apollo"},
            ),
        )
        result = await hub.on_message_send(params)
        assert isinstance(result, Task)
        assert len(result.artifacts) == 2
        assert result.metadata["success_count"] == 2

        # Verify persistence
        messages = await app.state.storage.get_messages("dev")
        assert len(messages) == 3  # 1 incoming + 2 responses

    async def test_observer_cannot_send(self, asgi_client):
        c, app = asgi_client
        hub = app.state.hub

        await c.post("/api/channels", json={"name": "dev", "channel_id": "dev"})
        await c.post("/api/channels/dev/members", json={
            "agent_id": "vigil", "name": "Vigil", "url": "http://localhost:9004", "role": "observer",
        })

        params = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(root=TextPart(text="I'm watching"))],
                message_id="msg-2",
                metadata={"channel_id": "dev", "sender_id": "vigil"},
            ),
        )
        result = await hub.on_message_send(params)
        assert isinstance(result, Message)
        text = result.parts[0].root.text.lower()
        assert "observer" in text or "permission" in text

    async def test_channel_lifecycle(self, asgi_client):
        c, _ = asgi_client
        assert (await c.post("/api/channels", json={"name": "temp", "channel_id": "temp"})).status_code == 201
        await c.post("/api/channels/temp/members", json={"agent_id": "bot", "name": "Bot", "url": "http://localhost:9999"})
        assert len((await c.get("/api/channels/temp")).json()["members"]) == 1
        assert (await c.patch("/api/channels/temp", json={"default_aggregation": "first"})).status_code == 200
        assert (await c.delete("/api/channels/temp")).status_code == 200
        assert (await c.get("/api/channels/temp")).status_code == 404

    async def test_multiple_channels_isolation(self, asgi_client):
        c, app = asgi_client
        hub = app.state.hub
        storage = app.state.storage

        await c.post("/api/channels", json={"name": "ch-a", "channel_id": "ch-a"})
        await c.post("/api/channels", json={"name": "ch-b", "channel_id": "ch-b"})
        await c.post("/api/channels/ch-a/members", json={"agent_id": "a1", "name": "A1", "url": "http://localhost:8001", "role": "owner"})
        await c.post("/api/channels/ch-b/members", json={"agent_id": "a2", "name": "A2", "url": "http://localhost:8002", "role": "owner"})

        from unittest.mock import AsyncMock
        hub._fanout_engine.fan_out = AsyncMock(return_value=[])

        params = MessageSendParams(
            message=Message(role=Role.user, parts=[Part(root=TextPart(text="Hello A"))],
                            message_id="msg-a", metadata={"channel_id": "ch-a", "sender_id": "a1"}),
        )
        await hub.on_message_send(params)
        assert len(await storage.get_messages("ch-a")) == 1
        assert len(await storage.get_messages("ch-b")) == 0

    async def test_streaming_mode(self, asgi_client):
        """Test the streaming handler yields status updates and artifacts."""
        _, app = asgi_client
        hub = app.state.hub
        registry = app.state.registry

        await registry.create_channel(name="stream-test", channel_id="stream")
        from src.channels.models import ChannelMember, MemberRole
        await registry.add_member("stream", ChannelMember(
            agent_id="owner", name="Owner", url="http://localhost:9001", role=MemberRole.owner))
        await registry.add_member("stream", ChannelMember(
            agent_id="agent1", name="Agent1", url="http://localhost:9002", role=MemberRole.member))

        from unittest.mock import AsyncMock
        hub._fanout_engine.fan_out = AsyncMock(return_value=[
            FanOutResult(agent_id="agent1", agent_name="Agent1", response_text="Streamed reply"),
        ])

        params = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(root=TextPart(text="Stream test"))],
                message_id="msg-stream",
                metadata={"channel_id": "stream", "sender_id": "owner"},
            ),
        )

        events = []
        async for event in hub.on_message_send_stream(params):
            events.append(event)

        # Should get: 1 working status, 1 artifact, 1 completed status
        assert len(events) == 3
        assert isinstance(events[0], TaskStatusUpdateEvent)
        assert events[0].status.state.value == "working"
        assert isinstance(events[1], TaskArtifactUpdateEvent)
        assert "Streamed reply" in events[1].artifact.parts[0].root.text
        assert isinstance(events[2], TaskStatusUpdateEvent)
        assert events[2].status.state.value == "completed"

    async def test_unknown_channel_returns_error(self, asgi_client):
        _, app = asgi_client
        hub = app.state.hub
        params = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(root=TextPart(text="Should fail"))],
                message_id="e2e-err-1",
                metadata={"channel_id": "nonexistent"},
            ),
        )
        result = await hub.on_message_send(params)
        assert isinstance(result, Message)
        assert "error" in result.parts[0].root.text.lower()


# ── Live agent tests (real agent subprocesses, real HTTP fan-out) ──────────


AGENT_PORTS = [19001, 19002, 19003]
AGENT_NAMES = ["rex", "pixel", "nova"]


def _wait_for_port(port: int, timeout: float = 10.0) -> bool:
    """Synchronously wait for a TCP port to accept connections."""
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.1)
    return False


@pytest.fixture(scope="module")
def agent_processes():
    """Start 3 demo echo agents as subprocesses on real ports."""
    procs = []
    for name, port in zip(AGENT_NAMES, AGENT_PORTS):
        proc = subprocess.Popen(
            [sys.executable, "-m", "agents.run_agent", "--role", "echo", "--port", str(port), "--name", name, "--log-level", "WARNING"],
            cwd=str(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(proc)

    # Wait for all agents to be reachable
    all_ready = all(_wait_for_port(port) for port in AGENT_PORTS)
    if not all_ready:
        for p in procs:
            p.terminate()
        pytest.skip("Could not start agent subprocesses (ports may be in use)")

    yield procs

    for p in procs:
        p.terminate()
        p.wait(timeout=5)


@pytest.fixture
async def hub_with_live_agents(agent_processes):
    """Hub app using ASGI transport but with real agents on the network."""
    app = create_app(storage_backend="memory")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, app


class TestE2ELiveAgents:
    """E2E tests with real agent subprocesses. Hub fan-out makes real HTTP calls."""

    async def test_full_live_flow_with_3_agents(self, hub_with_live_agents):
        """
        Full flow:
        1. Create channel via REST
        2. Add 3 real echo agents as members
        3. Send A2A message via hub handler (real fan-out to real agents)
        4. Verify 3 artifacts in aggregated response
        5. Verify messages persisted
        """
        c, app = hub_with_live_agents
        hub = app.state.hub
        storage = app.state.storage

        # Step 1: Create channel via REST
        resp = await c.post("/api/channels", json={"name": "live-team", "channel_id": "live"})
        assert resp.status_code == 201

        # Step 2: Add sender (owner) + 3 real agents
        await c.post("/api/channels/live/members", json={
            "agent_id": "sender", "name": "Sender", "url": "http://127.0.0.1:19999", "role": "owner",
        })
        for name, port in zip(AGENT_NAMES, AGENT_PORTS):
            resp = await c.post("/api/channels/live/members", json={
                "agent_id": name, "name": name.capitalize(),
                "url": f"http://127.0.0.1:{port}/", "role": "member",
            })
            assert resp.status_code == 201

        # Step 3: Verify 4 members
        resp = await c.get("/api/channels/live")
        assert len(resp.json()["members"]) == 4

        # Step 4: Send message through hub (real fan-out to real agents)
        params = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(root=TextPart(text="Hello from E2E test!"))],
                message_id="e2e-msg-1",
                metadata={"channel_id": "live", "sender_id": "sender"},
            ),
        )
        result = await hub.on_message_send(params)

        # Step 5: Verify 3 artifacts (one per agent)
        assert isinstance(result, Task)
        assert len(result.artifacts) == 3
        assert result.metadata["success_count"] == 3
        assert result.metadata["error_count"] == 0

        # Each should contain echo text
        for artifact in result.artifacts:
            text = artifact.parts[0].root.text
            assert "[echo]" in text.lower() or "echo" in text.lower()

        # Step 6: Verify persistence (1 incoming + 3 responses)
        messages = await storage.get_messages("live")
        assert len(messages) == 4

        # Step 7: Hub status
        resp = await c.get("/api/status")
        assert resp.json()["status"] == "running"

    async def test_live_streaming_with_real_agents(self, hub_with_live_agents):
        """Test streaming mode with real agents producing real echo responses."""
        c, app = hub_with_live_agents
        hub = app.state.hub

        await c.post("/api/channels", json={"name": "stream-live", "channel_id": "stream-live"})
        await c.post("/api/channels/stream-live/members", json={
            "agent_id": "streamer", "name": "Streamer", "url": "http://127.0.0.1:19999", "role": "owner",
        })
        for name, port in zip(AGENT_NAMES[:2], AGENT_PORTS[:2]):
            await c.post("/api/channels/stream-live/members", json={
                "agent_id": f"s-{name}", "name": name.capitalize(),
                "url": f"http://127.0.0.1:{port}/", "role": "member",
            })

        params = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(root=TextPart(text="Stream E2E test"))],
                message_id="e2e-stream-1",
                metadata={"channel_id": "stream-live", "sender_id": "streamer"},
            ),
        )

        events = []
        async for event in hub.on_message_send_stream(params):
            events.append(event)

        # working status + 2 artifacts + completed status = 4
        assert len(events) == 4
        assert isinstance(events[0], TaskStatusUpdateEvent)
        assert events[0].status.state.value == "working"
        assert isinstance(events[1], TaskArtifactUpdateEvent)
        assert isinstance(events[2], TaskArtifactUpdateEvent)
        assert isinstance(events[3], TaskStatusUpdateEvent)
        assert events[3].status.state.value == "completed"

        # Artifacts contain real echo responses
        for e in events[1:3]:
            assert "[echo]" in e.artifact.parts[0].root.text.lower() or "echo" in e.artifact.parts[0].root.text.lower()

    async def test_agent_down_returns_error_result(self, hub_with_live_agents):
        """If an agent URL is unreachable, fan-out returns error in the result."""
        c, app = hub_with_live_agents
        hub = app.state.hub

        await c.post("/api/channels", json={"name": "err-test", "channel_id": "err-test"})
        await c.post("/api/channels/err-test/members", json={
            "agent_id": "sender", "name": "Sender", "url": "http://127.0.0.1:19999", "role": "owner",
        })
        # Add an agent on a port that nobody is listening on
        await c.post("/api/channels/err-test/members", json={
            "agent_id": "dead-agent", "name": "DeadAgent",
            "url": "http://127.0.0.1:19999/", "role": "member",
        })

        params = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(root=TextPart(text="Anyone there?"))],
                message_id="e2e-err-2",
                metadata={"channel_id": "err-test", "sender_id": "sender"},
            ),
        )
        result = await hub.on_message_send(params)
        assert isinstance(result, Task)
        # Should have 1 artifact with an error
        assert len(result.artifacts) == 1
        assert result.metadata["error_count"] == 1
