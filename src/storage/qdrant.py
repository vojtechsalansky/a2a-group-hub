# src/storage/qdrant.py
"""Qdrant storage backend — message history with semantic search via Voyage AI."""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone

from src.channels.models import Channel, ChannelMember
from src.storage.base import StorageBackend, StoredMessage, Webhook

logger = logging.getLogger("a2a-hub.storage.qdrant")

try:
    from qdrant_client import QdrantClient, models
    _HAS_QDRANT = True
except ImportError:
    _HAS_QDRANT = False

try:
    import voyageai
    _HAS_VOYAGE = True
except ImportError:
    _HAS_VOYAGE = False


class QdrantBackend(StorageBackend):
    """Qdrant backend for message storage and semantic search.

    Handles ONLY messages. Channels, members, webhooks must be handled
    by another backend (Neo4j) via CompositeBackend.
    """

    COLLECTION = "hub_messages"
    EMBEDDING_MODEL = "voyage-3"
    VECTOR_SIZE = 1024

    def __init__(self, url: str = "http://localhost:6333", collection: str | None = None):
        if not _HAS_QDRANT:
            raise ImportError("qdrant-client not installed. pip install qdrant-client voyageai")
        self._url = url
        self._collection = collection or self.COLLECTION
        self._client = QdrantClient(url=url, timeout=5)
        self._voyage = None
        if _HAS_VOYAGE:
            api_key = os.environ.get("VOYAGE_API_KEY", "")
            if api_key:
                self._voyage = voyageai.Client(api_key=api_key)
            else:
                logger.warning("VOYAGE_API_KEY not set — embeddings disabled, semantic search unavailable")
        else:
            logger.warning("voyageai not installed — embeddings disabled")
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        """Create collection if it doesn't exist."""
        collections = [c.name for c in self._client.get_collections().collections]
        if self._collection not in collections:
            self._client.create_collection(
                collection_name=self._collection,
                vectors_config=models.VectorParams(
                    size=self.VECTOR_SIZE,
                    distance=models.Distance.COSINE,
                ),
            )
            logger.info(f"Created Qdrant collection: {self._collection}")

    def _embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts using Voyage AI. Returns empty list on failure."""
        if not texts:
            return []
        if not self._voyage:
            logger.warning("Voyage client not available — skipping embedding")
            return []
        try:
            result = self._voyage.embed(texts, model=self.EMBEDDING_MODEL)
            return result.embeddings
        except Exception:
            logger.exception("Voyage AI embedding failed")
            return []

    # -- Messages --

    async def save_message(self, channel_id: str, message: StoredMessage) -> None:
        vectors = self._embed([message.text])
        if not vectors:
            logger.warning("No embedding produced — skipping save for message %s", message.message_id)
            return
        vector = vectors[0]
        payload = {
            "message_id": message.message_id,
            "channel_id": message.channel_id or channel_id,
            "sender_id": message.sender_id,
            "text": message.text,
            "context_id": message.context_id,
            "timestamp": message.timestamp.isoformat() if isinstance(message.timestamp, datetime) else message.timestamp,
            "metadata": message.metadata,
            "project_id": message.project_id,
            "tags": message.tags,
            "reply_to_message_id": message.reply_to_message_id,
        }
        self._client.upsert(
            collection_name=self._collection,
            points=[models.PointStruct(
                id=str(uuid.uuid4()),
                vector=vector,
                payload=payload,
            )],
        )

    async def get_messages(self, channel_id: str, limit: int = 50, offset: int = 0) -> list[StoredMessage]:
        results, _ = self._client.scroll(
            collection_name=self._collection,
            scroll_filter=models.Filter(
                must=[models.FieldCondition(
                    key="channel_id",
                    match=models.MatchValue(value=channel_id),
                )],
            ),
            limit=limit,
            offset=offset,
            order_by=models.OrderBy(key="timestamp", direction=models.Direction.ASC),
        )
        return [self._point_to_message(p) for p in results]

    async def search_messages(self, channel_id: str, query: str, limit: int = 10) -> list[StoredMessage]:
        vectors = self._embed([query])
        if not vectors:
            return []
        vector = vectors[0]
        results = self._client.query_points(
            collection_name=self._collection,
            query=vector,
            query_filter=models.Filter(
                must=[models.FieldCondition(
                    key="channel_id",
                    match=models.MatchValue(value=channel_id),
                )],
            ),
            limit=limit,
        )
        return [self._point_to_message(p) for p in results.points]

    async def search_all_messages(self, query: str, limit: int = 10) -> list[StoredMessage]:
        """Search across ALL channels — for knowledge agents."""
        vectors = self._embed([query])
        if not vectors:
            return []
        vector = vectors[0]
        results = self._client.query_points(
            collection_name=self._collection,
            query=vector,
            limit=limit,
        )
        return [self._point_to_message(p) for p in results.points]

    def _point_to_message(self, point) -> StoredMessage:
        p = point.payload
        raw_ts = p.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(raw_ts) if raw_ts else datetime.now(timezone.utc)
            timestamp = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            timestamp = datetime.now(timezone.utc)
        return StoredMessage(
            message_id=p["message_id"],
            channel_id=p["channel_id"],
            sender_id=p["sender_id"],
            text=p["text"],
            context_id=p["context_id"],
            timestamp=timestamp,
            metadata=p.get("metadata", {}),
            project_id=p.get("project_id"),
            tags=p.get("tags", []),
            reply_to_message_id=p.get("reply_to_message_id"),
        )

    async def close(self) -> None:
        """Close Qdrant client. qdrant-client handles cleanup internally."""
        pass

    # -- Channel/Member/Webhook stubs (handled by Neo4j in composite) --

    async def save_channel(self, channel: Channel) -> None:
        raise NotImplementedError("Use CompositeBackend")

    async def get_channel(self, channel_id: str) -> Channel | None:
        raise NotImplementedError("Use CompositeBackend")

    async def list_channels(self) -> list[Channel]:
        raise NotImplementedError("Use CompositeBackend")

    async def delete_channel(self, channel_id: str) -> bool:
        raise NotImplementedError("Use CompositeBackend")

    async def save_member(self, channel_id: str, member: ChannelMember) -> None:
        raise NotImplementedError("Use CompositeBackend")

    async def remove_member(self, channel_id: str, agent_id: str) -> bool:
        raise NotImplementedError("Use CompositeBackend")

    async def save_webhook(self, channel_id: str, webhook: Webhook) -> None:
        raise NotImplementedError("Use CompositeBackend")

    async def list_webhooks(self, channel_id: str) -> list[Webhook]:
        raise NotImplementedError("Use CompositeBackend")

    async def delete_webhook(self, channel_id: str, webhook_id: str) -> bool:
        raise NotImplementedError("Use CompositeBackend")
