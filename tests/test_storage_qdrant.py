# tests/test_storage_qdrant.py
"""Tests for QdrantBackend — uses mocked QdrantClient (no real Qdrant needed)."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.storage.qdrant import QdrantBackend
from src.storage.base import StoredMessage


@pytest.fixture
def mock_qdrant_client():
    """Mock qdrant_client to avoid needing a real Qdrant instance."""
    with patch("src.storage.qdrant.QdrantClient") as MockClient:
        client = MagicMock()
        # _ensure_collection needs get_collections to return empty list
        client.get_collections.return_value = MagicMock(collections=[])
        MockClient.return_value = client
        yield client


@pytest.fixture
def backend(mock_qdrant_client):
    with patch("src.storage.qdrant.TextEmbedding") as MockEmbed:
        import numpy as np
        embedder = MagicMock()
        embedder.embed.return_value = iter([np.zeros(384)])
        MockEmbed.return_value = embedder
        b = QdrantBackend(url="http://localhost:6333", collection="test_hub_messages")
        b._embedder = embedder
        return b


class TestQdrantMessageStorage:
    async def test_save_message(self, backend, mock_qdrant_client):
        msg = StoredMessage(
            message_id="msg-1", channel_id="dev", sender_id="apollo",
            text="Hello team", context_id="ctx-1",
        )
        await backend.save_message("dev", msg)
        mock_qdrant_client.upsert.assert_called_once()

    async def test_get_messages_by_channel(self, backend, mock_qdrant_client):
        mock_qdrant_client.scroll.return_value = ([], None)
        messages = await backend.get_messages("dev", limit=10)
        assert isinstance(messages, list)
        assert len(messages) == 0

    async def test_search_messages_semantic(self, backend, mock_qdrant_client):
        mock_qdrant_client.query_points.return_value = MagicMock(points=[])
        results = await backend.search_messages("dev", query="authentication")
        assert isinstance(results, list)
        assert len(results) == 0

    async def test_search_all_messages(self, backend, mock_qdrant_client):
        mock_qdrant_client.query_points.return_value = MagicMock(points=[])
        results = await backend.search_all_messages(query="how did we solve auth?")
        assert isinstance(results, list)
        assert len(results) == 0

    async def test_save_message_with_project_and_tags(self, backend, mock_qdrant_client):
        msg = StoredMessage(
            message_id="msg-2", channel_id="dev", sender_id="rex",
            text="Use FastAPI", context_id="ctx-1",
            project_id="openclaw", tags=["architecture", "api"],
        )
        await backend.save_message("dev", msg)
        call_args = mock_qdrant_client.upsert.call_args
        points = call_args.kwargs.get("points") or call_args[1].get("points")
        point = points[0]
        assert point.payload["project_id"] == "openclaw"
        assert point.payload["tags"] == ["architecture", "api"]

    async def test_save_message_with_reply_to(self, backend, mock_qdrant_client):
        msg = StoredMessage(
            message_id="msg-3", channel_id="dev", sender_id="nova",
            text="I agree with Rex", context_id="ctx-1",
            reply_to_message_id="msg-2",
        )
        await backend.save_message("dev", msg)
        call_args = mock_qdrant_client.upsert.call_args
        points = call_args.kwargs.get("points") or call_args[1].get("points")
        point = points[0]
        assert point.payload["reply_to_message_id"] == "msg-2"

    async def test_get_messages_returns_stored_messages(self, backend, mock_qdrant_client):
        """Verify scroll results are converted to StoredMessage objects."""
        mock_point = MagicMock()
        mock_point.payload = {
            "message_id": "msg-1",
            "channel_id": "dev",
            "sender_id": "apollo",
            "text": "Hello team",
            "context_id": "ctx-1",
            "timestamp": "2026-03-28T10:00:00+00:00",
            "metadata": {},
            "project_id": None,
            "tags": [],
            "reply_to_message_id": None,
        }
        mock_qdrant_client.scroll.return_value = ([mock_point], None)
        messages = await backend.get_messages("dev", limit=10)
        assert len(messages) == 1
        assert isinstance(messages[0], StoredMessage)
        assert messages[0].message_id == "msg-1"
        assert messages[0].text == "Hello team"

    async def test_search_messages_returns_stored_messages(self, backend, mock_qdrant_client):
        """Verify query_points results are converted to StoredMessage objects."""
        mock_point = MagicMock()
        mock_point.payload = {
            "message_id": "msg-1",
            "channel_id": "dev",
            "sender_id": "apollo",
            "text": "Authentication fix",
            "context_id": "ctx-1",
            "timestamp": "2026-03-28T10:00:00+00:00",
            "metadata": {},
            "project_id": "openclaw",
            "tags": ["auth"],
            "reply_to_message_id": None,
        }
        mock_qdrant_client.query_points.return_value = MagicMock(points=[mock_point])
        results = await backend.search_messages("dev", query="auth")
        assert len(results) == 1
        assert results[0].project_id == "openclaw"
        assert results[0].tags == ["auth"]

    async def test_channel_methods_raise_not_implemented(self, backend):
        with pytest.raises(NotImplementedError):
            await backend.save_channel(None)
        with pytest.raises(NotImplementedError):
            await backend.get_channel("dev")
        with pytest.raises(NotImplementedError):
            await backend.list_channels()
        with pytest.raises(NotImplementedError):
            await backend.delete_channel("dev")

    async def test_member_methods_raise_not_implemented(self, backend):
        with pytest.raises(NotImplementedError):
            await backend.save_member("dev", None)
        with pytest.raises(NotImplementedError):
            await backend.remove_member("dev", "agent-1")

    async def test_webhook_methods_raise_not_implemented(self, backend):
        with pytest.raises(NotImplementedError):
            await backend.save_webhook("dev", None)
        with pytest.raises(NotImplementedError):
            await backend.list_webhooks("dev")
        with pytest.raises(NotImplementedError):
            await backend.delete_webhook("dev", "wh-1")


class TestQdrantCollectionInit:
    def test_creates_collection_if_not_exists(self, mock_qdrant_client):
        """Verify _ensure_collection creates the collection when missing."""
        with patch("src.storage.qdrant.TextEmbedding") as MockEmbed:
            embedder = MagicMock()
            MockEmbed.return_value = embedder
            QdrantBackend(url="http://localhost:6333", collection="new_collection")
            mock_qdrant_client.create_collection.assert_called_once()

    def test_skips_creation_if_collection_exists(self, mock_qdrant_client):
        """Verify _ensure_collection skips creation when collection exists."""
        existing = MagicMock()
        existing.name = "existing_collection"
        mock_qdrant_client.get_collections.return_value = MagicMock(collections=[existing])
        with patch("src.storage.qdrant.TextEmbedding") as MockEmbed:
            embedder = MagicMock()
            MockEmbed.return_value = embedder
            QdrantBackend(url="http://localhost:6333", collection="existing_collection")
            mock_qdrant_client.create_collection.assert_not_called()
