# tests/test_integration_openclaw.py
"""Comprehensive integration test for the OpenClaw A2A hub topology.

Verifies bootstrap, fan-out, permissions, Telegram bridge, and
cross-channel isolation working together.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from a2a.types import Message, MessageSendParams, Part, Role, TextPart

from src.bootstrap import OPENCLAW_CHANNELS, bootstrap_channels
from src.channels.models import MemberRole
from src.channels.permissions import PermissionError, check_can_send
from src.channels.registry import ChannelRegistry
from src.hub.fanout import FanOutEngine, FanOutResult
from src.hub.handler import GroupChatHub
from src.notifications.webhooks import WebhookEvent
from src.storage.memory import InMemoryBackend
from src.telegram.bridge import TelegramBridge
from src.telegram.config import TelegramConfig


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def storage() -> InMemoryBackend:
    return InMemoryBackend()


@pytest.fixture
def registry(storage: InMemoryBackend) -> ChannelRegistry:
    return ChannelRegistry(storage)


@pytest.fixture
async def bootstrapped_registry(registry: ChannelRegistry) -> ChannelRegistry:
    await bootstrap_channels(registry)
    return registry


@pytest.fixture
def hub(bootstrapped_registry: ChannelRegistry, storage: InMemoryBackend) -> GroupChatHub:
    return GroupChatHub(registry=bootstrapped_registry, storage=storage)


# ── 1. Bootstrap verification ────────────────────────────────────────────────


class TestBootstrapVerification:

    EXPECTED_MEMBER_COUNTS = {
        "leaders": 7,
        "dev-team": 7,
        "testing-team": 5,
        "ops-team": 4,
        "pm-team": 3,
        "knowledge-team": 4,
        "filip-nexus": 1,
    }

    async def test_creates_all_seven_channels(self, bootstrapped_registry: ChannelRegistry) -> None:
        channels = await bootstrapped_registry.list_channels()
        assert len(channels) == 7

    async def test_channel_ids_match_design(self, bootstrapped_registry: ChannelRegistry) -> None:
        channels = await bootstrapped_registry.list_channels()
        ids = {ch.channel_id for ch in channels}
        assert ids == set(self.EXPECTED_MEMBER_COUNTS.keys())

    async def test_member_counts(self, bootstrapped_registry: ChannelRegistry) -> None:
        for channel_id, expected_count in self.EXPECTED_MEMBER_COUNTS.items():
            ch = await bootstrapped_registry.get_channel(channel_id)
            assert ch is not None, f"Channel {channel_id} not found"
            assert len(ch.members) == expected_count, (
                f"{channel_id}: expected {expected_count} members, got {len(ch.members)}"
            )

    async def test_owner_roles(self, bootstrapped_registry: ChannelRegistry) -> None:
        expected_owners = {
            "leaders": "nexus",
            "dev-team": "apollo",
            "testing-team": "sentinel",
            "ops-team": "forge",
            "pm-team": "mona",
            "knowledge-team": "sage",
            "filip-nexus": "nexus",
        }
        for channel_id, owner_id in expected_owners.items():
            ch = await bootstrapped_registry.get_channel(channel_id)
            assert ch.members[owner_id].role == MemberRole.owner, (
                f"{channel_id}: {owner_id} should be owner"
            )

    async def test_observer_roles(self, bootstrapped_registry: ChannelRegistry) -> None:
        # vigil is observer in leaders and dev-team
        for channel_id in ("leaders", "dev-team"):
            ch = await bootstrapped_registry.get_channel(channel_id)
            assert ch.members["vigil"].role == MemberRole.observer

    async def test_member_roles(self, bootstrapped_registry: ChannelRegistry) -> None:
        dev = await bootstrapped_registry.get_channel("dev-team")
        for agent_id in ("rex", "pixel", "nova", "swift", "hawk"):
            assert dev.members[agent_id].role == MemberRole.member

    async def test_idempotency(self, bootstrapped_registry: ChannelRegistry) -> None:
        """Running bootstrap twice produces the same result."""
        channels_before = await bootstrapped_registry.list_channels()
        members_before = {
            ch.channel_id: set(ch.members.keys())
            for ch in channels_before
        }

        await bootstrap_channels(bootstrapped_registry)

        channels_after = await bootstrapped_registry.list_channels()
        assert len(channels_before) == len(channels_after)
        for ch in channels_after:
            assert set(ch.members.keys()) == members_before[ch.channel_id]


# ── 2. Fan-out with bootstrap channels ──────────────────────────────────────


class TestFanOut:

    async def test_fan_out_excludes_sender(self, bootstrapped_registry: ChannelRegistry) -> None:
        """When apollo sends to dev-team, apollo should be excluded from fan-out."""
        dev = await bootstrapped_registry.get_channel("dev-team")
        peers = dev.get_sendable_peers(exclude_agent_id="apollo")
        peer_ids = {m.agent_id for m in peers}
        assert "apollo" not in peer_ids
        assert peer_ids == {"rex", "pixel", "nova", "swift", "hawk"}

    async def test_fan_out_includes_all_members(self, bootstrapped_registry: ChannelRegistry) -> None:
        """Fan-out from apollo should reach all 5 dev members."""
        dev = await bootstrapped_registry.get_channel("dev-team")
        peers = dev.get_sendable_peers(exclude_agent_id="apollo")
        assert len(peers) == 5

    async def test_observers_excluded_from_sendable_peers(self, bootstrapped_registry: ChannelRegistry) -> None:
        """Vigil (observer) should NOT be in get_sendable_peers."""
        dev = await bootstrapped_registry.get_channel("dev-team")
        sendable = dev.get_sendable_peers(exclude_agent_id="apollo")
        sendable_ids = {m.agent_id for m in sendable}
        assert "vigil" not in sendable_ids

    async def test_observers_in_get_observers(self, bootstrapped_registry: ChannelRegistry) -> None:
        """Vigil should be in the observer list for fire-and-forget."""
        dev = await bootstrapped_registry.get_channel("dev-team")
        observers = dev.get_observers()
        assert len(observers) == 1
        assert observers[0].agent_id == "vigil"

    async def test_fan_out_engine_calls_members_and_observers(
        self, bootstrapped_registry: ChannelRegistry
    ) -> None:
        """Integration test with mocked _send_to_agent."""
        dev = await bootstrapped_registry.get_channel("dev-team")
        engine = FanOutEngine()

        sent_to: list[str] = []

        async def mock_send(member, **kwargs) -> FanOutResult:
            sent_to.append(member.agent_id)
            return FanOutResult(
                agent_id=member.agent_id,
                agent_name=member.name,
                response_text=f"Response from {member.name}",
            )

        engine._send_to_agent = mock_send

        parts = [Part(root=TextPart(text="Hello team"))]
        results = await engine.fan_out(
            channel=dev,
            message_parts=parts,
            sender_id="apollo",
            context_id=dev.context_id,
        )

        # Should get results from 5 members
        assert len(results) == 5
        result_agents = {r.agent_id for r in results}
        assert result_agents == {"rex", "pixel", "nova", "swift", "hawk"}

        # Observer (vigil) should also have been called (fire-and-forget)
        # Give the fire-and-forget task a moment to complete
        await asyncio.sleep(0.05)
        assert "vigil" in sent_to

        await engine.close()


# ── 3. Permission checks ────────────────────────────────────────────────────


class TestPermissions:

    async def test_observer_cannot_send(self, bootstrapped_registry: ChannelRegistry) -> None:
        """Vigil (observer) should NOT be allowed to send to dev-team."""
        dev = await bootstrapped_registry.get_channel("dev-team")
        with pytest.raises(PermissionError, match="observer"):
            check_can_send(dev, "vigil")

    async def test_owner_can_send(self, bootstrapped_registry: ChannelRegistry) -> None:
        """Apollo (owner) should be allowed to send to dev-team."""
        dev = await bootstrapped_registry.get_channel("dev-team")
        check_can_send(dev, "apollo")  # should not raise

    async def test_member_can_send(self, bootstrapped_registry: ChannelRegistry) -> None:
        """Rex (member) should be allowed to send to dev-team."""
        dev = await bootstrapped_registry.get_channel("dev-team")
        check_can_send(dev, "rex")  # should not raise

    async def test_non_member_cannot_send(self, bootstrapped_registry: ChannelRegistry) -> None:
        """Forge (not in dev-team) should NOT be allowed to send to dev-team."""
        dev = await bootstrapped_registry.get_channel("dev-team")
        with pytest.raises(PermissionError, match="not a member"):
            check_can_send(dev, "forge")

    async def test_hub_handler_enforces_permissions(
        self, hub: GroupChatHub, bootstrapped_registry: ChannelRegistry
    ) -> None:
        """GroupChatHub should return permission denied for observer."""
        msg = Message(
            role=Role.user,
            parts=[Part(root=TextPart(text="Hello"))],
            message_id=str(uuid.uuid4()),
            metadata={"channel_id": "dev-team", "sender_id": "vigil"},
        )
        result = await hub.on_message_send(MessageSendParams(message=msg))
        # Should be a Message (not Task) with permission denied
        assert isinstance(result, Message)
        text = result.parts[0].root.text
        assert "Permission denied" in text


# ── 4. Telegram bridge (mocked) ─────────────────────────────────────────────


class TestTelegramBridge:

    @pytest.fixture
    def tg_config(self) -> TelegramConfig:
        return TelegramConfig(
            enabled=True,
            bot_token="123:ABC",
            chat_id=-1001234567890,
            channel_topic_map={
                "leaders": 10,
                "dev-team": 20,
                "testing-team": 30,
                "ops-team": 40,
                "pm-team": 50,
                "knowledge-team": 60,
                "filip-nexus": 70,
            },
        )

    @pytest.fixture
    def bridge(self, tg_config: TelegramConfig) -> TelegramBridge:
        b = TelegramBridge(config=tg_config, hub_base_url="http://localhost:8000")
        b._system_bot = AsyncMock()
        b._system_bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
        b._agent_bots = {"apollo": b._system_bot, "rex": b._system_bot}
        b._http_client = AsyncMock()
        b._http_client.post = AsyncMock(return_value=MagicMock(status_code=200))
        return b

    async def test_hub_to_telegram_correct_topic(self, bridge: TelegramBridge) -> None:
        """Hub message to dev-team should go to topic 20."""
        event = WebhookEvent(
            event="message",
            channel_id="dev-team",
            timestamp="2026-03-28T12:00:00Z",
            data={"sender_id": "apollo", "sender_name": "Apollo", "text": "Build passed!"},
        )
        await bridge.on_hub_message(event)

        bridge._agent_bots["apollo"].send_message.assert_called_once()
        kwargs = bridge._agent_bots["apollo"].send_message.call_args[1]
        assert kwargs["chat_id"] == -1001234567890
        assert kwargs["message_thread_id"] == 20
        assert "Apollo" in kwargs["text"]
        assert "Build passed!" in kwargs["text"]

    async def test_telegram_to_hub_routes_correctly(self, bridge: TelegramBridge) -> None:
        """Telegram message in topic 20 should route to dev-team channel."""
        update = MagicMock()
        update.message.message_thread_id = 20
        update.message.text = "Deploy now"
        update.message.from_user.first_name = "Filip"
        update.message.from_user.username = "filip"
        update.message.chat.id = -1001234567890

        await bridge.on_telegram_message(update)

        bridge._http_client.post.assert_called_once()
        url = bridge._http_client.post.call_args[0][0]
        assert "dev-team" in url

    async def test_telegram_wrong_topic_ignored(self, bridge: TelegramBridge) -> None:
        """Message in unknown topic should not route anywhere."""
        update = MagicMock()
        update.message.message_thread_id = 9999
        update.message.text = "Hello"
        update.message.from_user.first_name = "Test"
        update.message.from_user.username = "test"
        update.message.chat.id = -1001234567890

        await bridge.on_telegram_message(update)
        bridge._http_client.post.assert_not_called()


# ── 5. Cross-channel isolation ──────────────────────────────────────────────


class TestCrossChannelIsolation:

    async def test_dev_team_members_not_in_ops(self, bootstrapped_registry: ChannelRegistry) -> None:
        """Dev-team members (rex, pixel, nova, swift, hawk) should not be in ops-team."""
        ops = await bootstrapped_registry.get_channel("ops-team")
        dev_members = {"rex", "pixel", "nova", "swift", "hawk"}
        ops_members = set(ops.members.keys())
        assert dev_members.isdisjoint(ops_members)

    async def test_ops_team_members_not_in_dev(self, bootstrapped_registry: ChannelRegistry) -> None:
        """Ops-team members (bolt, iris, kai) should not be in dev-team."""
        dev = await bootstrapped_registry.get_channel("dev-team")
        ops_only = {"bolt", "iris", "kai"}
        dev_members = set(dev.members.keys())
        assert ops_only.isdisjoint(dev_members)

    async def test_fan_out_does_not_leak_across_channels(
        self, bootstrapped_registry: ChannelRegistry
    ) -> None:
        """Fan-out in dev-team should only reach dev-team members."""
        dev = await bootstrapped_registry.get_channel("dev-team")
        ops = await bootstrapped_registry.get_channel("ops-team")

        dev_peers = dev.get_sendable_peers(exclude_agent_id="apollo")
        dev_peer_ids = {m.agent_id for m in dev_peers}

        ops_only_agents = set(ops.members.keys()) - set(dev.members.keys())
        # No ops-only agent should appear in dev fan-out
        assert dev_peer_ids.isdisjoint(ops_only_agents)

    async def test_leader_channel_spans_teams(self, bootstrapped_registry: ChannelRegistry) -> None:
        """Leaders channel should contain leads from all teams."""
        leaders = await bootstrapped_registry.get_channel("leaders")
        leader_ids = set(leaders.members.keys())
        expected_leads = {"nexus", "apollo", "sentinel", "forge", "mona", "sage", "vigil"}
        assert leader_ids == expected_leads

    async def test_message_storage_per_channel(
        self, hub: GroupChatHub, storage: InMemoryBackend
    ) -> None:
        """Messages sent to dev-team should not appear in ops-team storage."""
        # Mock the fan-out to avoid real HTTP calls
        hub._fanout_engine._send_to_agent = AsyncMock(
            return_value=FanOutResult(agent_id="rex", agent_name="Rex", response_text="Ack")
        )

        msg = Message(
            role=Role.user,
            parts=[Part(root=TextPart(text="Test message"))],
            message_id=str(uuid.uuid4()),
            metadata={"channel_id": "dev-team", "sender_id": "apollo"},
        )
        await hub.on_message_send(MessageSendParams(message=msg))

        dev_msgs = await storage.get_messages("dev-team")
        ops_msgs = await storage.get_messages("ops-team")
        assert len(dev_msgs) > 0
        assert len(ops_msgs) == 0
