# src/telegram/config.py
"""Configuration for Telegram bridge."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class TelegramConfig:
    """Telegram bridge configuration, typically loaded from environment."""

    enabled: bool = False
    bot_token: str = ""
    chat_id: int = 0
    channel_topic_map: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> TelegramConfig:
        """Load config from environment variables."""
        enabled = os.environ.get("TELEGRAM_ENABLED", "false").lower() in ("true", "1", "yes")
        return cls(
            enabled=enabled,
            bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            chat_id=int(os.environ.get("TELEGRAM_CHAT_ID", "0")),
        )
