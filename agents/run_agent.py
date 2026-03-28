#!/usr/bin/env python
# agents/run_agent.py
"""CLI launcher for A2A agents."""

from __future__ import annotations

import argparse
import logging
import uvicorn

from a2a.server.apps.jsonrpc.starlette_app import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCard, AgentCapabilities, AgentSkill

from agents.agents_config import AGENT_CONFIGS
from agents.demo_agent import DemoEchoExecutor
from agents.llm_agent import LLMAgentExecutor


def build_agent_card(name: str, port: int) -> AgentCard:
    return AgentCard(
        name=name,
        description=f"A2A agent: {name}",
        url=f"http://localhost:{port}/",
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=False),
        skills=[
            AgentSkill(
                id=name,
                name=name,
                description=f"Group chat participant ({name})",
            )
        ],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an A2A agent")
    parser.add_argument(
        "--role",
        choices=list(AGENT_CONFIGS.keys()) + ["echo"],
        default="echo",
        help="Agent role (default: echo)",
    )
    parser.add_argument("--port", type=int, default=9001, help="Port to listen on")
    parser.add_argument("--name", type=str, default=None, help="Agent name (defaults to role)")
    parser.add_argument("--api-key", type=str, default=None, help="Anthropic API key")
    parser.add_argument("--log-level", default="INFO", help="Logging level")
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper()))
    name = args.name or args.role

    if args.role == "echo":
        executor = DemoEchoExecutor()
    else:
        executor = LLMAgentExecutor(role=args.role, api_key=args.api_key)

    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
    )

    agent_card = build_agent_card(name, args.port)
    app = A2AStarletteApplication(agent_card=agent_card, http_handler=handler)

    uvicorn.run(app.build(), host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
