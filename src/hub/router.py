"""Hierarchical message router using Neo4j organizational graph."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger("a2a-hub.router")


@dataclass
class RoutingDecision:
    """Result of routing a message."""

    lead_id: str  # Channel owner/lead agent_id
    lead_name: str  # Display name
    delegates: list[str] = field(default_factory=list)  # Agent IDs from keyword matching
    strategy: str = "lead_only"  # "lead_only", "lead_and_delegates", "broadcast_fallback"


class HierarchicalRouter:
    """Routes messages through team hierarchy using Neo4j.

    Caches channel leads and delegation rules at startup for fast,
    zero-query-per-request routing.  Falls back to broadcast when
    Neo4j is unavailable or no lead is found for a channel.
    """

    def __init__(self, neo4j_driver=None):
        self._driver = neo4j_driver
        self._channel_leads: dict[str, tuple[str, str]] = {}  # channel_id -> (agent_id, name)
        self._delegates_cache: dict[str, list[dict]] = {}  # lead_id -> [{agent_id, domain, keywords}]

    @property
    def is_ready(self) -> bool:
        """True if the router has cached data and can make routing decisions."""
        return bool(self._channel_leads)

    async def initialize(self, max_retries: int = 5, retry_delay: float = 3.0) -> None:
        """Load routing data from Neo4j at startup with retry. Cache for fast lookup."""
        if not self._driver:
            logger.warning("No Neo4j driver — router disabled, falling back to broadcast")
            return

        for attempt in range(1, max_retries + 1):
            try:
                await self._load_channel_leads()
                await self._load_delegation_rules()
                return  # Success
            except Exception as e:
                if attempt < max_retries:
                    logger.warning("Router init attempt %d/%d failed (%s), retrying in %.0fs...", attempt, max_retries, e, retry_delay)
                    import asyncio
                    await asyncio.sleep(retry_delay)
                else:
                    logger.exception("Failed to initialize router after %d attempts — falling back to broadcast", max_retries)
                    self._channel_leads.clear()
                    self._delegates_cache.clear()

    async def _load_channel_leads(self) -> None:
        async with self._driver.session() as session:
            result = await session.run("""
                MATCH (a:Agent)-[:OWNS_CHANNEL]->(ch:HubChannel)
                RETURN ch.channel_id AS channel_id, a.agent_id AS agent_id, a.name AS name
            """)
            async for record in result:
                self._channel_leads[record["channel_id"]] = (
                    record["agent_id"],
                    record["name"],
                )

        logger.info("Router: loaded %d channel leads", len(self._channel_leads))

    async def _load_delegation_rules(self) -> None:
        async with self._driver.session() as session:
            result = await session.run("""
                MATCH (lead:Agent)-[d:DELEGATES_TO]->(agent:Agent)
                RETURN lead.agent_id AS lead_id, agent.agent_id AS agent_id,
                       d.domain AS domain, d.keywords AS keywords
            """)
            async for record in result:
                lead_id = record["lead_id"]
                if lead_id not in self._delegates_cache:
                    self._delegates_cache[lead_id] = []
                self._delegates_cache[lead_id].append({
                    "agent_id": record["agent_id"],
                    "domain": record["domain"],
                    "keywords": record["keywords"] or [],
                })

        total_rules = sum(len(v) for v in self._delegates_cache.values())
        logger.info("Router: loaded %d delegation rules", total_rules)

    def get_lead(self, channel_id: str) -> tuple[str, str] | None:
        """Get channel lead (agent_id, name). Returns None if not found."""
        return self._channel_leads.get(channel_id)

    def parse_mentions(self, text: str) -> list[str]:
        """Parse @agent_name mentions from text. Case-insensitive."""
        return re.findall(r"@([\w-]+)", text.lower())

    def get_keyword_delegates(self, lead_id: str, message_text: str) -> list[str]:
        """Find delegates whose DELEGATES_TO keywords match the message."""
        delegates = self._delegates_cache.get(lead_id, [])
        matched: list[str] = []
        text_lower = message_text.lower()
        for d in delegates:
            for kw in d["keywords"]:
                if kw.lower() in text_lower:
                    if d["agent_id"] not in matched:
                        matched.append(d["agent_id"])
                    break
        return matched

    async def route(self, channel_id: str, message_text: str) -> RoutingDecision:
        """Determine routing for a message in a channel.

        Returns a RoutingDecision with strategy:
        - "lead_only": send only to lead, who may delegate via @mentions
        - "broadcast_fallback": no lead found, caller should broadcast to all
        """
        lead = self.get_lead(channel_id)
        if not lead:
            logger.warning("No lead for channel %s — using broadcast fallback", channel_id)
            return RoutingDecision(
                lead_id="",
                lead_name="",
                delegates=[],
                strategy="broadcast_fallback",
            )

        lead_id, lead_name = lead

        # Pre-compute keyword delegates for the hub to use if lead doesn't explicitly delegate
        keyword_delegates = self.get_keyword_delegates(lead_id, message_text)

        return RoutingDecision(
            lead_id=lead_id,
            lead_name=lead_name,
            delegates=keyword_delegates,
            strategy="lead_only",
        )
