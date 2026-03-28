# tests/test_telegram_bridge.py
"""Tests for Telegram bridge — all Telegram Bot API calls are mocked."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.telegram.config import TelegramConfig
from src.telegram.formatter import (
    format_agent_message,
    format_human_message,
    format_system_message,
)


# ---------------------------------------------------------------------------
# Formatter tests
# ---------------------------------------------------------------------------

class TestFormatter:
    def test_format_agent_message(self):
        result = format_agent_message("Apollo", "Hello team")
        assert "Apollo" in result
        assert "Hello team" in result
        assert "\U0001f916" in result

    def test_format_system_message(self):
        result = format_system_message("Channel created")
        assert "Channel created" in result
        assert "\u2699" in result

    def test_format_human_message(self):
        result = format_human_message("Filip", "Hi agents")
        assert "Filip" in result
        assert "Hi agents" in result


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------

class TestConfig:
    def test_default_config(self):
        cfg = TelegramConfig()
        assert cfg.enabled is False
        assert cfg.bot_token == ""
        assert cfg.chat_id == 0
        assert cfg.channel_topic_map == {}

    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ENABLED", "true")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001234567890")
        cfg = TelegramConfig.from_env()
        assert cfg.enabled is True
        assert cfg.bot_token == "123:ABC"
        assert cfg.chat_id == -1001234567890

    def test_from_env_disabled(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ENABLED", "false")
        cfg = TelegramConfig.from_env()
        assert cfg.enabled is False


# ---------------------------------------------------------------------------
# Bridge tests
# ---------------------------------------------------------------------------

@pytest.fixture
def config():
    return TelegramConfig(
        enabled=True,
        bot_token="123:ABC",
        chat_id=-1001234567890,
        channel_topic_map={"dev-team": 42, "leaders": 99},
    )


@pytest.fixture
def mock_bot():
    """Create a mock Telegram Bot."""
    bot = AsyncMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
    # get_forum_topics is not a real method, we mock get_forum_topic_icon_stickers
    # and simulate topic discovery through get_updates or manual mapping
    return bot


@pytest.fixture
def mock_hub_client():
    """Mock HTTP client for sending messages to the hub."""
    client = AsyncMock()
    client.post = AsyncMock(return_value=MagicMock(status_code=200))
    return client


@pytest.fixture
def bridge(config, mock_bot, mock_hub_client):
    from src.telegram.bridge import TelegramBridge
    b = TelegramBridge(config=config, hub_base_url="http://localhost:8000")
    b._bot = mock_bot
    b._http_client = mock_hub_client
    return b


class TestBridgeHubToTelegram:
    """Test hub -> Telegram direction (on_hub_message)."""

    async def test_on_hub_message_sends_to_correct_topic(self, bridge, mock_bot):
        from src.notifications.webhooks import WebhookEvent
        event = WebhookEvent(
            event="message",
            channel_id="dev-team",
            timestamp="2026-03-28T12:00:00Z",
            data={"sender_id": "apollo", "sender_name": "Apollo", "text": "Build passed!"},
        )
        await bridge.on_hub_message(event)

        mock_bot.send_message.assert_called_once()
        call_kwargs = mock_bot.send_message.call_args[1]
        assert call_kwargs["chat_id"] == -1001234567890
        assert call_kwargs["message_thread_id"] == 42
        assert "Apollo" in call_kwargs["text"]
        assert "Build passed!" in call_kwargs["text"]

    async def test_on_hub_message_unknown_channel_uses_general(self, bridge, mock_bot):
        from src.notifications.webhooks import WebhookEvent
        event = WebhookEvent(
            event="message",
            channel_id="unknown-channel",
            timestamp="2026-03-28T12:00:00Z",
            data={"sender_id": "rex", "sender_name": "Rex", "text": "Hello"},
        )
        await bridge.on_hub_message(event)

        mock_bot.send_message.assert_called_once()
        call_kwargs = mock_bot.send_message.call_args[1]
        # No topic thread for unknown channels — send to general (no message_thread_id)
        assert "message_thread_id" not in call_kwargs or call_kwargs["message_thread_id"] is None

    async def test_on_hub_message_ignores_non_message_events(self, bridge, mock_bot):
        from src.notifications.webhooks import WebhookEvent
        event = WebhookEvent(
            event="member_join",
            channel_id="dev-team",
            timestamp="2026-03-28T12:00:00Z",
            data={"agent_id": "nova"},
        )
        await bridge.on_hub_message(event)
        mock_bot.send_message.assert_not_called()

    async def test_on_hub_system_event_formats_correctly(self, bridge, mock_bot):
        from src.notifications.webhooks import WebhookEvent
        event = WebhookEvent(
            event="message",
            channel_id="leaders",
            timestamp="2026-03-28T12:00:00Z",
            data={"sender_id": "system", "sender_name": "System", "text": "Channel archived"},
        )
        await bridge.on_hub_message(event)

        call_kwargs = mock_bot.send_message.call_args[1]
        assert call_kwargs["message_thread_id"] == 99


class TestBridgeTelegramToHub:
    """Test Telegram -> hub direction (on_telegram_message)."""

    async def test_on_telegram_message_sends_to_hub(self, bridge, mock_hub_client):
        update = MagicMock()
        update.message.message_thread_id = 42
        update.message.text = "Lets deploy"
        update.message.from_user.first_name = "Filip"
        update.message.from_user.username = "filip"
        update.message.chat.id = -1001234567890

        await bridge.on_telegram_message(update)

        mock_hub_client.post.assert_called_once()
        call_args = mock_hub_client.post.call_args
        assert "/api/channels/dev-team/messages" in call_args[0][0] or "dev-team" in str(call_args)

    async def test_on_telegram_message_unknown_topic_ignored(self, bridge, mock_hub_client):
        update = MagicMock()
        update.message.message_thread_id = 9999  # not in topic map
        update.message.text = "Hello"
        update.message.from_user.first_name = "Filip"
        update.message.from_user.username = "filip"
        update.message.chat.id = -1001234567890

        await bridge.on_telegram_message(update)

        mock_hub_client.post.assert_not_called()

    async def test_on_telegram_message_no_topic_ignored(self, bridge, mock_hub_client):
        update = MagicMock()
        update.message.message_thread_id = None
        update.message.text = "General message"
        update.message.from_user.first_name = "Filip"
        update.message.chat.id = -1001234567890

        await bridge.on_telegram_message(update)

        mock_hub_client.post.assert_not_called()

    async def test_on_telegram_message_wrong_chat_ignored(self, bridge, mock_hub_client):
        update = MagicMock()
        update.message.message_thread_id = 42
        update.message.text = "Spam"
        update.message.from_user.first_name = "Spammer"
        update.message.chat.id = -999  # wrong chat

        await bridge.on_telegram_message(update)

        mock_hub_client.post.assert_not_called()


class TestBridgeTopicMapping:
    """Test topic discovery and reverse mapping."""

    def test_reverse_topic_map(self, bridge):
        reverse = bridge._reverse_topic_map
        assert reverse[42] == "dev-team"
        assert reverse[99] == "leaders"

    def test_channel_for_topic(self, bridge):
        assert bridge._channel_for_topic(42) == "dev-team"
        assert bridge._channel_for_topic(99) == "leaders"
        assert bridge._channel_for_topic(9999) is None
