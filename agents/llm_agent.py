# agents/llm_agent.py
"""LLM-backed A2A agent using Claude API with conversation history."""

from __future__ import annotations

import logging
import os
import uuid
from collections import defaultdict

from a2a.server.agent_execution import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.types import Message, Part, Role, TextPart

from agents.agents_config import AGENT_CONFIGS, DEFAULT_CONFIG

logger = logging.getLogger(__name__)

try:
    from anthropic import AsyncAnthropic

    _HAS_ANTHROPIC = True
except ImportError:
    _HAS_ANTHROPIC = False


class LLMAgentExecutor(AgentExecutor):
    """Agent executor backed by Claude API with per-context conversation history."""

    def __init__(
        self,
        role: str = "researcher",
        api_key: str | None = None,
        max_history: int = 20,
    ) -> None:
        config = AGENT_CONFIGS.get(role, DEFAULT_CONFIG)
        self._system_prompt = config["system_prompt"]
        self._temperature = config["temperature"]
        self._model = config["model"]
        self._max_history = max_history
        self._history: dict[str, list[dict[str, str]]] = defaultdict(list)

        resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if resolved_key and _HAS_ANTHROPIC:
            self._client: AsyncAnthropic | None = AsyncAnthropic(api_key=resolved_key)
        else:
            self._client = None
            if not _HAS_ANTHROPIC:
                logger.warning("anthropic package not installed — using echo fallback")
            elif not resolved_key:
                logger.warning("ANTHROPIC_API_KEY not set — using echo fallback")

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        user_text = context.get_user_input()
        context_id = context.context_id or "default"

        # Add user message to history
        self._history[context_id].append({"role": "user", "content": user_text})

        if self._client:
            response_text = await self._call_llm(context_id)
        else:
            response_text = f"[echo-fallback:{context_id[:8]}] {user_text}"

        # Add assistant response to history
        self._history[context_id].append({"role": "assistant", "content": response_text})

        # Cap history
        if len(self._history[context_id]) > self._max_history:
            self._history[context_id] = self._history[context_id][-self._max_history:]

        message = Message(
            message_id=str(uuid.uuid4()),
            role=Role.agent,
            parts=[Part(root=TextPart(text=response_text))],
            context_id=context.context_id,
        )
        await event_queue.enqueue_event(message)

    async def _call_llm(self, context_id: str) -> str:
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=1024,
                system=self._system_prompt,
                temperature=self._temperature,
                messages=self._history[context_id],
            )
            return response.content[0].text
        except Exception as e:
            logger.error("Claude API error: %s", e)
            return f"[error] Failed to get LLM response: {e}"

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        pass  # LLM calls are not cancellable mid-flight in this implementation
