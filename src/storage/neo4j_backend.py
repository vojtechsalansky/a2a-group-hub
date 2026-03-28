# src/storage/neo4j_backend.py
"""Neo4j storage backend — channels, members, webhooks as graph.

Reuses existing :Agent nodes from the OpenClaw knowledge graph.
New labels: :HubChannel, :HubWebhook
Relationships: (:HubChannel)-[:HAS_MEMBER]->(:Agent), (:HubChannel)-[:HAS_WEBHOOK]->(:HubWebhook)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from neo4j import AsyncGraphDatabase

from src.channels.models import Channel, ChannelMember, MemberRole
from src.storage.base import StorageBackend, StoredMessage, Webhook

logger = logging.getLogger("a2a-hub.storage.neo4j")


class Neo4jBackend(StorageBackend):
    """Neo4j backend for channel structure, membership, and webhooks.

    Handles channels, members, webhooks. Message operations raise
    NotImplementedError — those are handled by Qdrant via CompositeBackend.
    """

    def __init__(self, url: str, user: str, password: str) -> None:
        self._driver = AsyncGraphDatabase.driver(url, auth=(user, password))

    async def close(self) -> None:
        await self._driver.close()

    # ── Channels ──────────────────────────────────────────────

    async def save_channel(self, channel: Channel) -> None:
        query = """
        MERGE (c:HubChannel {channel_id: $channel_id})
        SET c.name = $name,
            c.context_id = $context_id,
            c.default_aggregation = $default_aggregation,
            c.agent_timeout = $agent_timeout,
            c.message_count = $message_count
        """
        async with self._driver.session() as session:
            await session.run(query, {
                "channel_id": channel.channel_id,
                "name": channel.name,
                "context_id": channel.context_id,
                "default_aggregation": channel.default_aggregation,
                "agent_timeout": channel.agent_timeout,
                "message_count": channel.message_count,
            })

    async def get_channel(self, channel_id: str) -> Channel | None:
        query = """
        MATCH (c:HubChannel {channel_id: $channel_id})
        OPTIONAL MATCH (c)-[r:HAS_MEMBER]->(a:Agent)
        RETURN c, collect(
            CASE WHEN a IS NOT NULL
                THEN {agent: properties(a), rel: properties(r)}
                ELSE NULL
            END
        ) AS members
        """
        async with self._driver.session() as session:
            result = await session.run(query, {"channel_id": channel_id})
            record = await result.single()

        if record is None:
            return None

        c_data = record["c"]
        channel = Channel(
            channel_id=c_data["channel_id"],
            name=c_data["name"],
            context_id=c_data.get("context_id", ""),
            default_aggregation=c_data.get("default_aggregation", "all"),
            agent_timeout=c_data.get("agent_timeout", 60),
            message_count=c_data.get("message_count", 0),
        )

        for member_data in record["members"]:
            if member_data is None:
                continue
            agent = member_data["agent"]
            rel = member_data["rel"]
            member = ChannelMember(
                agent_id=agent["agent_id"],
                name=agent.get("name", agent["agent_id"]),
                url=agent.get("url", ""),
                role=MemberRole(rel.get("role", "member")),
            )
            channel.add_member(member)

        return channel

    async def list_channels(self) -> list[Channel]:
        # Must load members too — handler uses list_channels for context_id resolution
        channel_ids: list[str] = []
        query = "MATCH (c:HubChannel) RETURN c.channel_id AS channel_id"
        async with self._driver.session() as session:
            result = await session.run(query)
            async for record in result:
                channel_ids.append(record["channel_id"])
        # Use get_channel which properly loads members via HAS_MEMBER join
        channels: list[Channel] = []
        for cid in channel_ids:
            ch = await self.get_channel(cid)
            if ch:
                channels.append(ch)
        return channels

    async def delete_channel(self, channel_id: str) -> bool:
        query = """
        MATCH (c:HubChannel {channel_id: $channel_id})
        DETACH DELETE c
        """
        async with self._driver.session() as session:
            result = await session.run(query, {"channel_id": channel_id})
            summary = await result.consume()
        return summary.counters.nodes_deleted > 0

    # ── Members ───────────────────────────────────────────────

    async def save_member(self, channel_id: str, member: ChannelMember) -> None:
        query = """
        MATCH (c:HubChannel {channel_id: $channel_id})
        MERGE (a:Agent {name: $name})
        ON CREATE SET a.agent_id = $agent_id, a.url = $url
        ON MATCH SET a.agent_id = $agent_id, a.url = $url
        MERGE (c)-[r:HAS_MEMBER]->(a)
        SET r.role = $role,
            r.joined_at = datetime(),
            r.messages_sent = 0,
            r.last_active = datetime()
        """
        async with self._driver.session() as session:
            await session.run(query, {
                "channel_id": channel_id,
                "agent_id": member.agent_id,
                "name": member.name,
                "url": member.url,
                "role": member.role.value,
            })

    async def remove_member(self, channel_id: str, agent_id: str) -> bool:
        query = """
        MATCH (c:HubChannel {channel_id: $channel_id})-[r:HAS_MEMBER]->(a:Agent {agent_id: $agent_id})
        DELETE r
        """
        async with self._driver.session() as session:
            result = await session.run(query, {
                "channel_id": channel_id,
                "agent_id": agent_id,
            })
            summary = await result.consume()
        return summary.counters.relationships_deleted > 0

    async def update_member_activity(self, channel_id: str, agent_id: str) -> None:
        """Increment messages_sent and update last_active for a member."""
        query = """
        MATCH (c:HubChannel {channel_id: $channel_id})-[r:HAS_MEMBER]->(a:Agent {agent_id: $agent_id})
        SET r.messages_sent = r.messages_sent + 1,
            r.last_active = datetime()
        """
        async with self._driver.session() as session:
            await session.run(query, {
                "channel_id": channel_id,
                "agent_id": agent_id,
            })

    # ── Messages (handled by Qdrant in CompositeBackend) ──────

    async def save_message(self, channel_id: str, message: StoredMessage) -> None:
        raise NotImplementedError("Use CompositeBackend — messages handled by Qdrant")

    async def get_messages(self, channel_id: str, limit: int = 50, offset: int = 0) -> list[StoredMessage]:
        raise NotImplementedError("Use CompositeBackend — messages handled by Qdrant")

    async def search_messages(self, channel_id: str, query: str, limit: int = 10) -> list[StoredMessage]:
        raise NotImplementedError("Use CompositeBackend — messages handled by Qdrant")

    async def search_all_messages(self, query: str, limit: int = 10) -> list[StoredMessage]:
        raise NotImplementedError("Use CompositeBackend — messages handled by Qdrant")

    # ── Webhooks ──────────────────────────────────────────────

    async def save_webhook(self, channel_id: str, webhook: Webhook) -> None:
        query = """
        MATCH (c:HubChannel {channel_id: $channel_id})
        CREATE (c)-[:HAS_WEBHOOK]->(w:HubWebhook {
            webhook_id: $webhook_id,
            url: $url,
            events: $events
        })
        """
        async with self._driver.session() as session:
            await session.run(query, {
                "channel_id": channel_id,
                "webhook_id": webhook.webhook_id,
                "url": webhook.url,
                "events": webhook.events,
            })

    async def list_webhooks(self, channel_id: str) -> list[Webhook]:
        query = """
        MATCH (c:HubChannel {channel_id: $channel_id})-[:HAS_WEBHOOK]->(w:HubWebhook)
        RETURN w
        """
        webhooks: list[Webhook] = []
        async with self._driver.session() as session:
            result = await session.run(query, {"channel_id": channel_id})
            async for record in result:
                w_data = record["w"]
                webhooks.append(Webhook(
                    webhook_id=w_data["webhook_id"],
                    url=w_data["url"],
                    events=w_data.get("events", ["message"]),
                ))
        return webhooks

    async def delete_webhook(self, channel_id: str, webhook_id: str) -> bool:
        query = """
        MATCH (c:HubChannel {channel_id: $channel_id})-[:HAS_WEBHOOK]->(w:HubWebhook {webhook_id: $webhook_id})
        DETACH DELETE w
        """
        async with self._driver.session() as session:
            result = await session.run(query, {
                "channel_id": channel_id,
                "webhook_id": webhook_id,
            })
            summary = await result.consume()
        return summary.counters.nodes_deleted > 0
