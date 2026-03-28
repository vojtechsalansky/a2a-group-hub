# src/telegram/config.py
"""Configuration for Telegram bridge."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


# Default OpenClaw topic mapping (discovered 2026-03-28)
DEFAULT_CHANNEL_TOPIC_MAP = {
    "leaders": 3,
    "dev-team": 4,
    "testing-team": 5,
    "ops-team": 6,
    "pm-team": 7,
    "knowledge-team": 8,
}


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

        # Load topic map from env or use defaults
        topic_map = dict(DEFAULT_CHANNEL_TOPIC_MAP)
        # Allow overrides: TG_TOPIC_LEADERS=3, TG_TOPIC_DEV_TEAM=4, etc.
        for channel_id, default_topic in DEFAULT_CHANNEL_TOPIC_MAP.items():
            env_key = "TG_TOPIC_" + channel_id.upper().replace("-", "_")
            val = os.environ.get(env_key)
            if val:
                topic_map[channel_id] = int(val)

        return cls(
            enabled=enabled,
            bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            chat_id=int(os.environ.get("TELEGRAM_CHAT_ID", os.environ.get("TG_SUPERGROUP_ID", "0"))),
            channel_topic_map=topic_map,
        )
