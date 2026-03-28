# src/hub/aggregator.py
"""Response aggregation strategies for group chat hub."""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from datetime import datetime, timezone
from enum import Enum

from a2a.types import (
    Artifact, Message, Part, Role, Task, TaskState, TaskStatus, TextPart,
)

from src.hub.fanout import FanOutResult

logger = logging.getLogger("a2a-hub.aggregator")


class AggregationStrategy(str, Enum):
    all = "all"
    first = "first"
    consensus = "consensus"
    voting = "voting"
    best_of_n = "best_of_n"


class Aggregator:

    def aggregate(
        self,
        results: list[FanOutResult],
        strategy: AggregationStrategy,
        task_id: str,
        context_id: str,
        channel_id: str,
        channel_name: str,
    ) -> Task:
        actual_strategy = strategy
        if strategy == AggregationStrategy.first:
            artifacts = self._strategy_first(results, task_id)
        elif strategy == AggregationStrategy.voting:
            artifacts = self._strategy_voting(results, task_id)
        elif strategy in (AggregationStrategy.consensus, AggregationStrategy.best_of_n):
            # These need LLM — fall back to all with a note
            logger.warning(f"Strategy '{strategy.value}' requires LLM. Falling back to 'all'.")
            actual_strategy = AggregationStrategy.all
            artifacts = self._strategy_all(results, task_id)
        else:
            artifacts = self._strategy_all(results, task_id)

        success_count = sum(1 for r in results if not r.error)
        return Task(
            id=task_id,
            context_id=context_id,
            status=TaskStatus(
                state=TaskState.completed,
                timestamp=datetime.now(timezone.utc).isoformat(),
                message=Message(
                    role=Role.agent,
                    parts=[Part(root=TextPart(
                        text=f"Broadcast complete: {success_count}/{len(results)} agents responded in #{channel_name}",
                    ))],
                    message_id=str(uuid.uuid4()),
                ),
            ),
            artifacts=artifacts,
            metadata={
                "channel_id": channel_id,
                "channel_name": channel_name,
                "strategy": actual_strategy.value,
                "requested_strategy": strategy.value,
                "peer_count": len(results),
                "success_count": success_count,
                "error_count": len(results) - success_count,
            },
        )

    def _strategy_all(self, results: list[FanOutResult], task_id: str) -> list[Artifact]:
        artifacts = []
        for i, r in enumerate(results):
            text = r.error and f"[Error from {r.agent_name}]: {r.error}" or r.response_text or "[No response]"
            artifacts.append(Artifact(
                artifact_id=f"{task_id}-{r.agent_id}-{i}",
                name=f"Response from {r.agent_name}",
                parts=[Part(root=TextPart(text=text))],
            ))
        return artifacts

    def _strategy_first(self, results: list[FanOutResult], task_id: str) -> list[Artifact]:
        for r in results:
            if r.response_text and not r.error:
                return [Artifact(
                    artifact_id=f"{task_id}-{r.agent_id}-first",
                    name=f"Response from {r.agent_name} (first)",
                    parts=[Part(root=TextPart(text=r.response_text))],
                )]
        # All errored — return first error
        if results:
            r = results[0]
            return [Artifact(
                artifact_id=f"{task_id}-{r.agent_id}-first",
                name=f"Error from {r.agent_name}",
                parts=[Part(root=TextPart(text=r.error or "[No response]"))],
            )]
        return []

    def _strategy_voting(self, results: list[FanOutResult], task_id: str) -> list[Artifact]:
        votes = Counter()
        for r in results:
            if r.response_text and not r.error:
                vote = r.response_text.strip().lower()
                votes[vote] += 1

        if not votes:
            return [Artifact(
                artifact_id=f"{task_id}-voting",
                name="Voting Result",
                parts=[Part(root=TextPart(text="No valid votes received."))],
            )]

        total = sum(votes.values())
        winner, winner_count = votes.most_common(1)[0]
        breakdown = "\n".join(f"  - {v}: {c}/{total} votes" for v, c in votes.most_common())

        return [Artifact(
            artifact_id=f"{task_id}-voting",
            name="Voting Result",
            parts=[Part(root=TextPart(
                text=f"Result: {winner} ({winner_count}/{total} votes)\n\nBreakdown:\n{breakdown}",
            ))],
        )]
