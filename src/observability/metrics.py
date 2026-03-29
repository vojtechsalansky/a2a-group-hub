"""Prometheus-compatible metrics for the hub."""
import time
from dataclasses import dataclass, field

MAX_FANOUT_SAMPLES = 1000


@dataclass
class MetricsCollector:
    """Collects and exposes hub metrics in Prometheus text and JSON formats."""

    channels_total: int = 0
    agents_total: int = 0
    messages_total: int = 0
    fanout_durations: list[float] = field(default_factory=list)
    agent_errors: dict[str, int] = field(default_factory=dict)
    strategy_usage: dict[str, int] = field(default_factory=dict)
    channel_message_counts: dict[str, int] = field(default_factory=dict)
    webhook_success: int = 0
    webhook_failure: int = 0
    _start_time: float = field(default_factory=time.time)

    def record_message(self) -> None:
        self.messages_total += 1

    def record_channel_message(self, channel_id: str) -> None:
        self.channel_message_counts[channel_id] = self.channel_message_counts.get(channel_id, 0) + 1

    def record_fanout_duration(self, duration: float) -> None:
        self.fanout_durations.append(duration)
        if len(self.fanout_durations) > MAX_FANOUT_SAMPLES:
            self.fanout_durations = self.fanout_durations[-MAX_FANOUT_SAMPLES:]

    def record_agent_error(self, agent_id: str) -> None:
        self.agent_errors[agent_id] = self.agent_errors.get(agent_id, 0) + 1

    def record_strategy_usage(self, strategy: str) -> None:
        self.strategy_usage[strategy] = self.strategy_usage.get(strategy, 0) + 1

    def record_webhook_delivery(self, success: bool) -> None:
        if success:
            self.webhook_success += 1
        else:
            self.webhook_failure += 1

    def update_counts(self, channels: int, agents: int) -> None:
        self.channels_total = channels
        self.agents_total = agents

    def _percentile(self, values: list[float], p: float) -> float:
        if not values:
            return 0.0
        sorted_vals = sorted(values)
        idx = int(len(sorted_vals) * p / 100)
        return sorted_vals[min(idx, len(sorted_vals) - 1)]

    def to_json(self) -> dict:
        """Return metrics as a JSON-serializable dict."""
        return {
            "uptime_seconds": time.time() - self._start_time,
            "messages_total": self.messages_total,
            "channels_total": self.channels_total,
            "agents_total": self.agents_total,
            "fanout_latency": {
                "p50": self._percentile(self.fanout_durations, 50),
                "p95": self._percentile(self.fanout_durations, 95),
                "p99": self._percentile(self.fanout_durations, 99),
            },
            "fanout_samples": len(self.fanout_durations),
            "agent_errors": dict(self.agent_errors),
            "strategy_usage": dict(self.strategy_usage),
            "messages_per_channel": dict(self.channel_message_counts),
            "webhook_deliveries": {
                "success": self.webhook_success,
                "failure": self.webhook_failure,
            },
        }

    def to_prometheus(self) -> str:
        """Render metrics in Prometheus text exposition format."""
        lines = []

        lines.append("# HELP a2a_hub_channels_total Number of active channels")
        lines.append("# TYPE a2a_hub_channels_total gauge")
        lines.append(f"a2a_hub_channels_total {self.channels_total}")

        lines.append("# HELP a2a_hub_agents_total Number of registered agents across all channels")
        lines.append("# TYPE a2a_hub_agents_total gauge")
        lines.append(f"a2a_hub_agents_total {self.agents_total}")

        lines.append("# HELP a2a_hub_messages_total Total messages processed")
        lines.append("# TYPE a2a_hub_messages_total counter")
        lines.append(f"a2a_hub_messages_total {self.messages_total}")

        p50 = self._percentile(self.fanout_durations, 50)
        p95 = self._percentile(self.fanout_durations, 95)
        p99 = self._percentile(self.fanout_durations, 99)
        lines.append("# HELP a2a_hub_fanout_duration_seconds Fan-out broadcast latency")
        lines.append("# TYPE a2a_hub_fanout_duration_seconds summary")
        lines.append(f'a2a_hub_fanout_duration_seconds{{quantile="0.5"}} {p50:.3f}')
        lines.append(f'a2a_hub_fanout_duration_seconds{{quantile="0.95"}} {p95:.3f}')
        lines.append(f'a2a_hub_fanout_duration_seconds{{quantile="0.99"}} {p99:.3f}')

        if self.agent_errors:
            lines.append("# HELP a2a_hub_agent_errors_total Errors per agent")
            lines.append("# TYPE a2a_hub_agent_errors_total counter")
        for agent_id, count in self.agent_errors.items():
            lines.append(f'a2a_hub_agent_errors_total{{agent="{agent_id}"}} {count}')

        if self.strategy_usage:
            lines.append("# HELP a2a_hub_aggregation_strategy_usage Aggregation strategy usage count")
            lines.append("# TYPE a2a_hub_aggregation_strategy_usage counter")
        for strategy, count in self.strategy_usage.items():
            lines.append(f'a2a_hub_aggregation_strategy_usage{{strategy="{strategy}"}} {count}')

        lines.append("# HELP a2a_hub_webhook_deliveries_total Webhook delivery attempts")
        lines.append("# TYPE a2a_hub_webhook_deliveries_total counter")
        lines.append(f'a2a_hub_webhook_deliveries_total{{status="success"}} {self.webhook_success}')
        lines.append(f'a2a_hub_webhook_deliveries_total{{status="failure"}} {self.webhook_failure}')

        uptime = time.time() - self._start_time
        lines.append("# HELP a2a_hub_uptime_seconds Hub uptime in seconds")
        lines.append("# TYPE a2a_hub_uptime_seconds gauge")
        lines.append(f"a2a_hub_uptime_seconds {uptime:.1f}")

        return "\n".join(lines) + "\n"
