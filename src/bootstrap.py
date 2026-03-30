# src/bootstrap.py
"""Idempotent channel bootstrap for OpenClaw agent topology.

On hub startup (BOOTSTRAP_CHANNELS=true), creates the 7 team channels
and registers all 24 agents with correct roles. Safe to run on every
restart — only adds missing channels/members, never deletes.
"""

from __future__ import annotations

import logging
import os

from src.channels.models import ChannelMember, MemberRole
from src.channels.registry import ChannelRegistry

logger = logging.getLogger("a2a-hub.bootstrap")

# Agent port assignments (must match docker-compose)
AGENT_PORTS: dict[str, int] = {
    "nexus": 9001,
    "apollo": 9002,
    "rex": 9003,
    "pixel": 9004,
    "nova": 9005,
    "swift": 9006,
    "hawk": 9007,
    "sentinel": 9008,
    "shield": 9009,
    "phantom": 9010,
    "lens": 9011,
    "aria": 9012,
    "forge": 9013,
    "bolt": 9014,
    "iris": 9015,
    "kai": 9016,
    "mona": 9017,
    "atlas": 9018,
    "echo": 9019,
    "sage": 9020,
    "quill": 9021,
    "scout": 9022,
    "archi": 9023,
    "vigil": 9024,
}

# Docker service names that differ from agent_id
_SERVICE_NAMES: dict[str, str] = {
    "swift": "swift-agent",
    "echo": "echo-agent",
}


def _agent_url(agent_id: str) -> str:
    """Build the A2A endpoint URL for an agent.

    Supports AGENT_URL_TEMPLATE env var for non-Docker environments (e.g. brAIn).
    Template uses {agent_id} placeholder: http://localhost:3000/api/a2a/agent-proxy?agent={agent_id}
    """
    url_template = os.environ.get("AGENT_URL_TEMPLATE")
    if url_template:
        return url_template.replace("{agent_id}", agent_id)
    service = _SERVICE_NAMES.get(agent_id, agent_id)
    port = AGENT_PORTS[agent_id]
    return f"http://{service}:{port}/"


# Channel definitions: channel_id -> (name, owner, members, observers)
OPENCLAW_CHANNELS: list[dict] = [
    {
        "channel_id": "leaders",
        "name": "#leaders",
        "owner": "nexus",
        "members": ["apollo", "sentinel", "forge", "mona", "sage"],
        "observers": ["vigil"],
    },
    {
        "channel_id": "dev-team",
        "name": "#dev-team",
        "owner": "apollo",
        "members": ["rex", "pixel", "nova", "swift", "hawk"],
        "observers": ["vigil"],
    },
    {
        "channel_id": "testing-team",
        "name": "#testing-team",
        "owner": "sentinel",
        "members": ["shield", "phantom", "lens", "aria"],
        "observers": [],
    },
    {
        "channel_id": "ops-team",
        "name": "#ops-team",
        "owner": "forge",
        "members": ["bolt", "iris", "kai"],
        "observers": [],
    },
    {
        "channel_id": "pm-team",
        "name": "#pm-team",
        "owner": "mona",
        "members": ["atlas", "echo"],
        "observers": [],
    },
    {
        "channel_id": "knowledge-team",
        "name": "#knowledge-team",
        "owner": "sage",
        "members": ["quill", "scout", "archi"],
        "observers": [],
    },
    {
        "channel_id": "filip-nexus",
        "name": "#filip-nexus",
        "owner": "nexus",
        "members": [],
        "observers": [],
    },
]


# brAIn dashboard agent channels (used when BRAIN_MODE=true)
BRAIN_CHANNELS: list[dict] = [
    {
        "channel_id": "team",
        "name": "#team",
        "owner": "main",
        "members": ["researcher", "infrastructure", "knowledge-curator", "human"],
        "observers": [],
    },
    {
        "channel_id": "main",
        "name": "#main",
        "owner": "main",
        "members": ["human"],
        "observers": [],
    },
    {
        "channel_id": "claude-code",
        "name": "#claude-code",
        "owner": "claude_code",
        "members": ["human"],
        "observers": [],
    },
    {
        "channel_id": "researcher",
        "name": "#researcher",
        "owner": "researcher",
        "members": ["human"],
        "observers": [],
    },
    {
        "channel_id": "infrastructure",
        "name": "#infrastructure",
        "owner": "infrastructure",
        "members": ["human"],
        "observers": [],
    },
    {
        "channel_id": "knowledge-curator",
        "name": "#knowledge-curator",
        "owner": "knowledge-curator",
        "members": ["human"],
        "observers": [],
    },
]


def _build_member(agent_id: str, role: MemberRole) -> ChannelMember:
    return ChannelMember(
        agent_id=agent_id,
        name=agent_id.capitalize(),
        url=_agent_url(agent_id),
        role=role,
    )


async def bootstrap_channels(registry: ChannelRegistry) -> None:
    """Create channels and register agents. Idempotent.

    When BRAIN_MODE=true, uses brAIn agent channels instead of OpenClaw topology.
    """
    existing = {ch.channel_id: ch for ch in await registry.list_channels()}
    channels_to_use = (
        BRAIN_CHANNELS
        if os.environ.get("BRAIN_MODE", "").lower() in ("true", "1", "yes")
        else OPENCLAW_CHANNELS
    )
    logger.info("Bootstrap mode: %s (%d channels)", "brain" if channels_to_use is BRAIN_CHANNELS else "openclaw", len(channels_to_use))

    for chan_def in channels_to_use:
        cid = chan_def["channel_id"]
        channel = existing.get(cid)

        if channel is None:
            channel = await registry.create_channel(
                name=chan_def["name"],
                channel_id=cid,
                default_aggregation="all",
                agent_timeout=120,
            )
            logger.info("Created channel %s", cid)
        else:
            logger.debug("Channel %s already exists, checking members", cid)

        # Collect desired members with roles
        desired: list[tuple[str, MemberRole]] = []
        desired.append((chan_def["owner"], MemberRole.owner))
        for agent_id in chan_def["members"]:
            desired.append((agent_id, MemberRole.member))
        for agent_id in chan_def["observers"]:
            desired.append((agent_id, MemberRole.observer))

        # Add missing members
        for agent_id, role in desired:
            if agent_id not in channel.members:
                member = _build_member(agent_id, role)
                await registry.add_member(cid, member)
                logger.info("Added %s to %s as %s", agent_id, cid, role.value)
            else:
                logger.debug("%s already in %s", agent_id, cid)

    logger.info(
        "Bootstrap complete: %d channels configured",
        len(channels_to_use),
    )
