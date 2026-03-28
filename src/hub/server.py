# src/hub/server.py
"""Starlette app with REST management API and A2A endpoint."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.channels.models import Channel, ChannelMember, MemberRole
from src.channels.registry import ChannelRegistry
from src.hub.handler import GroupChatHub
from src.storage.memory import InMemoryBackend
from src.storage.base import StorageBackend

logger = logging.getLogger("a2a-hub.server")


def _serialize_channel(ch: Channel) -> dict:
    """Serialize a Channel to a JSON-safe dict."""
    return {
        "channel_id": ch.channel_id,
        "name": ch.name,
        "context_id": ch.context_id,
        "created_at": ch.created_at.isoformat(),
        "message_count": ch.message_count,
        "default_aggregation": ch.default_aggregation,
        "agent_timeout": ch.agent_timeout,
        "members": {
            mid: {
                "agent_id": m.agent_id,
                "name": m.name,
                "url": m.url,
                "role": m.role.value,
                "joined_at": m.joined_at.isoformat(),
            }
            for mid, m in ch.members.items()
        },
        "metadata": ch.metadata,
    }


def _serialize_member(m: ChannelMember) -> dict:
    return {
        "agent_id": m.agent_id,
        "name": m.name,
        "url": m.url,
        "role": m.role.value,
        "joined_at": m.joined_at.isoformat(),
    }


def create_app(storage_backend: str = "memory") -> Starlette:
    """Factory that builds the full Starlette application."""

    # Storage
    if storage_backend == "memory":
        storage: StorageBackend = InMemoryBackend()
    else:
        logger.warning(
            f"Requested storage backend '{storage_backend}' is not available. "
            "Falling back to in-memory backend. Install qdrant/neo4j extras for production use."
        )
        storage = InMemoryBackend()

    registry = ChannelRegistry(storage)
    hub = GroupChatHub(registry=registry, storage=storage)

    # -- REST route handlers ------------------------------------------------

    async def create_channel(request: Request) -> JSONResponse:
        body = await request.json()
        if "name" not in body:
            return JSONResponse({"error": "Missing required field: name"}, status_code=400)
        ch = await registry.create_channel(
            name=body["name"],
            channel_id=body.get("channel_id"),
            default_aggregation=body.get("default_aggregation", "all"),
            agent_timeout=body.get("agent_timeout", 60),
        )
        return JSONResponse(_serialize_channel(ch), status_code=201)

    async def list_channels(request: Request) -> JSONResponse:
        channels = await registry.list_channels()
        return JSONResponse([_serialize_channel(ch) for ch in channels])

    async def get_channel(request: Request) -> JSONResponse:
        channel_id = request.path_params["channel_id"]
        ch = await registry.get_channel(channel_id)
        if not ch:
            return JSONResponse({"error": "Channel not found"}, status_code=404)
        return JSONResponse(_serialize_channel(ch))

    async def delete_channel(request: Request) -> JSONResponse:
        channel_id = request.path_params["channel_id"]
        deleted = await registry.delete_channel(channel_id)
        if not deleted:
            return JSONResponse({"error": "Channel not found"}, status_code=404)
        return JSONResponse({"deleted": True})

    async def patch_channel(request: Request) -> JSONResponse:
        channel_id = request.path_params["channel_id"]
        ch = await registry.get_channel(channel_id)
        if not ch:
            return JSONResponse({"error": "Channel not found"}, status_code=404)
        body = await request.json()
        if "default_aggregation" in body:
            ch.default_aggregation = body["default_aggregation"]
        if "agent_timeout" in body:
            ch.agent_timeout = body["agent_timeout"]
        if "name" in body:
            ch.name = body["name"]
        await storage.save_channel(ch)
        return JSONResponse(_serialize_channel(ch))

    async def add_member(request: Request) -> JSONResponse:
        channel_id = request.path_params["channel_id"]
        ch = await registry.get_channel(channel_id)
        if not ch:
            return JSONResponse({"error": "Channel not found"}, status_code=404)
        body = await request.json()
        missing = [f for f in ("agent_id", "name", "url") if f not in body]
        if missing:
            return JSONResponse(
                {"error": f"Missing required field(s): {', '.join(missing)}"},
                status_code=400,
            )
        role = MemberRole(body.get("role", "member"))
        member = ChannelMember(
            agent_id=body["agent_id"],
            name=body["name"],
            url=body["url"],
            role=role,
            auth_token=body.get("auth_token"),
        )
        await registry.add_member(channel_id, member)
        return JSONResponse(_serialize_member(member), status_code=201)

    async def remove_member(request: Request) -> JSONResponse:
        channel_id = request.path_params["channel_id"]
        agent_id = request.path_params["agent_id"]
        removed = await registry.remove_member(channel_id, agent_id)
        if not removed:
            return JSONResponse({"error": "Member not found"}, status_code=404)
        return JSONResponse({"removed": True})

    async def patch_member(request: Request) -> JSONResponse:
        channel_id = request.path_params["channel_id"]
        agent_id = request.path_params["agent_id"]
        ch = await registry.get_channel(channel_id)
        if not ch:
            return JSONResponse({"error": "Channel not found"}, status_code=404)
        member = ch.members.get(agent_id)
        if not member:
            return JSONResponse({"error": "Member not found"}, status_code=404)
        body = await request.json()
        if "role" in body:
            member.role = MemberRole(body["role"])
        if "name" in body:
            member.name = body["name"]
        if "url" in body:
            member.url = body["url"]
        return JSONResponse(_serialize_member(member))

    async def hub_status(request: Request) -> JSONResponse:
        channels = await registry.list_channels()
        total_members = sum(len(ch.members) for ch in channels)
        return JSONResponse({
            "status": "running",
            "channels": len(channels),
            "total_members": total_members,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    # -- Build app ----------------------------------------------------------

    routes = [
        Route("/api/channels", create_channel, methods=["POST"]),
        Route("/api/channels", list_channels, methods=["GET"]),
        Route("/api/channels/{channel_id}", get_channel, methods=["GET"]),
        Route("/api/channels/{channel_id}", delete_channel, methods=["DELETE"]),
        Route("/api/channels/{channel_id}", patch_channel, methods=["PATCH"]),
        Route("/api/channels/{channel_id}/members", add_member, methods=["POST"]),
        Route("/api/channels/{channel_id}/members/{agent_id}", remove_member, methods=["DELETE"]),
        Route("/api/channels/{channel_id}/members/{agent_id}", patch_member, methods=["PATCH"]),
        Route("/api/status", hub_status, methods=["GET"]),
    ]

    middleware = [
        Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]),
    ]

    app = Starlette(routes=routes, middleware=middleware)

    # Store references for A2A integration
    app.state.hub = hub
    app.state.registry = registry
    app.state.storage = storage

    return app
