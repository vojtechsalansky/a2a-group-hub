# src/hub/handler.py
"""GroupChatHub — A2A RequestHandler implementing group chat broadcasting."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timezone

from a2a.server.request_handlers import RequestHandler
from a2a.server.context import ServerCallContext
from a2a.types import (
    Artifact, DeleteTaskPushNotificationConfigParams,
    GetTaskPushNotificationConfigParams, ListTaskPushNotificationConfigParams,
    Message, MessageSendParams, Part, Role, Task,
    TaskArtifactUpdateEvent, TaskIdParams, TaskPushNotificationConfig,
    TaskQueryParams, TaskState, TaskStatus, TaskStatusUpdateEvent, TextPart,
)

from src.channels.models import Channel, MemberRole
from src.channels.permissions import PermissionError, check_can_send
from src.channels.registry import ChannelRegistry
from src.hub.aggregator import Aggregator, AggregationStrategy
from src.hub.fanout import FanOutEngine
from src.storage.base import StorageBackend, StoredMessage

logger = logging.getLogger("a2a-hub.handler")


class GroupChatHub(RequestHandler):

    def __init__(self, registry: ChannelRegistry, storage: StorageBackend,
                 webhook_dispatcher=None) -> None:
        self.registry = registry
        self.storage = storage
        self._fanout_engine = FanOutEngine()
        self._aggregator = Aggregator()
        self._tasks: dict[str, Task] = {}
        self._webhook_dispatcher = webhook_dispatcher
        self._background_tasks: set[asyncio.Task] = set()

    async def close(self) -> None:
        # Cancel any pending background dispatch tasks
        for task in self._background_tasks:
            task.cancel()
        await self._fanout_engine.close()
        if self._webhook_dispatcher:
            await self._webhook_dispatcher.close()

    async def _fire_webhook(self, channel_id: str, event_type: str, data: dict) -> None:
        """Fire webhook for channel event if dispatcher is configured."""
        if not self._webhook_dispatcher:
            return
        webhooks = await self.storage.list_webhooks(channel_id)
        if not webhooks:
            return
        from src.notifications.webhooks import WebhookEvent
        event = WebhookEvent(
            event=event_type,
            channel_id=channel_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            data=data,
        )
        await self._webhook_dispatcher.dispatch(webhooks, event)

    # -- Channel resolution -------------------------------------------------

    async def _resolve_channel(self, params: MessageSendParams) -> tuple[Channel | None, str | None]:
        msg = params.message
        meta = msg.metadata or {}

        # Strategy 1: explicit channel_id
        channel_id = meta.get("channel_id")
        if channel_id:
            ch = await self.registry.get_channel(channel_id)
            return ch, meta.get("sender_id")

        # Strategy 2: context_id lookup
        if msg.context_id:
            for ch in await self.registry.list_channels():
                if ch.context_id == msg.context_id:
                    return ch, meta.get("sender_id")

        return None, None

    # -- Async dispatch (non-blocking) ----------------------------------------

    async def save_message(self, params: MessageSendParams) -> tuple[str | None, str | None]:
        """Save incoming message to storage without fan-out. Returns (message_id, error)."""
        channel, sender_id = await self._resolve_channel(params)
        if not channel:
            return None, "Could not resolve channel."
        try:
            check_can_send(channel, sender_id)
        except PermissionError as e:
            return None, str(e)

        text = self._extract_text(params.message)
        msg_id = params.message.message_id or str(uuid.uuid4())
        await self.storage.save_message(channel.channel_id, StoredMessage(
            message_id=msg_id,
            channel_id=channel.channel_id,
            sender_id=sender_id or "unknown",
            text=text,
            context_id=channel.context_id,
        ))
        await self._fire_webhook(channel.channel_id, "message", {
            "message_id": msg_id,
            "sender_id": sender_id or "unknown",
            "text": text,
            "context_id": channel.context_id,
        })
        return msg_id, None

    async def dispatch_async(self, params: MessageSendParams) -> None:
        """Run fan-out in background. Saves responses when done."""
        logger.info("dispatch_async: starting")
        channel, sender_id = await self._resolve_channel(params)
        if not channel:
            logger.warning("dispatch_async: channel not resolved")
            return

        logger.info("dispatch_async: channel=%s sender=%s", channel.channel_id, sender_id)
        meta = params.message.metadata or {}
        strategy_name = meta.get("aggregation", channel.default_aggregation)
        try:
            strategy = AggregationStrategy(strategy_name)
        except ValueError:
            strategy = AggregationStrategy.all

        try:
            results = await self._fanout_engine.fan_out(
                channel=channel,
                message_parts=params.message.parts,
                sender_id=sender_id,
                context_id=channel.context_id,
                message_metadata=params.message.metadata,
            )
        except Exception as exc:
            logger.exception("Background dispatch FAILED for #%s: %s", channel.name, exc)
            return

        logger.info("dispatch_async: fan-out complete, %d results", len(results))
        for r in results:
            text = r.response_text
            if r.error and not text:
                # Save error as visible message so user knows what happened
                text = f"[Agent error: {r.error}]"
                logger.error("dispatch_async: agent %s error: %s", r.agent_id, r.error)
            if text:
                await self.storage.save_message(channel.channel_id, StoredMessage(
                    message_id=str(uuid.uuid4()),
                    channel_id=channel.channel_id,
                    sender_id=r.agent_id,
                    text=text,
                    context_id=channel.context_id,
                ))
                await self._fire_webhook(channel.channel_id, "message", {
                    "sender_id": r.agent_id,
                    "agent_name": r.agent_name,
                    "text": text,
                    "context_id": channel.context_id,
                })

        channel.message_count += 1
        logger.info("Background dispatch completed for #%s (%d results)", channel.name, len(results))

    def schedule_dispatch(self, params: MessageSendParams) -> None:
        """Schedule fan-out as a background asyncio task."""
        channel_id = params.message.metadata.get('channel_id', 'unknown') if params.message.metadata else 'unknown'
        logger.info("schedule_dispatch: scheduling for channel=%s", channel_id)
        task = asyncio.create_task(
            self.dispatch_async(params),
            name=f"dispatch-{channel_id}",
        )
        self._background_tasks.add(task)
        def _done(t: asyncio.Task) -> None:
            self._background_tasks.discard(t)
            if t.cancelled():
                logger.warning("dispatch task cancelled: %s", t.get_name())
            elif t.exception():
                logger.error("dispatch task exception: %s — %s", t.get_name(), t.exception())
            else:
                logger.info("dispatch task completed: %s", t.get_name())
        task.add_done_callback(_done)

    # -- Core handlers ------------------------------------------------------

    async def on_message_send(
        self,
        params: MessageSendParams,
        context: ServerCallContext | None = None,
    ) -> Task | Message:
        channel, sender_id = await self._resolve_channel(params)

        if not channel:
            return Message(
                role=Role.agent,
                parts=[Part(root=TextPart(text="Error: Could not resolve channel. Include 'channel_id' in message metadata."))],
                message_id=str(uuid.uuid4()),
            )

        # Permission check
        try:
            check_can_send(channel, sender_id)
        except PermissionError as e:
            return Message(
                role=Role.agent,
                parts=[Part(root=TextPart(text=f"Permission denied: {e}"))],
                message_id=str(uuid.uuid4()),
            )

        # Extract text and persist incoming message
        text = self._extract_text(params.message)
        msg_id = params.message.message_id or str(uuid.uuid4())
        await self.storage.save_message(channel.channel_id, StoredMessage(
            message_id=msg_id,
            channel_id=channel.channel_id,
            sender_id=sender_id or "unknown",
            text=text,
            context_id=channel.context_id,
        ))

        # Fire webhook for user message
        await self._fire_webhook(channel.channel_id, "message", {
            "message_id": msg_id,
            "sender_id": sender_id or "unknown",
            "text": text,
            "context_id": channel.context_id,
        })

        # Fan-out
        meta = params.message.metadata or {}
        strategy_name = meta.get("aggregation", channel.default_aggregation)
        try:
            strategy = AggregationStrategy(strategy_name)
        except ValueError:
            strategy = AggregationStrategy.all

        results = await self._fanout_engine.fan_out(
            channel=channel,
            message_parts=params.message.parts,
            sender_id=sender_id,
            context_id=channel.context_id,
            message_metadata=params.message.metadata,
        )

        # Persist responses and fire webhooks
        for r in results:
            if r.response_text:
                await self.storage.save_message(channel.channel_id, StoredMessage(
                    message_id=str(uuid.uuid4()),
                    channel_id=channel.channel_id,
                    sender_id=r.agent_id,
                    text=r.response_text,
                    context_id=channel.context_id,
                ))
                # Fire webhook for agent response
                await self._fire_webhook(channel.channel_id, "message", {
                    "sender_id": r.agent_id,
                    "agent_name": r.agent_name,
                    "text": r.response_text,
                    "context_id": channel.context_id,
                })

        # Aggregate
        task_id = str(uuid.uuid4())
        task = self._aggregator.aggregate(
            results=results,
            strategy=strategy,
            task_id=task_id,
            context_id=channel.context_id,
            channel_id=channel.channel_id,
            channel_name=channel.name,
        )

        channel.message_count += 1
        self._tasks[task_id] = task
        return task

    async def on_message_send_stream(
        self,
        params: MessageSendParams,
        context: ServerCallContext | None = None,
    ) -> AsyncGenerator[Message | Task | TaskStatusUpdateEvent | TaskArtifactUpdateEvent]:
        channel, sender_id = await self._resolve_channel(params)

        if not channel:
            yield Message(
                role=Role.agent,
                parts=[Part(root=TextPart(text="Error: Could not resolve channel."))],
                message_id=str(uuid.uuid4()),
            )
            return

        try:
            check_can_send(channel, sender_id)
        except PermissionError as e:
            yield Message(
                role=Role.agent,
                parts=[Part(root=TextPart(text=f"Permission denied: {e}"))],
                message_id=str(uuid.uuid4()),
            )
            return

        task_id = str(uuid.uuid4())
        peers = channel.get_sendable_peers(exclude_agent_id=sender_id)

        yield TaskStatusUpdateEvent(
            taskId=task_id,
            contextId=channel.context_id,
            final=False,
            status=TaskStatus(
                state=TaskState.working,
                timestamp=datetime.now(timezone.utc).isoformat(),
                message=Message(
                    role=Role.agent,
                    parts=[Part(root=TextPart(text=f"Broadcasting to {len(peers)} agents in #{channel.name}..."))],
                    message_id=str(uuid.uuid4()),
                ),
            ),
        )

        # Fan-out with streaming results via as_completed
        results = await self._fanout_engine.fan_out(
            channel=channel,
            message_parts=params.message.parts,
            sender_id=sender_id,
            context_id=channel.context_id,
            message_metadata=params.message.metadata,
        )

        for i, r in enumerate(results):
            text = r.error and f"[Error]: {r.error}" or r.response_text or "[No response]"
            yield TaskArtifactUpdateEvent(
                taskId=task_id,
                contextId=channel.context_id,
                artifact=Artifact(
                    artifact_id=f"{task_id}-{i}",
                    name=f"Response from {r.agent_name}",
                    parts=[Part(root=TextPart(text=text))],
                ),
            )

        channel.message_count += 1
        yield TaskStatusUpdateEvent(
            taskId=task_id,
            contextId=channel.context_id,
            final=True,
            status=TaskStatus(
                state=TaskState.completed,
                timestamp=datetime.now(timezone.utc).isoformat(),
                message=Message(
                    role=Role.agent,
                    parts=[Part(root=TextPart(text=f"All {len(peers)} agents responded in #{channel.name}"))],
                    message_id=str(uuid.uuid4()),
                ),
            ),
        )

    # -- Task management stubs ----------------------------------------------

    async def on_get_task(self, params: TaskQueryParams, context=None) -> Task | None:
        return self._tasks.get(params.id)

    async def on_cancel_task(self, params: TaskIdParams, context=None) -> Task | None:
        task = self._tasks.get(params.id)
        if task:
            task.status = TaskStatus(state=TaskState.canceled, timestamp=datetime.now(timezone.utc).isoformat())
        return task

    async def on_set_task_push_notification_config(self, params: TaskPushNotificationConfig, context=None) -> TaskPushNotificationConfig:
        return params

    async def on_get_task_push_notification_config(self, params, context=None) -> TaskPushNotificationConfig:
        raise NotImplementedError

    async def on_list_task_push_notification_config(self, params, context=None) -> list[TaskPushNotificationConfig]:
        return []

    async def on_delete_task_push_notification_config(self, params, context=None) -> None:
        pass

    async def on_resubscribe_to_task(self, params, context=None):
        task = self._tasks.get(params.id)
        if task:
            yield task

    @staticmethod
    def _extract_text(message: Message) -> str:
        return "".join(
            p.root.text for p in message.parts
            if hasattr(p, "root") and hasattr(p.root, "text")
        )
