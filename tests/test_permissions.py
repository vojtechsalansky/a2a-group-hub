# tests/test_permissions.py
"""Tests for channel permission checks."""

import pytest
from src.channels.models import Channel, ChannelMember, MemberRole
from src.channels.permissions import PermissionError, check_can_send, check_can_manage


@pytest.fixture
def channel_with_members():
    ch = Channel(channel_id="dev", name="dev-team")
    ch.add_member(ChannelMember(agent_id="apollo", name="Apollo", url="http://localhost:9001", role=MemberRole.owner))
    ch.add_member(ChannelMember(agent_id="rex", name="Rex", url="http://localhost:9002", role=MemberRole.member))
    ch.add_member(ChannelMember(agent_id="vigil", name="Vigil", url="http://localhost:9003", role=MemberRole.observer))
    return ch


class TestCheckCanSend:
    def test_owner_can_send(self, channel_with_members):
        check_can_send(channel_with_members, "apollo")  # should not raise

    def test_member_can_send(self, channel_with_members):
        check_can_send(channel_with_members, "rex")  # should not raise

    def test_observer_cannot_send(self, channel_with_members):
        with pytest.raises(PermissionError, match="observer"):
            check_can_send(channel_with_members, "vigil")

    def test_unknown_agent_cannot_send(self, channel_with_members):
        with pytest.raises(PermissionError, match="not a member"):
            check_can_send(channel_with_members, "unknown")

    def test_none_sender_allowed(self, channel_with_members):
        """None sender = external/hub message, always allowed."""
        check_can_send(channel_with_members, None)


class TestCheckCanManage:
    def test_owner_can_manage(self, channel_with_members):
        check_can_manage(channel_with_members, "apollo")

    def test_member_cannot_manage(self, channel_with_members):
        with pytest.raises(PermissionError):
            check_can_manage(channel_with_members, "rex")

    def test_observer_cannot_manage(self, channel_with_members):
        with pytest.raises(PermissionError):
            check_can_manage(channel_with_members, "vigil")
