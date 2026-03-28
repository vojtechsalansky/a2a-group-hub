# tests/test_api.py
"""Tests for REST management API."""

import pytest
from httpx import ASGITransport, AsyncClient

from src.hub.server import create_app


@pytest.fixture
async def client():
    app = create_app(storage_backend="memory")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


class TestChannelAPI:
    async def test_create_channel(self, client):
        resp = await client.post("/api/channels", json={"name": "dev-team", "channel_id": "dev"})
        assert resp.status_code == 201
        assert resp.json()["channel_id"] == "dev"

    async def test_list_channels(self, client):
        await client.post("/api/channels", json={"name": "dev", "channel_id": "dev"})
        resp = await client.get("/api/channels")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    async def test_get_channel(self, client):
        await client.post("/api/channels", json={"name": "dev", "channel_id": "dev"})
        resp = await client.get("/api/channels/dev")
        assert resp.status_code == 200
        assert resp.json()["name"] == "dev"

    async def test_get_nonexistent_channel(self, client):
        resp = await client.get("/api/channels/nope")
        assert resp.status_code == 404

    async def test_delete_channel(self, client):
        await client.post("/api/channels", json={"name": "dev", "channel_id": "dev"})
        resp = await client.delete("/api/channels/dev")
        assert resp.status_code == 200

    async def test_patch_channel(self, client):
        await client.post("/api/channels", json={"name": "dev", "channel_id": "dev"})
        resp = await client.patch("/api/channels/dev", json={"default_aggregation": "first"})
        assert resp.status_code == 200


class TestMemberAPI:
    async def test_add_member(self, client):
        await client.post("/api/channels", json={"name": "dev", "channel_id": "dev"})
        resp = await client.post("/api/channels/dev/members", json={
            "agent_id": "rex", "name": "Rex", "url": "http://localhost:9001", "role": "member",
        })
        assert resp.status_code == 201

    async def test_remove_member(self, client):
        await client.post("/api/channels", json={"name": "dev", "channel_id": "dev"})
        await client.post("/api/channels/dev/members", json={
            "agent_id": "rex", "name": "Rex", "url": "http://localhost:9001",
        })
        resp = await client.delete("/api/channels/dev/members/rex")
        assert resp.status_code == 200

    async def test_patch_member_role(self, client):
        await client.post("/api/channels", json={"name": "dev", "channel_id": "dev"})
        await client.post("/api/channels/dev/members", json={
            "agent_id": "rex", "name": "Rex", "url": "http://localhost:9001",
        })
        resp = await client.patch("/api/channels/dev/members/rex", json={"role": "observer"})
        assert resp.status_code == 200


class TestValidationAPI:
    async def test_create_channel_missing_name(self, client):
        resp = await client.post("/api/channels", json={"channel_id": "dev"})
        assert resp.status_code == 400
        assert "name" in resp.json()["error"]

    async def test_add_member_missing_required_fields(self, client):
        await client.post("/api/channels", json={"name": "dev", "channel_id": "dev"})
        resp = await client.post("/api/channels/dev/members", json={"agent_id": "rex"})
        assert resp.status_code == 400
        assert "name" in resp.json()["error"]
        assert "url" in resp.json()["error"]

    async def test_add_member_missing_agent_id(self, client):
        await client.post("/api/channels", json={"name": "dev", "channel_id": "dev"})
        resp = await client.post("/api/channels/dev/members", json={"name": "Rex", "url": "http://localhost:9001"})
        assert resp.status_code == 400
        assert "agent_id" in resp.json()["error"]


class TestStatusAPI:
    async def test_hub_status(self, client):
        resp = await client.get("/api/status")
        assert resp.status_code == 200
        assert resp.json()["status"] == "running"
