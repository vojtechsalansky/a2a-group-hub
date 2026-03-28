# tests/test_llm_agent.py
"""Tests for the LLM agent and agent configs."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.types import Message, MessageSendParams, Part, Role, TextPart

from agents.llm_agent import LLMAgentExecutor
from agents.agents_config import AGENT_CONFIGS


def _make_params(text: str, context_id: str | None = None) -> MessageSendParams:
    return MessageSendParams(
        message=Message(
            message_id=str(uuid.uuid4()),
            role=Role.user,
            parts=[Part(root=TextPart(text=text))],
            context_id=context_id or str(uuid.uuid4()),
        )
    )


class TestAgentConfigs:
    def test_researcher_config_exists(self):
        assert "researcher" in AGENT_CONFIGS
        cfg = AGENT_CONFIGS["researcher"]
        assert "system_prompt" in cfg
        assert "temperature" in cfg

    def test_engineer_config_exists(self):
        assert "engineer" in AGENT_CONFIGS
        cfg = AGENT_CONFIGS["engineer"]
        assert cfg["temperature"] == 0.5

    def test_critic_config_exists(self):
        assert "critic" in AGENT_CONFIGS
        cfg = AGENT_CONFIGS["critic"]
        assert cfg["temperature"] == 0.8

    def test_all_configs_have_required_fields(self):
        for name, cfg in AGENT_CONFIGS.items():
            assert "system_prompt" in cfg, f"{name} missing system_prompt"
            assert "temperature" in cfg, f"{name} missing temperature"
            assert "model" in cfg, f"{name} missing model"


class TestLLMAgentExecutor:
    async def test_fallback_echo_without_api_key(self):
        """Without ANTHROPIC_API_KEY, should fall back to echo behavior."""
        executor = LLMAgentExecutor(role="researcher", api_key=None)
        params = _make_params("What is Python?")
        ctx = RequestContext(request=params)
        queue = EventQueue()

        await executor.execute(ctx, queue)

        event = await queue.dequeue_event(no_wait=True)
        assert isinstance(event, Message)
        assert event.role == Role.agent
        # Fallback echo should include the original text
        text = event.parts[0].root.text
        assert "What is Python?" in text

    async def test_conversation_history_tracked(self):
        """Conversation history should be tracked per context_id."""
        executor = LLMAgentExecutor(role="researcher", api_key=None)
        context_id = str(uuid.uuid4())

        params1 = _make_params("First message", context_id=context_id)
        ctx1 = RequestContext(request=params1)
        queue1 = EventQueue()
        await executor.execute(ctx1, queue1)

        params2 = _make_params("Second message", context_id=context_id)
        ctx2 = RequestContext(request=params2)
        queue2 = EventQueue()
        await executor.execute(ctx2, queue2)

        # Should have 4 entries in history: user1, assistant1, user2, assistant2
        assert len(executor._history[context_id]) == 4

    async def test_separate_contexts_have_separate_history(self):
        executor = LLMAgentExecutor(role="researcher", api_key=None)

        ctx_id_a = str(uuid.uuid4())
        ctx_id_b = str(uuid.uuid4())

        params_a = _make_params("msg A", context_id=ctx_id_a)
        ctx_a = RequestContext(request=params_a)
        queue_a = EventQueue()
        await executor.execute(ctx_a, queue_a)

        params_b = _make_params("msg B", context_id=ctx_id_b)
        ctx_b = RequestContext(request=params_b)
        queue_b = EventQueue()
        await executor.execute(ctx_b, queue_b)

        assert len(executor._history[ctx_id_a]) == 2
        assert len(executor._history[ctx_id_b]) == 2

    async def test_history_capped_at_max(self):
        """History should be capped at max_history entries."""
        executor = LLMAgentExecutor(role="researcher", api_key=None, max_history=4)
        context_id = str(uuid.uuid4())

        for i in range(5):
            params = _make_params(f"Message {i}", context_id=context_id)
            ctx = RequestContext(request=params)
            queue = EventQueue()
            await executor.execute(ctx, queue)

        # 5 exchanges = 10 messages, capped at 4
        assert len(executor._history[context_id]) <= 4

    async def test_cancel_is_noop(self):
        executor = LLMAgentExecutor(role="researcher", api_key=None)
        ctx = RequestContext()
        queue = EventQueue()
        await executor.cancel(ctx, queue)

    async def test_unknown_role_uses_defaults(self):
        """An unknown role should still create a valid executor with defaults."""
        executor = LLMAgentExecutor(role="unknown_role", api_key=None)
        params = _make_params("test")
        ctx = RequestContext(request=params)
        queue = EventQueue()

        await executor.execute(ctx, queue)

        event = await queue.dequeue_event(no_wait=True)
        assert isinstance(event, Message)
