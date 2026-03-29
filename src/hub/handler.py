# src/hub/handler.py
"""GroupChatHub — A2A RequestHandler implementing group chat broadcasting."""

from __future__ import annotations

import asyncio
import logging
import time
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
from src.hub.fanout import CircuitBreaker, FanOutEngine, FanOutResult
from src.observability.metrics import MetricsCollector
from src.storage.base import StorageBackend, StoredMessage

# Router is optional — built by another agent, may not exist yet
try:
    from src.hub.router import HierarchicalRouter
except ImportError:
    HierarchicalRouter = None  # type: ignore[assignment,misc]

logger = logging.getLogger("a2a-hub.handler")


class GroupChatHub(RequestHandler):

    def __init__(
        self,
        registry: ChannelRegistry,
        storage: StorageBackend,
        router=None,
        metrics: MetricsCollector | None = None,
    ) -> None:
        self.registry = registry
        self.storage = storage
        self._router = router
        self._metrics = metrics
        self._circuit_breaker = CircuitBreaker()
        self._fanout_engine = FanOutEngine(circuit_breaker=self._circuit_breaker)
        self._aggregator = Aggregator()
        self._tasks: dict[str, Task] = {}

    async def close(self) -> None:
        await self._fanout_engine.close()

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

    # -- Memory recall --------------------------------------------------------

    async def _recall_memory(self, channel: Channel, text: str, message_id: str) -> str:
        """Search for relevant past messages and return memory context."""
        memory_context = ""
        try:
            past_messages = await self.storage.search_messages(
                channel_id=channel.channel_id,
                query=text,
                limit=5,
            )
            past_messages = [m for m in past_messages if m.message_id != message_id]
            if past_messages:
                lines = []
                for m in past_messages[:3]:
                    ts = m.timestamp.strftime("%d.%m %H:%M") if hasattr(m.timestamp, 'strftime') else str(m.timestamp)[:16]
                    lines.append(f"- {m.sender_id} ({ts}): {m.text[:300]}")
                memory_context = "[Relevantní historie z kanálu #{}]\n{}".format(
                    channel.name, "\n".join(lines)
                )
                logger.info("Memory recall: %d messages for #%s", len(past_messages), channel.name)
        except Exception:
            logger.debug("Memory recall unavailable")
        return memory_context

    # -- Two-phase fan-out helpers -------------------------------------------

    async def _two_phase_fanout(
        self,
        channel: Channel,
        message_parts: list[Part],
        sender_id: str | None,
        text: str,
        msg_meta: dict,
        memory_context: str,
    ) -> list[FanOutResult]:
        """Two-phase fan-out: lead first, then delegates based on @mentions or keywords.

        Falls back to broadcast when router is absent, routing fails, or lead
        is not in the channel.
        """
        routing = None
        if self._router:
            try:
                routing = await self._router.route(channel.channel_id, text)
            except Exception:
                logger.warning("Router failed for #%s, falling back to broadcast", channel.name)
                routing = None

        if routing and routing.strategy != "broadcast_fallback":
            lead_member = channel.members.get(routing.lead_id)
            if lead_member:
                return await self._delegated_fanout(
                    channel=channel,
                    message_parts=message_parts,
                    lead_member=lead_member,
                    routing=routing,
                    msg_meta=msg_meta,
                    memory_context=memory_context,
                )

        # No router, broadcast fallback, or lead not in channel — original behavior
        return await self._fanout_engine.fan_out(
            channel=channel,
            message_parts=message_parts,
            sender_id=sender_id,
            context_id=channel.context_id,
            message_metadata=msg_meta,
        )

    async def _delegated_fanout(
        self,
        channel: Channel,
        message_parts: list[Part],
        lead_member,
        routing,
        msg_meta: dict,
        memory_context: str,
    ) -> list[FanOutResult]:
        """Phase 1: send to lead. Phase 2: send to delegates with accumulated context."""
        # Phase 1: Send to lead
        try:
            lead_result = await self._fanout_engine.send_to_single(
                member=lead_member,
                message_parts=message_parts,
                channel=channel,
                context_id=channel.context_id,
                message_metadata=msg_meta,
                memory_context=memory_context,
                previous_responses=[],
            )
        except Exception:
            logger.error("Lead %s failed, falling back to broadcast", lead_member.name)
            return await self._fanout_engine.fan_out(
                channel=channel,
                message_parts=message_parts,
                sender_id=None,
                context_id=channel.context_id,
                message_metadata=msg_meta,
            )

        # Phase 2: Parse @mentions from lead's response
        delegates: list[str] = []
        if lead_result.response_text and self._router:
            mentions = self._router.parse_mentions(lead_result.response_text)
            if mentions:
                delegates = [
                    m for m in mentions
                    if m in channel.members and m != routing.lead_id
                ]

            # Fallback: keyword-based delegates if no @mentions parsed
            if not delegates and hasattr(routing, "delegates") and routing.delegates:
                delegates = [
                    d for d in routing.delegates
                    if d in channel.members and d != routing.lead_id
                ]

        # Send to delegates with accumulated context
        delegate_results: list[FanOutResult] = []
        for agent_id in delegates:
            member = channel.members.get(agent_id)
            if member:
                result = await self._fanout_engine.send_to_single(
                    member=member,
                    message_parts=message_parts,
                    channel=channel,
                    context_id=channel.context_id,
                    message_metadata=msg_meta,
                    memory_context=memory_context,
                    previous_responses=[lead_result] + delegate_results,
                )
                delegate_results.append(result)

        return [lead_result] + delegate_results

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

        # Record metrics
        if self._metrics:
            self._metrics.record_message()
            self._metrics.record_channel_message(channel.channel_id)

        # Extract text and persist incoming message
        text = self._extract_text(params.message)
        await self.storage.save_message(channel.channel_id, StoredMessage(
            message_id=params.message.message_id or str(uuid.uuid4()),
            channel_id=channel.channel_id,
            sender_id=sender_id or "unknown",
            text=text,
            context_id=channel.context_id,
        ))

        # Memory recall
        incoming_id = params.message.message_id or ""
        memory_context = await self._recall_memory(channel, text, incoming_id)
        msg_meta = dict(params.message.metadata or {})
        if memory_context:
            msg_meta["memory_context"] = memory_context

        # Aggregation strategy
        strategy_name = msg_meta.get("aggregation", channel.default_aggregation)
        try:
            strategy = AggregationStrategy(strategy_name)
        except ValueError:
            strategy = AggregationStrategy.all

        # Two-phase fan-out (falls back to broadcast without router)
        fanout_start = time.time()
        results = await self._two_phase_fanout(
            channel=channel,
            message_parts=params.message.parts,
            sender_id=sender_id,
            text=text,
            msg_meta=msg_meta,
            memory_context=memory_context,
        )
        if self._metrics:
            self._metrics.record_fanout_duration(time.time() - fanout_start)

        # Persist responses
        for r in results:
            if r.response_text:
                await self.storage.save_message(channel.channel_id, StoredMessage(
                    message_id=str(uuid.uuid4()),
                    channel_id=channel.channel_id,
                    sender_id=r.agent_id,
                    text=r.response_text,
                    context_id=channel.context_id,
                ))

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

        # Record metrics
        if self._metrics:
            self._metrics.record_message()
            self._metrics.record_channel_message(channel.channel_id)

        # Memory recall
        text = self._extract_text(params.message)
        incoming_id = params.message.message_id or ""
        memory_context = await self._recall_memory(channel, text, incoming_id)
        msg_meta = dict(params.message.metadata or {})
        if memory_context:
            msg_meta["memory_context"] = memory_context

        task_id = str(uuid.uuid4())

        yield TaskStatusUpdateEvent(
            taskId=task_id,
            contextId=channel.context_id,
            final=False,
            status=TaskStatus(
                state=TaskState.working,
                timestamp=datetime.now(timezone.utc).isoformat(),
                message=Message(
                    role=Role.agent,
                    parts=[Part(root=TextPart(text=f"Routing message in #{channel.name}..."))],
                    message_id=str(uuid.uuid4()),
                ),
            ),
        )

        # Two-phase fan-out (falls back to broadcast without router)
        fanout_start = time.time()
        results = await self._two_phase_fanout(
            channel=channel,
            message_parts=params.message.parts,
            sender_id=sender_id,
            text=text,
            msg_meta=msg_meta,
            memory_context=memory_context,
        )
        if self._metrics:
            self._metrics.record_fanout_duration(time.time() - fanout_start)

        for i, r in enumerate(results):
            resp_text = r.error and f"[Error]: {r.error}" or r.response_text or "[No response]"
            yield TaskArtifactUpdateEvent(
                taskId=task_id,
                contextId=channel.context_id,
                artifact=Artifact(
                    artifact_id=f"{task_id}-{i}",
                    name=f"Response from {r.agent_name}",
                    parts=[Part(root=TextPart(text=resp_text))],
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
                    parts=[Part(root=TextPart(text=f"{len(results)} agent(s) responded in #{channel.name}"))],
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
