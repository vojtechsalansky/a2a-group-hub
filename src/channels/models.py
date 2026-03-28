# src/channels/models.py
"""Channel data models with role-based membership."""
from __future__ import annotations
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

class MemberRole(str, Enum):
    owner = "owner"
    member = "member"
    observer = "observer"
    @property
    def can_send(self) -> bool:
        return self in (MemberRole.owner, MemberRole.member)
    @property
    def can_manage(self) -> bool:
        return self == MemberRole.owner

@dataclass
class ChannelMember:
    agent_id: str
    name: str
    url: str
    role: MemberRole = MemberRole.member
    auth_token: str | None = None
    joined_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    @property
    def auth_headers(self) -> dict[str, str]:
        if self.auth_token:
            return {"Authorization": f"Bearer {self.auth_token}"}
        return {}

@dataclass
class Channel:
    channel_id: str
    name: str
    context_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    members: dict[str, ChannelMember] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    message_count: int = 0
    default_aggregation: str = "all"
    agent_timeout: int = 60
    metadata: dict = field(default_factory=dict)
    def add_member(self, member: ChannelMember) -> None:
        self.members[member.agent_id] = member
    def remove_member(self, agent_id: str) -> ChannelMember | None:
        return self.members.pop(agent_id, None)
    def get_peers(self, exclude_agent_id: str | None = None) -> list[ChannelMember]:
        return [m for m in self.members.values() if m.agent_id != exclude_agent_id]
    def get_sendable_peers(self, exclude_agent_id: str | None = None) -> list[ChannelMember]:
        return [m for m in self.members.values() if m.agent_id != exclude_agent_id and m.role.can_send]
    def get_observers(self) -> list[ChannelMember]:
        return [m for m in self.members.values() if m.role == MemberRole.observer]
    def get_member_by_url(self, url: str) -> ChannelMember | None:
        for m in self.members.values():
            if m.url == url:
                return m
        return None
