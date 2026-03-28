# src/storage/base.py
"""Storage backend interface and shared data models."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.channels.models import Channel, ChannelMember


@dataclass
class StoredMessage:
    """A persisted message from a channel conversation."""
    message_id: str
    channel_id: str
    sender_id: str
    text: str
    context_id: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict = field(default_factory=dict)
    project_id: str | None = None
    tags: list[str] = field(default_factory=list)
    reply_to_message_id: str | None = None


@dataclass
class Webhook:
    """A registered webhook for channel event notifications."""
    webhook_id: str
    url: str
    events: list[str] = field(default_factory=lambda: ["message"])


class StorageBackend(ABC):
    """Abstract interface for hub persistence."""

    # Channels
    @abstractmethod
    async def save_channel(self, channel: Channel) -> None: ...

    @abstractmethod
    async def get_channel(self, channel_id: str) -> Channel | None: ...

    @abstractmethod
    async def list_channels(self) -> list[Channel]: ...

    @abstractmethod
    async def delete_channel(self, channel_id: str) -> bool: ...

    # Members
    @abstractmethod
    async def save_member(self, channel_id: str, member: ChannelMember) -> None: ...

    @abstractmethod
    async def remove_member(self, channel_id: str, agent_id: str) -> bool: ...

    # Messages
    @abstractmethod
    async def save_message(self, channel_id: str, message: StoredMessage) -> None: ...

    @abstractmethod
    async def get_messages(self, channel_id: str, limit: int = 50, offset: int = 0) -> list[StoredMessage]: ...

    @abstractmethod
    async def search_messages(self, channel_id: str, query: str, limit: int = 10) -> list[StoredMessage]: ...

    @abstractmethod
    async def search_all_messages(self, query: str, limit: int = 10) -> list[StoredMessage]: ...

    # Webhooks
    @abstractmethod
    async def save_webhook(self, channel_id: str, webhook: Webhook) -> None: ...

    @abstractmethod
    async def list_webhooks(self, channel_id: str) -> list[Webhook]: ...

    @abstractmethod
    async def delete_webhook(self, channel_id: str, webhook_id: str) -> bool: ...
