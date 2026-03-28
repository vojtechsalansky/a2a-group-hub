# tests/test_channels_models.py
"""Tests for channel models and role-based membership."""

import pytest
from src.channels.models import Channel, ChannelMember, MemberRole


class TestMemberRole:
    def test_roles_exist(self):
        assert MemberRole.owner == "owner"
        assert MemberRole.member == "member"
        assert MemberRole.observer == "observer"

    def test_can_send(self):
        assert MemberRole.owner.can_send is True
        assert MemberRole.member.can_send is True
        assert MemberRole.observer.can_send is False

    def test_can_manage(self):
        assert MemberRole.owner.can_manage is True
        assert MemberRole.member.can_manage is False
        assert MemberRole.observer.can_manage is False


class TestChannelMember:
    def test_create_member(self):
        m = ChannelMember(agent_id="rex", name="Rex", url="http://localhost:9001")
        assert m.role == MemberRole.member
        assert m.auth_token is None

    def test_auth_headers_with_token(self):
        m = ChannelMember(agent_id="rex", name="Rex", url="http://localhost:9001", auth_token="secret")
        assert m.auth_headers == {"Authorization": "Bearer secret"}

    def test_auth_headers_without_token(self):
        m = ChannelMember(agent_id="rex", name="Rex", url="http://localhost:9001")
        assert m.auth_headers == {}


class TestChannel:
    def test_create_channel(self):
        ch = Channel(channel_id="dev", name="dev-team")
        assert ch.channel_id == "dev"
        assert ch.context_id  # auto-generated UUID
        assert ch.members == {}
        assert ch.message_count == 0

    def test_add_and_get_members(self):
        ch = Channel(channel_id="dev", name="dev-team")
        rex = ChannelMember(agent_id="rex", name="Rex", url="http://localhost:9001")
        pixel = ChannelMember(agent_id="pixel", name="Pixel", url="http://localhost:9002")
        ch.add_member(rex)
        ch.add_member(pixel)
        assert len(ch.members) == 2

    def test_get_peers_excludes_sender(self):
        ch = Channel(channel_id="dev", name="dev-team")
        ch.add_member(ChannelMember(agent_id="rex", name="Rex", url="http://localhost:9001"))
        ch.add_member(ChannelMember(agent_id="pixel", name="Pixel", url="http://localhost:9002"))
        ch.add_member(ChannelMember(agent_id="vigil", name="Vigil", url="http://localhost:9003", role=MemberRole.observer))
        peers = ch.get_peers(exclude_agent_id="rex")
        assert len(peers) == 2
        assert all(p.agent_id != "rex" for p in peers)

    def test_get_sendable_peers(self):
        """Sendable peers = members who can respond (not observers)."""
        ch = Channel(channel_id="dev", name="dev-team")
        ch.add_member(ChannelMember(agent_id="rex", name="Rex", url="http://localhost:9001"))
        ch.add_member(ChannelMember(agent_id="vigil", name="Vigil", url="http://localhost:9003", role=MemberRole.observer))
        sendable = ch.get_sendable_peers(exclude_agent_id=None)
        assert len(sendable) == 1
        assert sendable[0].agent_id == "rex"

    def test_get_observers(self):
        ch = Channel(channel_id="dev", name="dev-team")
        ch.add_member(ChannelMember(agent_id="rex", name="Rex", url="http://localhost:9001"))
        ch.add_member(ChannelMember(agent_id="vigil", name="Vigil", url="http://localhost:9003", role=MemberRole.observer))
        observers = ch.get_observers()
        assert len(observers) == 1
        assert observers[0].agent_id == "vigil"

    def test_remove_member(self):
        ch = Channel(channel_id="dev", name="dev-team")
        ch.add_member(ChannelMember(agent_id="rex", name="Rex", url="http://localhost:9001"))
        removed = ch.remove_member("rex")
        assert removed is not None
        assert len(ch.members) == 0

    def test_remove_nonexistent_member(self):
        ch = Channel(channel_id="dev", name="dev-team")
        removed = ch.remove_member("nobody")
        assert removed is None
