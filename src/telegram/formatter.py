# src/telegram/formatter.py
"""Message formatting for Telegram display."""

from __future__ import annotations


def format_agent_message(agent_name: str, text: str) -> str:
    """Format a hub agent message for Telegram display."""
    return f"\U0001f916 {agent_name}: {text}"


def format_system_message(text: str) -> str:
    """Format a system notification for Telegram display."""
    return f"\u2699\ufe0f {text}"


def format_human_message(user_name: str, text: str) -> str:
    """Format a human (Telegram user) message for hub display."""
    return f"[{user_name}]: {text}"
