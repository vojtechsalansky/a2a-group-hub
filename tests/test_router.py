"""Tests for HierarchicalRouter — Neo4j-backed message routing."""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.hub.router import HierarchicalRouter, RoutingDecision


# -- Helpers ----------------------------------------------------------------


class _AsyncIterResult:
    """Mock Neo4j result that supports async iteration."""

    def __init__(self, records):
        self._records = records

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._records:
            raise StopAsyncIteration
        return self._records.pop(0)


class _MockSession:
    """Mock Neo4j async session that dispatches queries by content."""

    def __init__(self, channel_leads, delegates):
        self._channel_leads = channel_leads
        self._delegates = delegates

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def run(self, query, *args, **kwargs):
        if "OWNS_CHANNEL" in query:
            records = [MagicMock(**{
                "__getitem__": lambda self, key, r=rec: r[key],
            }) for rec in self._channel_leads]
        elif "DELEGATES_TO" in query:
            records = [MagicMock(**{
                "__getitem__": lambda self, key, r=rec: r[key],
            }) for rec in self._delegates]
        else:
            records = []
        return _AsyncIterResult(records)


def _make_mock_driver(channel_leads: list[dict], delegates: list[dict]):
    """Build a mock Neo4j driver that returns channel leads and delegation rules."""
    driver = MagicMock()
    driver.session.return_value = _MockSession(channel_leads, delegates)
    return driver


# -- Test data ----------------------------------------------------------------

CHANNEL_LEADS = [
    {"channel_id": "dev-team", "agent_id": "apollo", "name": "Apollo"},
    {"channel_id": "testing-team", "agent_id": "sentinel", "name": "Sentinel"},
    {"channel_id": "leaders", "agent_id": "nexus", "name": "Nexus"},
]

DELEGATES = [
    {"lead_id": "apollo", "agent_id": "pixel", "domain": "frontend", "keywords": ["react", "css", "ui", "component", "frontend"]},
    {"lead_id": "apollo", "agent_id": "nova", "domain": "backend", "keywords": ["api", "database", "auth", "backend", "server"]},
    {"lead_id": "apollo", "agent_id": "swift-agent", "domain": "mobile", "keywords": ["ios", "android", "swift", "kotlin", "mobile"]},
    {"lead_id": "apollo", "agent_id": "rex", "domain": "review", "keywords": ["pr", "review", "security", "code quality"]},
    {"lead_id": "sentinel", "agent_id": "shield", "domain": "e2e", "keywords": ["e2e", "playwright", "regression", "test"]},
]


# -- Tests: initialize --------------------------------------------------------


class TestInitialize:
    async def test_loads_channel_leads_from_neo4j(self):
        driver = _make_mock_driver(CHANNEL_LEADS, DELEGATES)
        router = HierarchicalRouter(neo4j_driver=driver)
        await router.initialize()

        assert router.get_lead("dev-team") == ("apollo", "Apollo")
        assert router.get_lead("testing-team") == ("sentinel", "Sentinel")
        assert router.get_lead("leaders") == ("nexus", "Nexus")

    async def test_loads_delegation_rules(self):
        driver = _make_mock_driver(CHANNEL_LEADS, DELEGATES)
        router = HierarchicalRouter(neo4j_driver=driver)
        await router.initialize()

        assert router.is_ready
        assert len(router._delegates_cache["apollo"]) == 4
        assert len(router._delegates_cache["sentinel"]) == 1

    async def test_no_driver_stays_empty(self):
        router = HierarchicalRouter(neo4j_driver=None)
        await router.initialize()

        assert not router.is_ready
        assert router.get_lead("dev-team") is None

    async def test_driver_error_clears_cache(self):
        driver = AsyncMock()
        session = AsyncMock()
        session.run = AsyncMock(side_effect=Exception("connection refused"))
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        driver.session.return_value = session

        router = HierarchicalRouter(neo4j_driver=driver)
        await router.initialize()

        assert not router.is_ready


# -- Tests: get_lead ----------------------------------------------------------


class TestGetLead:
    async def test_returns_channel_owner(self):
        driver = _make_mock_driver(CHANNEL_LEADS, DELEGATES)
        router = HierarchicalRouter(neo4j_driver=driver)
        await router.initialize()

        lead = router.get_lead("dev-team")
        assert lead == ("apollo", "Apollo")

    async def test_returns_none_for_unknown_channel(self):
        driver = _make_mock_driver(CHANNEL_LEADS, DELEGATES)
        router = HierarchicalRouter(neo4j_driver=driver)
        await router.initialize()

        assert router.get_lead("nonexistent-channel") is None


# -- Tests: parse_mentions ----------------------------------------------------


