# src/storage/qdrant.py
"""Qdrant storage backend — message history with semantic search via FastEmbed."""

from __future__ import annotations

import logging
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
    from fastembed import TextEmbedding
    _HAS_FASTEMBED = True
except ImportError:
    _HAS_FASTEMBED = False


class QdrantBackend(StorageBackend):
    """Qdrant backend for message storage and semantic search.

    Handles ONLY messages. Channels, members, webhooks must be handled
    by another backend (Neo4j) via CompositeBackend.
    """

    COLLECTION = "hub_messages"
    EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
    VECTOR_SIZE = 384

    def __init__(self, url: str = "http://localhost:6333", collection: str | None = None):
        if not _HAS_QDRANT:
            raise ImportError("qdrant-client not installed. pip install qdrant-client fastembed")
        self._url = url
        self._collection = collection or self.COLLECTION
        self._client = QdrantClient(url=url, prefer_grpc=False)
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

    def _embed(self, text: str) -> list[float]:
        """Generate embedding using FastEmbed (local, no API key)."""
        if not hasattr(self, "_embedder"):
            if not _HAS_FASTEMBED:
                raise ImportError("fastembed not installed. pip install fastembed")
            self._embedder = TextEmbedding(model_name=self.EMBEDDING_MODEL)
        embeddings = list(self._embedder.embed([text]))
        return embeddings[0].tolist()

    # -- Messages --

    async def save_message(self, channel_id: str, message: StoredMessage) -> None:
        vector = self._embed(message.text)
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
        vector = self._embed(query)
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
        vector = self._embed(query)
        results = self._client.query_points(
            collection_name=self._collection,
            query=vector,
            limit=limit,
        )
        return [self._point_to_message(p) for p in results.points]

    def _point_to_message(self, point) -> StoredMessage:
        p = point.payload
        return StoredMessage(
            message_id=p["message_id"],
            channel_id=p["channel_id"],
            sender_id=p["sender_id"],
            text=p["text"],
            context_id=p["context_id"],
            timestamp=p.get("timestamp", ""),
            metadata=p.get("metadata", {}),
            project_id=p.get("project_id"),
            tags=p.get("tags", []),
            reply_to_message_id=p.get("reply_to_message_id"),
        )

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
