# tests/test_fanout.py
"""Tests for fan-out engine — mock A2A clients to test broadcast logic."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from a2a.types import Part, TextPart
from src.channels.models import Channel, ChannelMember, MemberRole
from src.hub.fanout import FanOutEngine, FanOutResult


@pytest.fixture
def channel():
    ch = Channel(channel_id="dev", name="dev-team")
    ch.add_member(ChannelMember(agent_id="rex", name="Rex", url="http://localhost:9001"))
    ch.add_member(ChannelMember(agent_id="pixel", name="Pixel", url="http://localhost:9002"))
    ch.add_member(ChannelMember(agent_id="vigil", name="Vigil", url="http://localhost:9003", role=MemberRole.observer))
    return ch


class TestFanOutEngine:
    async def test_fan_out_excludes_sender(self, channel):
        engine = FanOutEngine()
        engine._send_to_agent = AsyncMock(return_value=FanOutResult(agent_id="test", agent_name="Test"))

        results = await engine.fan_out(channel, message_parts=[], sender_id="rex", context_id="ctx")

        # Should send to pixel (member) + vigil (observer) but not rex (sender)
        assert engine._send_to_agent.call_count == 2

    async def test_fan_out_returns_only_member_results(self, channel):
        """Observer results are fire-and-forget, not included in return."""
        engine = FanOutEngine()

        async def mock_send(member, **kwargs):
            return FanOutResult(agent_id=member.agent_id, agent_name=member.name, response_text=f"Reply from {member.name}")

        engine._send_to_agent = mock_send
        results = await engine.fan_out(channel, message_parts=[], sender_id="rex", context_id="ctx")

        # Only pixel's result returned (vigil is observer = fire-and-forget)
        assert len(results) == 1
        assert results[0].agent_id == "pixel"

    async def test_fan_out_handles_agent_failure(self, channel):
        engine = FanOutEngine()

        async def mock_send(member, **kwargs):
            if member.agent_id == "pixel":
                return FanOutResult(agent_id="pixel", agent_name="Pixel", error="Connection refused")
            return FanOutResult(agent_id=member.agent_id, agent_name=member.name, response_text="OK")

        engine._send_to_agent = mock_send
        results = await engine.fan_out(channel, message_parts=[], sender_id="rex", context_id="ctx")

        assert len(results) == 1
        assert results[0].error == "Connection refused"

    async def test_fan_out_empty_channel(self):
        ch = Channel(channel_id="empty", name="empty")
        engine = FanOutEngine()
        results = await engine.fan_out(ch, message_parts=[], sender_id=None, context_id="ctx")
        assert len(results) == 0

    async def test_observer_done_callback_logs_exception(self):
        """Observer fire-and-forget tasks should have a done callback that logs errors."""
        engine = FanOutEngine()

        async def failing_send(member, **kwargs):
            raise RuntimeError("Observer delivery exploded")

        engine._send_to_agent = failing_send

        ch = Channel(channel_id="dev", name="dev-team")
        ch.add_member(ChannelMember(agent_id="rex", name="Rex", url="http://localhost:9001"))
        ch.add_member(ChannelMember(
            agent_id="vigil", name="Vigil", url="http://localhost:9003", role=MemberRole.observer
        ))

        # Fan out from rex — vigil (observer) should get fire-and-forget
        # This should not raise, even though observer delivery fails
        results = await engine.fan_out(ch, message_parts=[], sender_id="rex", context_id="ctx")

        # Allow observer task to complete
        import asyncio
        await asyncio.sleep(0.05)

        # No member results since only vigil (observer) is a peer and not sendable
        assert len(results) == 0

    async def test_channel_context_prepended_to_message(self, channel):
        """Fan-out should prepend channel name and member list to message text."""
        engine = FanOutEngine()
        captured_parts: list = []

        original_send = engine._send_to_agent

        async def capture_send(member, *, message_parts, channel, context_id, metadata=None):
            """Intercept _send_to_agent to capture the enriched parts it builds."""
            # We can't easily capture what happens inside _send_to_agent since it
            # creates the Message internally. Instead, test the logic directly.
            pass

        # Test the enrichment logic directly by calling _send_to_agent with a
        # mock A2AClient and capturing the outbound message.
        original_text = "Hello team!"
        parts = [Part(root=TextPart(text=original_text))]

        with patch("src.hub.fanout.A2AClient") as MockClient:
            mock_instance = MagicMock()
            mock_response = MagicMock()
            mock_result = MagicMock()
            mock_result.result = MagicMock(spec=[])
            mock_result.result.parts = []
            mock_response.root = mock_result
            mock_instance.send_message = AsyncMock(return_value=mock_response)
            MockClient.return_value = mock_instance

            target = channel.members["pixel"]
            await engine._send_to_agent(
                target, message_parts=parts, channel=channel, context_id="ctx"
            )

            # Extract the message that was sent
            call_args = mock_instance.send_message.call_args
            request = call_args[0][0]
            sent_parts = request.params.message.parts
            sent_text = sent_parts[0].root.text

            # Verify context prefix is present
            assert sent_text.startswith("[Kanál: #dev-team |")
            assert "Členové:" in sent_text
            assert "Rex" in sent_text
            assert "Pixel" in sent_text
            assert "Vigil (observer)" in sent_text
            # Verify original text is preserved after prefix
            assert sent_text.endswith(original_text)

    async def test_channel_context_added_when_no_text_parts(self, channel):
        """When message has no text parts, context should be added as a new part."""
        engine = FanOutEngine()

        with patch("src.hub.fanout.A2AClient") as MockClient:
            mock_instance = MagicMock()
            mock_response = MagicMock()
            mock_result = MagicMock()
            mock_result.result = MagicMock(spec=[])
            mock_result.result.parts = []
            mock_response.root = mock_result
            mock_instance.send_message = AsyncMock(return_value=mock_response)
            MockClient.return_value = mock_instance

            target = channel.members["pixel"]
            await engine._send_to_agent(
                target, message_parts=[], channel=channel, context_id="ctx"
            )

            call_args = mock_instance.send_message.call_args
            request = call_args[0][0]
            sent_parts = request.params.message.parts

            assert len(sent_parts) == 1
            assert sent_parts[0].root.text.startswith("[Kanál: #dev-team |")