class TestParseMentions:
    def test_extracts_single_mention(self):
        router = HierarchicalRouter()
        assert router.parse_mentions("@pixel please fix the CSS") == ["pixel"]

    def test_extracts_multiple_mentions(self):
        router = HierarchicalRouter()
        result = router.parse_mentions("@pixel @nova prosim opravte")
        assert result == ["pixel", "nova"]

    def test_empty_on_no_mentions(self):
        router = HierarchicalRouter()
        assert router.parse_mentions("No mentions here") == []

    def test_case_insensitive(self):
        router = HierarchicalRouter()
        result = router.parse_mentions("@Pixel @NOVA please help")
        assert result == ["pixel", "nova"]

    def test_mention_in_middle_of_text(self):
        router = HierarchicalRouter()
        result = router.parse_mentions("Deleguju to na @pixel, at to opravi")
        assert result == ["pixel"]


# -- Tests: get_keyword_delegates ----------------------------------------------


class TestGetKeywordDelegates:
    async def test_matches_frontend_keywords(self):
        driver = _make_mock_driver(CHANNEL_LEADS, DELEGATES)
        router = HierarchicalRouter(neo4j_driver=driver)
        await router.initialize()

        result = router.get_keyword_delegates("apollo", "fix the CSS on homepage")
        assert result == ["pixel"]

    async def test_matches_multiple_delegates(self):
        driver = _make_mock_driver(CHANNEL_LEADS, DELEGATES)
        router = HierarchicalRouter(neo4j_driver=driver)
        await router.initialize()

        result = router.get_keyword_delegates("apollo", "review the frontend component PR")
        assert "pixel" in result
        assert "rex" in result

    async def test_no_match_returns_empty(self):
        driver = _make_mock_driver(CHANNEL_LEADS, DELEGATES)
        router = HierarchicalRouter(neo4j_driver=driver)
        await router.initialize()

        result = router.get_keyword_delegates("apollo", "general question about nothing specific")
        assert result == []

    async def test_unknown_lead_returns_empty(self):
        driver = _make_mock_driver(CHANNEL_LEADS, DELEGATES)
        router = HierarchicalRouter(neo4j_driver=driver)
        await router.initialize()

        result = router.get_keyword_delegates("unknown-lead", "fix the CSS")
        assert result == []

    async def test_case_insensitive_keywords(self):
        driver = _make_mock_driver(CHANNEL_LEADS, DELEGATES)
        router = HierarchicalRouter(neo4j_driver=driver)
        await router.initialize()

        result = router.get_keyword_delegates("apollo", "Fix the REACT component")
        assert result == ["pixel"]


# -- Tests: route --------------------------------------------------------------


class TestRoute:
    async def test_returns_lead_only_strategy(self):
        driver = _make_mock_driver(CHANNEL_LEADS, DELEGATES)
        router = HierarchicalRouter(neo4j_driver=driver)
        await router.initialize()

        decision = await router.route("dev-team", "Hello team")
        assert isinstance(decision, RoutingDecision)
        assert decision.lead_id == "apollo"
        assert decision.lead_name == "Apollo"
        assert decision.strategy == "lead_only"

    async def test_includes_keyword_delegates(self):
        driver = _make_mock_driver(CHANNEL_LEADS, DELEGATES)
        router = HierarchicalRouter(neo4j_driver=driver)
        await router.initialize()

        decision = await router.route("dev-team", "fix the CSS on the homepage")
        assert decision.lead_id == "apollo"
        assert "pixel" in decision.delegates
        assert decision.strategy == "lead_only"

    async def test_broadcast_fallback_for_unknown_channel(self):
        driver = _make_mock_driver(CHANNEL_LEADS, DELEGATES)
        router = HierarchicalRouter(neo4j_driver=driver)
        await router.initialize()

        decision = await router.route("nonexistent", "hello")
        assert decision.strategy == "broadcast_fallback"
        assert decision.lead_id == ""
        assert decision.delegates == []

    async def test_broadcast_fallback_without_neo4j(self):
        router = HierarchicalRouter(neo4j_driver=None)
        await router.initialize()

        decision = await router.route("dev-team", "hello")
        assert decision.strategy == "broadcast_fallback"


# -- Tests: Channel label fix (01-01) ----------------------------------------


class TestChannelLabelFix:
    """Verify the router queries use :Channel label and correct property names
    matching the Neo4j seed data (not :HubChannel)."""

    def test_query_uses_channel_label_not_hubchannel(self):
        """The Cypher query in _load_channel_leads must use :Channel, not :HubChannel."""
        source = inspect.getsource(HierarchicalRouter._load_channel_leads)
        assert ":Channel" in source, "Expected :Channel label in _load_channel_leads query"
        assert ":HubChannel" not in source, (
            "Found :HubChannel in _load_channel_leads — should be :Channel to match seed data"
        )

    def test_channel_id_property_matches_seed_data(self):
        """The Cypher query must return ch.channel_id (new seed data property)."""
        source = inspect.getsource(HierarchicalRouter._load_channel_leads)
        assert "ch.channel_id" in source, (
            "Expected ch.channel_id in _load_channel_leads query to match seed data property"
        )

    def test_agent_id_property_matches_seed_data(self):
        """The Cypher query must return a.agent_id (new seed data property)."""
        source = inspect.getsource(HierarchicalRouter._load_channel_leads)
        assert "a.agent_id" in source, (
            "Expected a.agent_id in _load_channel_leads query to match seed data property"
        )
