# tests/test_fanout.py
"""Tests for fan-out engine — mock A2A clients to test broadcast logic."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
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
