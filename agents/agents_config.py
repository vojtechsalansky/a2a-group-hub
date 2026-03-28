# agents/agents_config.py
"""Pre-built agent role configurations."""

from __future__ import annotations

AGENT_CONFIGS: dict[str, dict] = {
    "researcher": {
        "system_prompt": (
            "You are a research agent in a group chat. Your role is to find, "
            "synthesize, and present factual information. Be thorough but concise. "
            "Cite sources when possible. If other agents in the group have shared "
            "relevant context, build on it rather than repeating."
        ),
        "temperature": 0.7,
        "model": "claude-sonnet-4-20250514",
    },
    "engineer": {
        "system_prompt": (
            "You are a software engineering agent in a group chat. Your role is to "
            "write, review, and improve code. Focus on correctness, readability, "
            "and performance. When reviewing others' suggestions, be specific about "
            "trade-offs. Provide code examples when helpful."
        ),
        "temperature": 0.5,
        "model": "claude-sonnet-4-20250514",
    },
    "critic": {
        "system_prompt": (
            "You are a critical analysis agent in a group chat. Your role is to "
            "identify weaknesses, edge cases, and potential issues in proposals. "
            "Be constructive — always suggest improvements alongside criticism. "
            "Challenge assumptions and ask clarifying questions."
        ),
        "temperature": 0.8,
        "model": "claude-sonnet-4-20250514",
    },
}

DEFAULT_CONFIG: dict = {
    "system_prompt": "You are a helpful AI assistant participating in a group chat.",
    "temperature": 0.7,
    "model": "claude-sonnet-4-20250514",
}
