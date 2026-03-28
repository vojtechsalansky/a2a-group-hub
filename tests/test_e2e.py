# tests/test_e2e.py
"""End-to-end integration test: hub + REST API + fan-out with mock agents."""

import pytest
import httpx
from unittest.mock import AsyncMock, patch
from httpx import ASGITransport, AsyncClient

from src.hub.server import create_app
from src.hub.fanout import FanOutResult


@pytest.fixture
async def client():
    app = create_app(storage_backend="memory")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, app


class TestE2EFlow:
    """Full workflow: create channel -> add members -> send message -> verify response."""

    async def test_full_group_chat_flow(self, client):
        c, app = client
        hub = app.state.hub

        # Step 1: Create a channel
        resp = await c.post("/api/channels", json={"name": "dev-team", "channel_id": "dev"})
        assert resp.status_code == 201
        channel = resp.json()
        assert channel["channel_id"] == "dev"

        # Step 2: Add 3 members
        members = [
            {"agent_id": "apollo", "name": "Apollo", "url": "http://localhost:9001", "role": "owner"},
            {"agent_id": "rex", "name": "Rex", "url": "http://localhost:9002", "role": "member"},
            {"agent_id": "pixel", "name": "Pixel", "url": "http://localhost:9003", "role": "member"},
        ]
        for m in members:
            resp = await c.post("/api/channels/dev/members", json=m)
            assert resp.status_code == 201

        # Step 3: Verify channel has 3 members
        resp = await c.get("/api/channels/dev")
        assert resp.status_code == 200
        assert len(resp.json()["members"]) == 3

        # Step 4: Mock fan-out and send message through hub handler
        hub._fanout_engine.fan_out = AsyncMock(return_value=[
            FanOutResult(agent_id="rex", agent_name="Rex", response_text="[echo] Hello team"),
            FanOutResult(agent_id="pixel", agent_name="Pixel", response_text="[echo] Hello team"),
        ])

        from a2a.types import Message, MessageSendParams, Part, Role, TextPart
        params = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(root=TextPart(text="Hello team"))],
                message_id="msg-1",
                metadata={"channel_id": "dev", "sender_id": "apollo"},
            ),
        )
        result = await hub.on_message_send(params)

        # Step 5: Verify aggregated response
        from a2a.types import Task
        assert isinstance(result, Task)
        assert len(result.artifacts) == 2
        assert result.metadata["channel_id"] == "dev"
        assert result.metadata["success_count"] == 2

        # Step 6: Verify messages persisted
        storage = app.state.storage
        messages = await storage.get_messages("dev")
        # 1 incoming + 2 response = 3 messages
        assert len(messages) == 3

        # Step 7: Verify hub status reflects activity
        resp = await c.get("/api/status")
        assert resp.status_code == 200
        status = resp.json()
        assert status["channels"] == 1
        assert status["total_members"] == 3

    async def test_observer_flow(self, client):
        """Observer can be added but cannot send messages."""
        c, app = client
        hub = app.state.hub

        await c.post("/api/channels", json={"name": "dev", "channel_id": "dev"})
        await c.post("/api/channels/dev/members", json={
            "agent_id": "vigil", "name": "Vigil", "url": "http://localhost:9004", "role": "observer",
        })

        from a2a.types import Message, MessageSendParams, Part, Role, TextPart
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
        assert "observer" in result.parts[0].root.text.lower() or "permission" in result.parts[0].root.text.lower()

    async def test_channel_lifecycle(self, client):
        """Create, populate, query, and delete a channel."""
        c, _ = client

        # Create
        resp = await c.post("/api/channels", json={"name": "temp", "channel_id": "temp"})
        assert resp.status_code == 201

        # Add member
        await c.post("/api/channels/temp/members", json={
            "agent_id": "bot", "name": "Bot", "url": "http://localhost:9999",
        })

        # Verify
        resp = await c.get("/api/channels/temp")
        assert len(resp.json()["members"]) == 1

        # Modify
        resp = await c.patch("/api/channels/temp", json={"default_aggregation": "first"})
        assert resp.status_code == 200

        # Delete
        resp = await c.delete("/api/channels/temp")
        assert resp.status_code == 200

        # Verify gone
        resp = await c.get("/api/channels/temp")
        assert resp.status_code == 404

    async def test_multiple_channels_isolation(self, client):
        """Messages in one channel don't leak to another."""
        c, app = client
        hub = app.state.hub
        storage = app.state.storage

        # Create two channels
        await c.post("/api/channels", json={"name": "ch-a", "channel_id": "ch-a"})
        await c.post("/api/channels", json={"name": "ch-b", "channel_id": "ch-b"})

        await c.post("/api/channels/ch-a/members", json={
            "agent_id": "agent1", "name": "Agent1", "url": "http://localhost:8001", "role": "owner",
        })
        await c.post("/api/channels/ch-b/members", json={
            "agent_id": "agent2", "name": "Agent2", "url": "http://localhost:8002", "role": "owner",
        })

        hub._fanout_engine.fan_out = AsyncMock(return_value=[])

        from a2a.types import Message, MessageSendParams, Part, Role, TextPart
        params_a = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(root=TextPart(text="Hello A"))],
                message_id="msg-a",
                metadata={"channel_id": "ch-a", "sender_id": "agent1"},
            ),
        )
        await hub.on_message_send(params_a)

        # ch-a should have 1 message, ch-b should have 0
        msgs_a = await storage.get_messages("ch-a")
        msgs_b = await storage.get_messages("ch-b")
        assert len(msgs_a) == 1
        assert len(msgs_b) == 0
