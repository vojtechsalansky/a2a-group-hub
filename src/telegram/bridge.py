# src/telegram/bridge.py
"""Bidirectional Telegram <-> A2A Hub bridge.

Hub -> Telegram: Receives webhook events, formats and sends to Telegram topics.
Telegram -> Hub: Receives Telegram updates, sends A2A messages to hub channels.
"""

from __future__ import annotations

import logging

import httpx

from src.notifications.webhooks import WebhookEvent
from src.telegram.config import TelegramConfig
from src.telegram.formatter import format_agent_message, format_system_message

logger = logging.getLogger("a2a-hub.telegram")


class TelegramBridge:
    """Bidirectional Telegram <-> A2A Hub bridge."""

    def __init__(
        self,
        config: TelegramConfig,
        hub_base_url: str = "http://localhost:8000",
    ) -> None:
        self._config = config
        self._hub_base_url = hub_base_url.rstrip("/")
        self._bot = None
        self._http_client = None

    @property
    def _reverse_topic_map(self) -> dict[int, str]:
        """Reverse map: topic_id -> channel_id."""
        return {v: k for k, v in self._config.channel_topic_map.items()}

    def _channel_for_topic(self, topic_id: int) -> str | None:
        """Look up channel_id for a Telegram topic thread ID."""
        return self._reverse_topic_map.get(topic_id)

    async def start(self) -> None:
        """Initialize bot and HTTP client."""
        try:
            from telegram import Bot
            self._bot = Bot(token=self._config.bot_token)
        except ImportError:
            logger.error("python-telegram-bot not installed")
            raise
        self._http_client = httpx.AsyncClient(timeout=15.0)
        logger.info(
            "Telegram bridge started: chat_id=%s, channels=%s",
            self._config.chat_id,
            list(self._config.channel_topic_map.keys()),
        )

    async def stop(self) -> None:
        """Clean up resources."""
        if self._http_client:
            await self._http_client.aclose()

    # ── Hub -> Telegram ───────────────────────────────────────

    async def on_hub_message(self, event: WebhookEvent) -> None:
        """Forward a hub webhook event to the corresponding Telegram topic."""
        if event.event != "message":
            return

        if self._bot is None:
            logger.warning("Bridge not started, ignoring event")
            return

        sender_name = event.data.get("sender_name", event.data.get("sender_id", "Unknown"))
        text = event.data.get("text", "")

        if event.data.get("sender_id") == "system":
            formatted = format_system_message(text)
        else:
            formatted = format_agent_message(sender_name, text)

        topic_id = self._config.channel_topic_map.get(event.channel_id)

        kwargs: dict = {
            "chat_id": self._config.chat_id,
            "text": formatted,
        }
        if topic_id is not None:
            kwargs["message_thread_id"] = topic_id
        else:
            kwargs["message_thread_id"] = None

        await self._bot.send_message(**kwargs)

    # ── Telegram -> Hub ───────────────────────────────────────

    async def on_telegram_message(self, update) -> None:
        """Forward a Telegram message to the corresponding hub channel."""
        message = update.message
        if message is None:
            return

        # Only process messages from our supergroup
        if message.chat.id != self._config.chat_id:
            return

        # Must be in a topic thread to route to a channel
        topic_id = message.message_thread_id
        if topic_id is None:
            return

        channel_id = self._channel_for_topic(topic_id)
        if channel_id is None:
            return

        user_name = message.from_user.first_name or "Unknown"
        text = message.text or ""

        if self._http_client is None:
            logger.warning("Bridge not started, ignoring Telegram message")
            return

        url = f"{self._hub_base_url}/api/channels/{channel_id}/messages"
        payload = {
            "sender_id": f"telegram:{message.from_user.username or user_name}",
            "sender_name": user_name,
            "text": text,
            "source": "telegram",
        }

        try:
            await self._http_client.post(url, json=payload)
        except Exception:
            logger.exception("Failed to forward Telegram message to hub: %s", url)

    # ── Topic discovery ───────────────────────────────────────

    async def discover_topics(self, channel_names: list[str]) -> dict[str, int]:
        """Auto-map Telegram forum topics to channel IDs by name matching.

        This requires the bot to have access to forum topic info.
        Returns a dict of channel_id -> topic_id for matches found.
        """
        # This is a best-effort approach — Telegram Bot API doesn't have
        # a direct "list all topics" endpoint. In practice, topics are
        # configured manually via channel_topic_map or discovered through
        # the getForumTopicIconStickers + getUpdates approach.
        logger.info("Topic auto-discovery requested for: %s", channel_names)
        return self._config.channel_topic_map
