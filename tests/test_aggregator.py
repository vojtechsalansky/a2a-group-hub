# tests/test_aggregator.py
"""Tests for aggregation strategies."""

import pytest
from src.hub.fanout import FanOutResult
from src.hub.aggregator import Aggregator, AggregationStrategy


@pytest.fixture
def three_results():
    return [
        FanOutResult(agent_id="rex", agent_name="Rex", response_text="I think we should use PostgreSQL"),
        FanOutResult(agent_id="pixel", agent_name="Pixel", response_text="I agree, PostgreSQL is best"),
        FanOutResult(agent_id="nova", agent_name="Nova", response_text="Redis might be better for this use case"),
    ]


@pytest.fixture
def results_with_error():
    return [
        FanOutResult(agent_id="rex", agent_name="Rex", response_text="My response"),
        FanOutResult(agent_id="pixel", agent_name="Pixel", error="Connection refused"),
    ]


class TestAllStrategy:
    def test_returns_all_results(self, three_results):
        agg = Aggregator()
        task = agg.aggregate(three_results, strategy=AggregationStrategy.all,
                             task_id="t1", context_id="c1", channel_id="dev", channel_name="dev-team")
        assert len(task.artifacts) == 3

    def test_includes_errors_as_artifacts(self, results_with_error):
        agg = Aggregator()
        task = agg.aggregate(results_with_error, strategy=AggregationStrategy.all,
                             task_id="t1", context_id="c1", channel_id="dev", channel_name="dev-team")
        assert len(task.artifacts) == 2
        assert "Error" in task.artifacts[1].parts[0].root.text or "error" in task.artifacts[1].name.lower() or "Connection refused" in task.artifacts[1].parts[0].root.text


class TestFirstStrategy:
    def test_returns_first_only(self, three_results):
        agg = Aggregator()
        task = agg.aggregate(three_results, strategy=AggregationStrategy.first,
                             task_id="t1", context_id="c1", channel_id="dev", channel_name="dev-team")
        assert len(task.artifacts) == 1


class TestStrategyFallback:
    def test_consensus_falls_back_to_all_and_reports_actual_strategy(self, three_results):
        agg = Aggregator()
        task = agg.aggregate(three_results, strategy=AggregationStrategy.consensus,
                             task_id="t1", context_id="c1", channel_id="dev", channel_name="dev-team")
        # Should report actual strategy as "all" (fallback), not "consensus"
        assert task.metadata["strategy"] == "all"
        assert task.metadata["requested_strategy"] == "consensus"
        # Should still return all results
        assert len(task.artifacts) == 3

    def test_best_of_n_falls_back_to_all_and_reports_actual_strategy(self, three_results):
        agg = Aggregator()
        task = agg.aggregate(three_results, strategy=AggregationStrategy.best_of_n,
                             task_id="t1", context_id="c1", channel_id="dev", channel_name="dev-team")
        assert task.metadata["strategy"] == "all"
        assert task.metadata["requested_strategy"] == "best_of_n"

    def test_non_fallback_strategy_matches_requested(self, three_results):
        agg = Aggregator()
        task = agg.aggregate(three_results, strategy=AggregationStrategy.first,
                             task_id="t1", context_id="c1", channel_id="dev", channel_name="dev-team")
        assert task.metadata["strategy"] == "first"
        assert task.metadata["requested_strategy"] == "first"


class TestVotingStrategy:
    def test_voting_counts_responses(self):
        results = [
            FanOutResult(agent_id="a", agent_name="A", response_text="approve"),
            FanOutResult(agent_id="b", agent_name="B", response_text="approve"),
            FanOutResult(agent_id="c", agent_name="C", response_text="reject"),
        ]
        agg = Aggregator()
        task = agg.aggregate(results, strategy=AggregationStrategy.voting,
                             task_id="t1", context_id="c1", channel_id="dev", channel_name="dev-team")
        assert task.artifacts  # should have voting summary
        summary_text = task.artifacts[0].parts[0].root.text
        assert "approve" in summary_text.lower()
