# src/hub/fanout.py
"""Fan-out engine — parallel A2A SendMessage to channel peers."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass

import httpx
from a2a.client import A2AClient
from a2a.types import (
    Message, MessageSendConfiguration, MessageSendParams, Part,
    Role, SendMessageRequest, Task, TextPart,
)

from src.channels.models import Channel, ChannelMember, MemberRole

logger = logging.getLogger("a2a-hub.fanout")


@dataclass
class FanOutResult:
    """Result from one agent in a fan-out broadcast."""
    agent_id: str
    agent_name: str
    response_text: str | None = None
    response: Message | Task | None = None
    error: str | None = None


class FanOutEngine:

    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        default_timeout = float(os.environ.get("FANOUT_TIMEOUT_SECONDS", "600"))
        self._http_client = http_client or httpx.AsyncClient(timeout=default_timeout)
        self._owns_client = http_client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._http_client.aclose()

    @staticmethod
    def _observer_done_callback(task: asyncio.Task) -> None:
        """Log exceptions from observer fire-and-forget tasks."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(f"Observer task {task.get_name()} failed: {exc}")

    async def fan_out(
        self,
        channel: Channel,
        message_parts: list[Part],
        sender_id: str | None,
        context_id: str,
        message_metadata: dict | None = None,
    ) -> list[FanOutResult]:
        """Broadcast to peers. If metadata contains 'recipient_id', only that agent receives.
        Otherwise broadcasts to all peers (excluding sender). Observers always fire-and-forget."""

        meta = message_metadata or {}
        recipient_id = meta.get("recipient_id")

        # DEBUG: log metadata to trace recipient_id issues
        print(f"[fan_out] channel={channel.channel_id} sender={sender_id} recipient_id={recipient_id} metadata_keys={list(meta.keys())}", flush=True)

        sendable = channel.get_sendable_peers(exclude_agent_id=sender_id)

        # Directed message: only send to specific recipient
        if recipient_id:
            before = [m.agent_id for m in sendable]
            sendable = [m for m in sendable if m.agent_id == recipient_id]
            print(f"[fan_out] directed filter: {before} -> {[m.agent_id for m in sendable]}", flush=True)
            if not sendable:
                logger.warning(f"Recipient '{recipient_id}' not found in #{channel.name}")

        observers = [o for o in channel.get_observers() if o.agent_id != sender_id]

        logger.info(
            f"Fan-out in #{channel.name}: {len(sendable)} members, {len(observers)} observers"
            + (f" (directed to {recipient_id})" if recipient_id else "")
        )

        # Fire-and-forget to observers (tracked with error logging callback)
        for obs in observers:
            task = asyncio.create_task(
                self._send_to_agent(obs, message_parts=message_parts, channel=channel,
                                     context_id=context_id, metadata=message_metadata),
                name=f"observer-{obs.agent_id}",
            )
            task.add_done_callback(self._observer_done_callback)

        if not sendable:
            return []

        # Sequential send with small delay to avoid overwhelming the LLM proxy
        # (parallel fan-out can trigger rate limits when all agents call the same proxy)
        fan_out_delay = float(os.environ.get("FANOUT_DELAY_SECONDS", "5.0"))
        results: list[FanOutResult] = []
        for i, member in enumerate(sendable):
            result = await self._send_to_agent(
                member, message_parts=message_parts, channel=channel,
                context_id=context_id, metadata=message_metadata,
            )
            results.append(result)
            if i < len(sendable) - 1 and fan_out_delay > 0:
                await asyncio.sleep(fan_out_delay)
        return results

    async def _send_to_agent(
        self,
        member: ChannelMember,
        message_parts: list[Part],
        channel: Channel,
        context_id: str,
        metadata: dict | None = None,
    ) -> FanOutResult:
        """Send a message to a single agent via A2A SendMessage."""
        try:
            client = A2AClient(httpx_client=self._http_client, url=member.url)

            outbound = Message(
                role=Role.user,
                parts=message_parts,
                message_id=str(uuid.uuid4()),
                context_id=context_id,
                metadata={
                    **(metadata or {}),
                    "hub_channel_id": channel.channel_id,
                    "hub_channel_name": channel.name,
                },
            )

            request = SendMessageRequest(
                id=str(uuid.uuid4()),
                params=MessageSendParams(
                    message=outbound,
                    configuration=MessageSendConfiguration(
                        blocking=True,
                        accepted_output_modes=["text"],
                    ),
                ),
            )

            response = await client.send_message(
                request,
                http_kwargs={"headers": member.auth_headers} if member.auth_token else {},
            )

            result = response.root
            if hasattr(result, "result"):
                inner = result.result
                # Extract text from response
                text = self._extract_text(inner)
                return FanOutResult(
                    agent_id=member.agent_id,
                    agent_name=member.name,
                    response_text=text,
                    response=inner,
                )
            elif hasattr(result, "error"):
                return FanOutResult(
                    agent_id=member.agent_id,
                    agent_name=member.name,
                    error=str(result.error),
                )
            return FanOutResult(agent_id=member.agent_id, agent_name=member.name, error="Unknown response format")

        except Exception as e:
            logger.error(f"Fan-out to {member.name} ({member.url}) failed: {e}")
            return FanOutResult(agent_id=member.agent_id, agent_name=member.name, error=str(e))

    @staticmethod
    def _extract_text(response: Message | Task) -> str:
        """Extract text content from an A2A response."""
        if isinstance(response, Message):
            return "".join(
                p.root.text for p in response.parts
                if hasattr(p, "root") and hasattr(p.root, "text")
            )
        if isinstance(response, Task):
            texts = []
            if response.artifacts:
                for art in response.artifacts:
                    for p in art.parts:
                        if hasattr(p, "root") and hasattr(p.root, "text"):
                            texts.append(p.root.text)
            return "\n".join(texts)
        return ""
