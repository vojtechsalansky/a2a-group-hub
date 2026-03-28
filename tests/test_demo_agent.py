# tests/test_demo_agent.py
"""Tests for the demo echo agent."""

import uuid

import pytest
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.types import Message, MessageSendParams, Part, Role, TextPart

from agents.demo_agent import DemoEchoExecutor


@pytest.fixture
def executor():
    return DemoEchoExecutor()


def _make_params(text: str, context_id: str | None = None) -> MessageSendParams:
    return MessageSendParams(
        message=Message(
            message_id=str(uuid.uuid4()),
            role=Role.user,
            parts=[Part(root=TextPart(text=text))],
            context_id=context_id or str(uuid.uuid4()),
        )
    )


class TestDemoEchoExecutor:
    async def test_echo_response(self, executor):
        params = _make_params("Hello, world!")
        ctx = RequestContext(request=params)
        queue = EventQueue()

        await executor.execute(ctx, queue)

        event = await queue.dequeue_event(no_wait=True)
        assert isinstance(event, Message)
        text = event.parts[0].root.text
        assert "Hello, world!" in text
        assert event.role == Role.agent

    async def test_echo_preserves_context_id(self, executor):
        context_id = str(uuid.uuid4())
        params = _make_params("test", context_id=context_id)
        ctx = RequestContext(request=params)
        queue = EventQueue()

        await executor.execute(ctx, queue)

        event = await queue.dequeue_event(no_wait=True)
        assert isinstance(event, Message)
        assert event.context_id == context_id

    async def test_empty_input(self, executor):
        params = _make_params("")
        ctx = RequestContext(request=params)
        queue = EventQueue()

        await executor.execute(ctx, queue)

        event = await queue.dequeue_event(no_wait=True)
        assert isinstance(event, Message)

    async def test_cancel_is_noop(self, executor):
        ctx = RequestContext()
        queue = EventQueue()
        # cancel should not raise
        await executor.cancel(ctx, queue)
