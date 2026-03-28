# src/channels/permissions.py
"""Role-based permission checks for channel operations."""

from __future__ import annotations

from src.channels.models import Channel


class PermissionError(Exception):
    """Raised when an agent lacks permission for an operation."""
    pass


def check_can_send(channel: Channel, sender_id: str | None) -> None:
    """Verify sender has permission to send messages in this channel."""
    if sender_id is None:
        return  # external/hub messages always allowed

    member = channel.members.get(sender_id)
    if not member:
        raise PermissionError(f"Agent '{sender_id}' is not a member of #{channel.name}")
    if not member.role.can_send:
        raise PermissionError(
            f"Agent '{sender_id}' is an observer in #{channel.name} and cannot send messages"
        )


def check_can_manage(channel: Channel, agent_id: str) -> None:
    """Verify agent has permission to manage channel (add/remove members, delete)."""
    member = channel.members.get(agent_id)
    if not member:
        raise PermissionError(f"Agent '{agent_id}' is not a member of #{channel.name}")
    if not member.role.can_manage:
        raise PermissionError(
            f"Agent '{agent_id}' ({member.role.value}) cannot manage #{channel.name}"
        )
