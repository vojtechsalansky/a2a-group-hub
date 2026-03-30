# src/storage/sqlite_backend.py
"""SQLite storage backend — persistent conversations across hub restarts."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from src.channels.models import Channel, ChannelMember, MemberRole
from src.storage.base import StorageBackend, StoredMessage, Webhook

logger = logging.getLogger("a2a-hub.storage.sqlite")

DEFAULT_DB_PATH = "data/a2a-hub.db"


class SqliteBackend(StorageBackend):
    """SQLite-backed persistence for channels, messages, and webhooks."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._create_tables()
        logger.info("SQLite storage initialized at %s", db_path)

    def _create_tables(self) -> None:
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS channels (
                channel_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                context_id TEXT NOT NULL,
                default_aggregation TEXT DEFAULT 'all',
                agent_timeout INTEGER DEFAULT 120,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS members (
                channel_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                name TEXT NOT NULL,
                url TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT 'member',
                joined_at TEXT NOT NULL,
                PRIMARY KEY (channel_id, agent_id),
                FOREIGN KEY (channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS messages (
                message_id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                text TEXT NOT NULL,
                context_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                metadata TEXT DEFAULT '{}',
                project_id TEXT,
                tags TEXT DEFAULT '[]',
                reply_to_message_id TEXT,
                FOREIGN KEY (channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id, timestamp);
            CREATE TABLE IF NOT EXISTS webhooks (
                webhook_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                url TEXT NOT NULL,
                events TEXT DEFAULT '["message"]',
                PRIMARY KEY (webhook_id, channel_id),
                FOREIGN KEY (channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE
            );
        """)

    def _channel_from_row(self, row: sqlite3.Row) -> Channel:
        ch = Channel(
            channel_id=row["channel_id"],
            name=row["name"],
            context_id=row["context_id"],
            default_aggregation=row["default_aggregation"],
            agent_timeout=row["agent_timeout"],
        )
        ch.created_at = datetime.fromisoformat(row["created_at"])
        # Load members
        cur = self.db.execute(
            "SELECT * FROM members WHERE channel_id = ?", (ch.channel_id,)
        )
        cur.row_factory = sqlite3.Row
        for mrow in cur.fetchall():
            member = ChannelMember(
                agent_id=mrow["agent_id"],
                name=mrow["name"],
                url=mrow["url"],
                role=MemberRole(mrow["role"]),
            )
            member.joined_at = datetime.fromisoformat(mrow["joined_at"])
            ch.members[member.agent_id] = member
        return ch

    # --- Channels ---

    async def save_channel(self, channel: Channel) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO channels (channel_id, name, context_id, default_aggregation, agent_timeout, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (channel.channel_id, channel.name, channel.context_id,
             channel.default_aggregation, channel.agent_timeout,
             channel.created_at.isoformat()),
        )
        for member in channel.members.values():
            self.db.execute(
                "INSERT OR REPLACE INTO members (channel_id, agent_id, name, url, role, joined_at) VALUES (?, ?, ?, ?, ?, ?)",
                (channel.channel_id, member.agent_id, member.name, member.url,
                 member.role.value, member.joined_at.isoformat()),
            )
        self.db.commit()

    async def get_channel(self, channel_id: str) -> Channel | None:
        cur = self.db.execute("SELECT * FROM channels WHERE channel_id = ?", (channel_id,))
        cur.row_factory = sqlite3.Row
        row = cur.fetchone()
        return self._channel_from_row(row) if row else None

    async def list_channels(self) -> list[Channel]:
        cur = self.db.execute("SELECT * FROM channels ORDER BY created_at")
        cur.row_factory = sqlite3.Row
        return [self._channel_from_row(r) for r in cur.fetchall()]

    async def delete_channel(self, channel_id: str) -> bool:
        cur = self.db.execute("DELETE FROM channels WHERE channel_id = ?", (channel_id,))
        self.db.commit()
        return cur.rowcount > 0

    # --- Members ---

    async def save_member(self, channel_id: str, member: ChannelMember) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO members (channel_id, agent_id, name, url, role, joined_at) VALUES (?, ?, ?, ?, ?, ?)",
            (channel_id, member.agent_id, member.name, member.url,
             member.role.value, member.joined_at.isoformat()),
        )
        self.db.commit()

    async def remove_member(self, channel_id: str, agent_id: str) -> bool:
        cur = self.db.execute(
            "DELETE FROM members WHERE channel_id = ? AND agent_id = ?",
            (channel_id, agent_id),
        )
        self.db.commit()
        return cur.rowcount > 0

    # --- Messages ---

    async def save_message(self, channel_id: str, message: StoredMessage) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO messages (message_id, channel_id, sender_id, text, context_id, timestamp, metadata, project_id, tags, reply_to_message_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (message.message_id, channel_id, message.sender_id, message.text,
             message.context_id, message.timestamp.isoformat(),
             json.dumps(message.metadata), message.project_id,
             json.dumps(message.tags), message.reply_to_message_id),
        )
        self.db.commit()

    async def get_messages(self, channel_id: str, limit: int = 50, offset: int = 0) -> list[StoredMessage]:
        cur = self.db.execute(
            "SELECT * FROM messages WHERE channel_id = ? ORDER BY timestamp ASC LIMIT ? OFFSET ?",
            (channel_id, limit, offset),
        )
        cur.row_factory = sqlite3.Row
        return [
            StoredMessage(
                message_id=r["message_id"],
                channel_id=r["channel_id"],
                sender_id=r["sender_id"],
                text=r["text"],
                context_id=r["context_id"],
                timestamp=datetime.fromisoformat(r["timestamp"]),
                metadata=json.loads(r["metadata"] or "{}"),
                project_id=r["project_id"],
                tags=json.loads(r["tags"] or "[]"),
                reply_to_message_id=r["reply_to_message_id"],
            )
            for r in cur.fetchall()
        ]

    async def search_messages(self, channel_id: str, query: str, limit: int = 10) -> list[StoredMessage]:
        cur = self.db.execute(
            "SELECT * FROM messages WHERE channel_id = ? AND text LIKE ? ORDER BY timestamp DESC LIMIT ?",
            (channel_id, f"%{query}%", limit),
        )
        cur.row_factory = sqlite3.Row
        return [
            StoredMessage(
                message_id=r["message_id"], channel_id=r["channel_id"],
                sender_id=r["sender_id"], text=r["text"],
                context_id=r["context_id"],
                timestamp=datetime.fromisoformat(r["timestamp"]),
                metadata=json.loads(r["metadata"] or "{}"),
            )
            for r in cur.fetchall()
        ]

    async def search_all_messages(self, query: str, limit: int = 10) -> list[StoredMessage]:
        cur = self.db.execute(
            "SELECT * FROM messages WHERE text LIKE ? ORDER BY timestamp DESC LIMIT ?",
            (f"%{query}%", limit),
        )
        cur.row_factory = sqlite3.Row
        return [
            StoredMessage(
                message_id=r["message_id"], channel_id=r["channel_id"],
                sender_id=r["sender_id"], text=r["text"],
                context_id=r["context_id"],
                timestamp=datetime.fromisoformat(r["timestamp"]),
                metadata=json.loads(r["metadata"] or "{}"),
            )
            for r in cur.fetchall()
        ]

    # --- Webhooks ---

    async def save_webhook(self, channel_id: str, webhook: Webhook) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO webhooks (webhook_id, channel_id, url, events) VALUES (?, ?, ?, ?)",
            (webhook.webhook_id, channel_id, webhook.url, json.dumps(webhook.events)),
        )
        self.db.commit()

    async def list_webhooks(self, channel_id: str) -> list[Webhook]:
        cur = self.db.execute(
            "SELECT * FROM webhooks WHERE channel_id = ?", (channel_id,)
        )
        cur.row_factory = sqlite3.Row
        return [
            Webhook(webhook_id=r["webhook_id"], url=r["url"], events=json.loads(r["events"] or '["message"]'))
            for r in cur.fetchall()
        ]

    async def delete_webhook(self, channel_id: str, webhook_id: str) -> bool:
        cur = self.db.execute(
            "DELETE FROM webhooks WHERE channel_id = ? AND webhook_id = ?",
            (channel_id, webhook_id),
        )
        self.db.commit()
        return cur.rowcount > 0
