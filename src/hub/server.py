# src/hub/server.py
"""Starlette app with REST management API and A2A endpoint."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from a2a.server.apps.jsonrpc.starlette_app import A2AStarletteApplication
from a2a.types import AgentCapabilities, AgentCard, AgentSkill

from src.channels.models import Channel, ChannelMember, MemberRole
from src.channels.registry import ChannelRegistry
from src.hub.handler import GroupChatHub
from src.storage.memory import InMemoryBackend
from src.storage.base import StorageBackend
from src.bootstrap import bootstrap_channels

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


def create_app(storage_backend: str | None = None) -> Starlette:
    """Factory that builds the full Starlette application."""

    # Resolve backend from arg or env (uvicorn --factory calls with no args)
    backend = storage_backend or os.environ.get("STORAGE_BACKEND", "memory")

    # Storage
    if backend == "composite":
        from src.storage.neo4j_backend import Neo4jBackend
        from src.storage.qdrant import QdrantBackend
        from src.storage.composite import CompositeBackend

        neo4j = Neo4jBackend(
            url=os.environ.get("NEO4J_URL", "bolt://localhost:7687"),
            user=os.environ.get("NEO4J_USER", "neo4j"),
            password=os.environ.get("NEO4J_PASSWORD", "password"),
        )
        qdrant = QdrantBackend(
            url=os.environ.get("QDRANT_URL", "http://localhost:6333"),
        )
        storage: StorageBackend = CompositeBackend(neo4j_backend=neo4j, qdrant_backend=qdrant)
    elif backend == "sqlite":
        from src.storage.sqlite_backend import SqliteBackend
        db_path = os.environ.get("SQLITE_DB_PATH", "data/a2a-hub.db")
        storage = SqliteBackend(db_path=db_path)
    elif backend == "memory":
        storage = InMemoryBackend()
    else:
        logger.warning(
            "Requested storage backend '%s' is not available. "
            "Falling back to in-memory backend. Install qdrant/neo4j extras for production use.",
            backend,
        )
        storage = InMemoryBackend()

    registry = ChannelRegistry(storage)

    from src.notifications.webhooks import WebhookDispatcher
    webhook_dispatcher = WebhookDispatcher()
    hub = GroupChatHub(registry=registry, storage=storage, webhook_dispatcher=webhook_dispatcher)

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

    # -- Webhook API routes -------------------------------------------------

    async def create_webhook(request: Request) -> JSONResponse:
        channel_id = request.path_params["channel_id"]
        ch = await registry.get_channel(channel_id)
        if not ch:
            return JSONResponse({"error": "Channel not found"}, status_code=404)
        body = await request.json()
        from src.storage.base import Webhook
        import uuid as _uuid
        wh = Webhook(
            webhook_id=body.get("webhook_id", str(_uuid.uuid4())[:8]),
            url=body["url"],
            events=body.get("events", ["message"]),
        )
        await storage.save_webhook(channel_id, wh)
        return JSONResponse({"webhook_id": wh.webhook_id, "url": wh.url, "events": wh.events}, status_code=201)

    async def list_webhooks(request: Request) -> JSONResponse:
        channel_id = request.path_params["channel_id"]
        webhooks = await storage.list_webhooks(channel_id)
        return JSONResponse([{"webhook_id": w.webhook_id, "url": w.url, "events": w.events} for w in webhooks])

    async def delete_webhook(request: Request) -> JSONResponse:
        channel_id = request.path_params["channel_id"]
        webhook_id = request.path_params["webhook_id"]
        deleted = await storage.delete_webhook(channel_id, webhook_id)
        if not deleted:
            return JSONResponse({"error": "Webhook not found"}, status_code=404)
        return JSONResponse({"deleted": True})

    # -- Telegram webhook endpoint ---------------------------------------------

    telegram_bridge = None

    async def telegram_webhook(request: Request) -> JSONResponse:
        """Receive Telegram Bot API webhook updates."""
        if telegram_bridge is None:
            return JSONResponse({"error": "Telegram bridge not enabled"}, status_code=503)
        try:
            from telegram import Update
            body = await request.json()
            update = Update.de_json(body, telegram_bridge._bot)
            if update and update.message:
                await telegram_bridge.on_telegram_message(update)
            return JSONResponse({"ok": True})
        except Exception:
            logger.exception("Error processing Telegram webhook")
            return JSONResponse({"error": "Processing failed"}, status_code=500)

    # -- Message history & send endpoints -----------------------------------

    async def get_channel_messages(request: Request) -> JSONResponse:
        """GET /api/channels/{channel_id}/messages — Return message history."""
        channel_id = request.path_params["channel_id"]
        limit = int(request.query_params.get("limit", "50"))
        offset = int(request.query_params.get("offset", "0"))
        messages = await storage.get_messages(channel_id, limit=limit, offset=offset)
        return JSONResponse([{
            "message_id": m.message_id,
            "channel_id": m.channel_id,
            "sender_id": m.sender_id,
            "text": m.text,
            "context_id": m.context_id,
            "timestamp": m.timestamp.isoformat(),
            "metadata": m.metadata,
        } for m in messages])

    async def send_to_channel(request: Request) -> JSONResponse:
        """POST /api/channels/{channel_id}/send — Send a message to a channel.

        When `async: true` in body: save message + schedule fan-out in background,
        return immediately with message_id (202 Accepted).
        Default (async=false): blocking fan-out, returns agent responses.
        """
        channel_id = request.path_params["channel_id"]
        body = await request.json()
        text = body.get("text", "")
        sender_id = body.get("sender_id", "human")
        is_async = body.get("async", False)
        if not text:
            return JSONResponse({"error": "Missing required field: text"}, status_code=400)

        ch = await registry.get_channel(channel_id)
        if not ch:
            return JSONResponse({"error": "Channel not found"}, status_code=404)

        # Build A2A MessageSendParams for the hub handler
        from a2a.types import (
            Message as A2AMessage, Part, TextPart, Role,
            MessageSendParams, MessageSendConfiguration,
        )
        import uuid as _uuid

        # Pass through any extra metadata (e.g., recipient_id for directed messages)
        extra_meta = body.get("metadata", {}) if isinstance(body.get("metadata"), dict) else {}
        msg = A2AMessage(
            role=Role.user,
            parts=[Part(root=TextPart(text=text))],
            message_id=str(_uuid.uuid4()),
            context_id=ch.context_id,
            metadata={"channel_id": channel_id, "sender_id": sender_id, **extra_meta},
        )

        params = MessageSendParams(
            message=msg,
            configuration=MessageSendConfiguration(
                blocking=not is_async,
                accepted_output_modes=["text"],
            ),
        )

        if is_async:
            # Save message immediately, fan-out in background
            msg_id, error = await hub.save_message(params)
            if error:
                return JSONResponse({"error": error}, status_code=400)

            # Inter-agent messages (non-human sender): save only, no fan-out.
            # The message stays in channel history and agents see it in context
            # on the next user-triggered dispatch.
            if sender_id != "human":
                logger.info("Inter-agent message from %s in #%s — saved, no dispatch", sender_id, channel_id)
                return JSONResponse({
                    "status": "accepted",
                    "message_id": msg_id,
                    "channel_id": channel_id,
                    "note": "inter-agent message saved, no fan-out",
                }, status_code=202)

            hub.schedule_dispatch(params)
            return JSONResponse({
                "status": "accepted",
                "message_id": msg_id,
                "channel_id": channel_id,
            }, status_code=202)

        # Blocking mode (backward compatible)
        try:
            result = await hub.on_message_send(params)
        except Exception as exc:
            logger.exception("send_to_channel fan-out error")
            return JSONResponse({"error": str(exc)}, status_code=500)

        # Serialize result (Task or Message)
        if hasattr(result, "id"):
            # Task response with artifacts
            artifacts = []
            if result.artifacts:
                for art in result.artifacts:
                    art_text = ""
                    for p in art.parts:
                        if hasattr(p, "root") and hasattr(p.root, "text"):
                            art_text += p.root.text
                    artifacts.append({
                        "artifact_id": art.artifact_id,
                        "name": art.name,
                        "text": art_text,
                    })
            return JSONResponse({
                "task_id": result.id,
                "status": result.status.state.value if result.status else "unknown",
                "artifacts": artifacts,
            })
        else:
            # Message response (error case)
            text_out = ""
            for p in result.parts:
                if hasattr(p, "root") and hasattr(p.root, "text"):
                    text_out += p.root.text
            return JSONResponse({"message": text_out}, status_code=400)

    # -- Build app ----------------------------------------------------------

    routes = [
        # Message history and send (more specific paths first)
        Route("/api/channels/{channel_id}/messages", get_channel_messages, methods=["GET"]),
        Route("/api/channels/{channel_id}/send", send_to_channel, methods=["POST"]),
        # Channel CRUD
        Route("/api/channels", create_channel, methods=["POST"]),
        Route("/api/channels", list_channels, methods=["GET"]),
        Route("/api/channels/{channel_id}", get_channel, methods=["GET"]),
        Route("/api/channels/{channel_id}", delete_channel, methods=["DELETE"]),
        Route("/api/channels/{channel_id}", patch_channel, methods=["PATCH"]),
        Route("/api/channels/{channel_id}/members", add_member, methods=["POST"]),
        Route("/api/channels/{channel_id}/members/{agent_id}", remove_member, methods=["DELETE"]),
        Route("/api/channels/{channel_id}/members/{agent_id}", patch_member, methods=["PATCH"]),
        Route("/api/channels/{channel_id}/webhooks", create_webhook, methods=["POST"]),
        Route("/api/channels/{channel_id}/webhooks", list_webhooks, methods=["GET"]),
        Route("/api/channels/{channel_id}/webhooks/{webhook_id}", delete_webhook, methods=["DELETE"]),
        Route("/api/status", hub_status, methods=["GET"]),
        Route("/api/telegram/webhook", telegram_webhook, methods=["POST"]),
    ]

    middleware = [
        Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]),
    ]

    # -- Lifespan (startup + shutdown) -----------------------------------------

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None, None]:
        nonlocal telegram_bridge

        # Startup
        if os.environ.get("BOOTSTRAP_CHANNELS", "").lower() in ("true", "1", "yes"):
            logger.info("BOOTSTRAP_CHANNELS enabled — running channel bootstrap")
            await bootstrap_channels(registry)

        # Auto-register webhook for all channels if WEBHOOK_URL is set
        webhook_url = os.environ.get("WEBHOOK_URL")
        if webhook_url:
            from src.storage.base import Webhook
            channels = await registry.list_channels()
            for ch in channels:
                existing_wh = await storage.list_webhooks(ch.channel_id)
                already_registered = any(w.url == webhook_url for w in existing_wh)
                if not already_registered:
                    wh = Webhook(
                        webhook_id=f"brain-{ch.channel_id}",
                        url=webhook_url,
                        events=["message", "member_join"],
                    )
                    await storage.save_webhook(ch.channel_id, wh)
                    logger.info("Registered webhook %s for channel %s", webhook_url, ch.channel_id)

        if os.environ.get("TELEGRAM_ENABLED", "").lower() in ("true", "1", "yes"):
            from src.telegram.config import TelegramConfig
            from src.telegram.bridge import TelegramBridge
            tg_config = TelegramConfig.from_env()
            telegram_bridge = TelegramBridge(config=tg_config, hub_base_url="http://localhost:8000")
            await telegram_bridge.start()
            logger.info("Telegram bridge started")

        yield

        # Shutdown
        if telegram_bridge is not None:
            await telegram_bridge.stop()
        await hub.close()

    # -- A2A Protocol endpoint (JSON-RPC) ------------------------------------

    hub_url = os.environ.get("HUB_PUBLIC_URL", "http://localhost:8000")
    hub_agent_card = AgentCard(
        name="A2A Group Chat Hub",
        description="Broadcasting hub for A2A agent group communication.",
        url=hub_url,
        version="1.0.0",
        protocol_version="1.0",
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        default_input_modes=["text"],
        default_output_modes=["text"],
        skills=[
            AgentSkill(
                id="group-broadcast",
                name="Group Broadcast",
                description="Send a message to all agents in a channel.",
                tags=["broadcast", "group-chat"],
            ),
        ],
    )

    a2a_app = A2AStarletteApplication(
        agent_card=hub_agent_card,
        http_handler=hub,
    )

    # Merge REST routes + A2A protocol routes
    all_routes = routes + a2a_app.routes()

    app = Starlette(
        routes=all_routes,
        middleware=middleware,
        lifespan=lifespan,
    )

    # Store references
    app.state.hub = hub
    app.state.registry = registry
    app.state.storage = storage

    return app
