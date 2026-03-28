# agents/demo_agent.py
"""Demo echo agent — responds by echoing the input. No LLM required."""

from __future__ import annotations

import uuid

from a2a.server.agent_execution import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.types import Message, Part, Role, TextPart


class DemoEchoExecutor(AgentExecutor):
    """Simple echo agent for testing the hub without any LLM dependency."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        user_text = context.get_user_input()
        response_text = f"[echo] {user_text}" if user_text else "[echo] (empty message)"

        message = Message(
            message_id=str(uuid.uuid4()),
            role=Role.agent,
            parts=[Part(root=TextPart(text=response_text))],
            context_id=context.context_id,
        )
        await event_queue.enqueue_event(message)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        pass  # echo agent has nothing to cancel
