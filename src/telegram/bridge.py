# src/telegram/bridge.py
"""Bidirectional Telegram <-> A2A Hub bridge.

Hub -> Telegram: After fan-out, agent responses are forwarded to Telegram topics.
Telegram -> Hub: Filip's messages in Telegram topics are sent as A2A SendMessage.

Uses polling (not webhooks) since the server has no public URL.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

import httpx

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
        self._http_client: httpx.AsyncClient | None = None
        self._polling_task: asyncio.Task | None = None
        self._running = False

    @property
    def _reverse_topic_map(self) -> dict[int, str]:
        """Reverse map: topic_id -> channel_id."""
        return {v: k for k, v in self._config.channel_topic_map.items()}

    def _channel_for_topic(self, topic_id: int) -> str | None:
        return self._reverse_topic_map.get(topic_id)

    async def start(self) -> None:
        """Initialize bot, HTTP client, and start polling."""
        try:
            from telegram import Bot
            self._bot = Bot(token=self._config.bot_token)
        except ImportError:
            logger.error("python-telegram-bot not installed — pip install python-telegram-bot")
            return

        self._http_client = httpx.AsyncClient(timeout=120.0)
        self._running = True

        # Start polling for Telegram messages
        self._polling_task = asyncio.create_task(self._poll_telegram())

        logger.info(
            "Telegram bridge started (polling mode): chat_id=%s, channels=%s",
            self._config.chat_id,
            list(self._config.channel_topic_map.keys()),
        )

    async def stop(self) -> None:
        """Stop polling and clean up."""
        self._running = False
        if self._polling_task:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
        if self._http_client:
            await self._http_client.aclose()

    # ── Telegram polling ─────────────────────────────────────

    async def _poll_telegram(self) -> None:
        """Long-poll Telegram Bot API for new messages."""
        offset = 0
        logger.info("Telegram polling started")

        while self._running:
            try:
                url = f"https://api.telegram.org/bot{self._config.bot_token}/getUpdates"
                params = {"offset": offset, "timeout": 30, "allowed_updates": ["message"]}

                resp = await self._http_client.get(url, params=params, timeout=45.0)
                data = resp.json()

                if not data.get("ok"):
                    logger.error("Telegram API error: %s", data)
                    await asyncio.sleep(5)
                    continue

                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    message = update.get("message")
                    if message:
                        await self._handle_telegram_message(message)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Telegram polling error")
                await asyncio.sleep(5)

    async def _handle_telegram_message(self, message: dict) -> None:
        """Process incoming Telegram message — forward to hub if in a mapped topic."""
        chat_id = message.get("chat", {}).get("id")
        if chat_id != self._config.chat_id:
            return

        topic_id = message.get("message_thread_id")
        if topic_id is None:
            return  # General topic, not mapped

        channel_id = self._channel_for_topic(topic_id)
        if channel_id is None:
            return  # Unknown topic

        text = message.get("text", "")
        if not text:
            return

        user = message.get("from", {})
        user_name = user.get("first_name", "Unknown")

        logger.info("Telegram -> Hub: [%s] %s in #%s: %s", user_name, topic_id, channel_id, text[:80])

        # Send to hub via A2A JSON-RPC
        await self._send_to_hub(channel_id, user_name, text)

    async def _send_to_hub(self, channel_id: str, sender_name: str, text: str) -> None:
        """Send message to hub via A2A SendMessage (JSON-RPC)."""
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "messageId": str(uuid.uuid4()),
                    "parts": [{"kind": "text", "text": text}],
                    "metadata": {
                        "channel_id": channel_id,
                        "sender_name": sender_name,
                        "source": "telegram",
                    },
                },
            },
        }

        try:
            resp = await self._http_client.post(
                f"{self._hub_base_url}/",
                json=payload,
                timeout=120.0,
            )
            result = resp.json()

            # Forward agent responses back to Telegram
            artifacts = result.get("result", {}).get("artifacts", [])
            if artifacts:
                await self._send_responses_to_telegram(channel_id, artifacts)
            else:
                logger.warning("No artifacts in hub response for #%s", channel_id)

        except Exception:
            logger.exception("Failed to send to hub: #%s", channel_id)

    # ── Hub -> Telegram ──────────────────────────────────────

    async def _send_responses_to_telegram(self, channel_id: str, artifacts: list[dict]) -> None:
        """Send agent responses back to the Telegram topic."""
        topic_id = self._config.channel_topic_map.get(channel_id)
        if topic_id is None:
            return

        for artifact in artifacts:
            name = artifact.get("name", "Agent")
            parts = artifact.get("parts", [])
            text = ""
            for part in parts:
                if part.get("kind") == "text":
                    text += part.get("text", "")

            if not text or text.startswith("[Error from"):
                continue  # Skip errors from agents that aren't running

            # Extract agent name from "Response from Apollo"
            agent_name = name.replace("Response from ", "")
            formatted = format_agent_message(agent_name, text)

            # Telegram max message length is 4096
            if len(formatted) > 4096:
                formatted = formatted[:4093] + "..."

            await self._send_telegram_message(formatted, topic_id)

    async def _send_telegram_message(self, text: str, topic_id: int | None = None) -> None:
        """Send a message to the Telegram supergroup (optionally to a specific topic)."""
        if self._bot is None:
            return

        try:
            kwargs = {
                "chat_id": self._config.chat_id,
                "text": text,
                "parse_mode": None,  # Plain text
            }
            if topic_id is not None:
                kwargs["message_thread_id"] = topic_id

            await self._bot.send_message(**kwargs)
        except Exception:
            logger.exception("Failed to send Telegram message to topic %s", topic_id)

    # ── Public API for hub integration ────────────────────────

    async def notify_channel_message(self, channel_id: str, sender_name: str, text: str) -> None:
        """Called by hub after agent responds — forwards to Telegram."""
        topic_id = self._config.channel_topic_map.get(channel_id)
        if topic_id is None:
            return

        formatted = format_agent_message(sender_name, text)
        if len(formatted) > 4096:
            formatted = formatted[:4093] + "..."

        await self._send_telegram_message(formatted, topic_id)
