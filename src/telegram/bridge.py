# src/telegram/bridge.py
"""Bidirectional Telegram <-> A2A Hub bridge.

Hub -> Telegram: Agent responses sent via each agent's OWN bot.
Telegram -> Hub: Filip's messages polled via System bot.
Errors -> system-log topic via System bot.

Each agent has its own Telegram bot — responses appear as coming from
the correct agent, not from a generic system bot.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

import httpx

from src.telegram.config import TelegramConfig
from src.telegram.formatter import format_agent_message, format_system_message

logger = logging.getLogger("a2a-hub.telegram")

SYSTEM_LOG_TOPIC_ID = 9


class TelegramBridge:

    def __init__(
        self,
        config: TelegramConfig,
        hub_base_url: str = "http://localhost:8000",
    ) -> None:
        self._config = config
        self._hub_base_url = hub_base_url.rstrip("/")
        self._system_bot = None         # System bot — polling + errors
        self._agent_bots: dict = {}     # agent_id → Bot instance
        self._http_client: httpx.AsyncClient | None = None
        self._polling_task: asyncio.Task | None = None
        self._running = False
        self._poll_offset = 0

    @property
    def _reverse_topic_map(self) -> dict[int, str]:
        return {v: k for k, v in self._config.channel_topic_map.items()}

    def _channel_for_topic(self, topic_id: int) -> str | None:
        return self._reverse_topic_map.get(topic_id)

    async def start(self) -> None:
        """Initialize bots, flush old messages, start polling."""
        try:
            from telegram import Bot
        except ImportError:
            logger.error("python-telegram-bot not installed")
            return

        # System bot for polling + error log
        self._system_bot = Bot(token=self._config.bot_token)

        # Per-agent bots for sending responses
        for agent_id, token in self._config.agent_tokens.items():
            self._agent_bots[agent_id] = Bot(token=token)
            logger.info("Registered agent bot: %s", agent_id)

        self._http_client = httpx.AsyncClient(timeout=120.0)
        self._running = True

        await self._flush_old_updates()
        self._polling_task = asyncio.create_task(self._poll_telegram())

        logger.info(
            "Telegram bridge started: chat_id=%s, agent_bots=%d, channels=%s",
            self._config.chat_id,
            len(self._agent_bots),
            list(self._config.channel_topic_map.keys()),
        )

    async def stop(self) -> None:
        self._running = False
        if self._polling_task:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
        if self._http_client:
            await self._http_client.aclose()

    # ── Flush old updates on startup ─────────────────────────

    async def _flush_old_updates(self) -> None:
        url = f"https://api.telegram.org/bot{self._config.bot_token}/getUpdates"
        flushed = 0
        offset = 0
        while True:
            resp = await self._http_client.get(url, params={"offset": offset, "timeout": 0})
            data = resp.json()
            updates = data.get("result", [])
            if not updates:
                break
            flushed += len(updates)
            offset = updates[-1]["update_id"] + 1
        self._poll_offset = offset
        logger.info("Flushed %d old Telegram updates", flushed)

    # ── Telegram polling ─────────────────────────────────────

    async def _poll_telegram(self) -> None:
        offset = self._poll_offset
        logger.info("Telegram polling started (offset=%d)", offset)

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
        chat_id = message.get("chat", {}).get("id")
        if chat_id != self._config.chat_id:
            return

        from_user = message.get("from", {})
        if from_user.get("is_bot", False):
            return

        topic_id = message.get("message_thread_id")
        if topic_id is None:
            return

        channel_id = self._channel_for_topic(topic_id)
        if channel_id is None:
            return

        text = message.get("text", "")
        if not text:
            return

        user_name = from_user.get("first_name", "Unknown")
        logger.info("Telegram -> Hub: [%s] in #%s: %s", user_name, channel_id, text[:80])

        await self._send_to_hub(channel_id, user_name, text)

    # ── Telegram -> Hub ──────────────────────────────────────

    async def _send_to_hub(self, channel_id: str, sender_name: str, text: str) -> None:
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

            artifacts = result.get("result", {}).get("artifacts", [])
            if artifacts:
                await self._send_responses_to_telegram(channel_id, artifacts)

        except Exception:
            logger.exception("Failed to send to hub: #%s", channel_id)

    # ── Hub -> Telegram (per-agent bots) ─────────────────────

    async def _send_responses_to_telegram(self, channel_id: str, artifacts: list[dict]) -> None:
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

            if not text:
                continue

            agent_name = name.replace("Response from ", "")
            agent_id = agent_name.lower()

            # Detect errors — skip DNS errors silently, log others to system-log
            is_dns_error = "Temporary failure in name resolution" in text
            is_error = (
                text.startswith("[Error from")
                or text.startswith("[error]")
                or "Rate limited" in text
                or "error**:" in text
            )

            if is_dns_error:
                # Agent container not running — skip silently
                continue

            if is_error:
                error_msg = format_system_message(f"[{agent_name}] {text[:500]}")
                await self._send_telegram_message_as(self._system_bot, error_msg, SYSTEM_LOG_TOPIC_ID)
                continue

            # Real response — send via agent's own bot
            bot = self._agent_bots.get(agent_id, self._system_bot)
            if bot is None:
                continue

            # Agent sends just their message text (no prefix needed — bot name IS the agent)
            msg_text = text
            if len(msg_text) > 4096:
                msg_text = msg_text[:4093] + "..."

            await self._send_telegram_message_as(bot, msg_text, topic_id)

    # ── Public API (used by webhook dispatcher & server) ───

    async def on_hub_message(self, event) -> None:
        """Handle a WebhookEvent from the hub and forward to Telegram."""
        if event.event != "message":
            return

        data = event.data
        sender_name = data.get("sender_name", data.get("sender_id", "Agent"))
        text = data.get("text", "")
        if not text:
            return

        topic_id = self._config.channel_topic_map.get(event.channel_id)

        if sender_name.lower() == "system":
            msg = format_system_message(text)
        else:
            msg = format_agent_message(sender_name, text)

        bot = self._bot if hasattr(self, "_bot") and self._bot else self._system_bot
        if bot is None:
            return

        kwargs = {"chat_id": self._config.chat_id, "text": msg}
        if topic_id is not None:
            kwargs["message_thread_id"] = topic_id

        try:
            await bot.send_message(**kwargs)
        except Exception:
            logger.exception("Failed to send hub message to Telegram")

    async def on_telegram_message(self, update) -> None:
        """Handle a Telegram Update object and forward to the hub."""
        message = update.message
        if message is None:
            return

        chat_id = message.chat.id
        if chat_id != self._config.chat_id:
            return

        topic_id = message.message_thread_id
        if topic_id is None:
            return

        channel_id = self._channel_for_topic(topic_id)
        if channel_id is None:
            return

        text = message.text or ""
        if not text:
            return

        user_name = getattr(message.from_user, "first_name", "Unknown")
        logger.info("Telegram -> Hub: [%s] in #%s: %s", user_name, channel_id, text[:80])

        if self._http_client is None:
            return

        payload = {
            "sender_name": user_name,
            "text": text,
            "source": "telegram",
        }
        try:
            await self._http_client.post(
                f"{self._hub_base_url}/api/channels/{channel_id}/messages",
                json=payload,
                timeout=30.0,
            )
        except Exception:
            logger.exception("Failed to forward Telegram message to hub: #%s", channel_id)

    async def _send_telegram_message_as(self, bot, text: str, topic_id: int | None = None) -> None:
        """Send a message using a specific bot instance."""
        if bot is None:
            return

        try:
            kwargs = {"chat_id": self._config.chat_id, "text": text}
            if topic_id is not None:
                kwargs["message_thread_id"] = topic_id
            await bot.send_message(**kwargs)
        except Exception:
            logger.exception("Failed to send Telegram message to topic %s", topic_id)
