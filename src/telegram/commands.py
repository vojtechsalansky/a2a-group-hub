"""Telegram admin command handlers for OpenClaw system monitoring."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

logger = logging.getLogger("a2a-hub.commands")

# Agent service registry — name, port, model tier, team
AGENT_SERVICES: dict[str, dict] = {
    "nexus": {"port": 9001, "model": "opus", "team": "executive"},
    "apollo": {"port": 9002, "model": "opus", "team": "dev"},
    "rex": {"port": 9003, "model": "opus", "team": "dev"},
    "pixel": {"port": 9004, "model": "opus", "team": "dev"},
    "nova": {"port": 9005, "model": "opus", "team": "dev"},
    "swift": {"port": 9006, "model": "opus", "team": "dev"},
    "hawk": {"port": 9007, "model": "opus", "team": "dev"},
    "sentinel": {"port": 9008, "model": "opus", "team": "testing"},
    "shield": {"port": 9009, "model": "sonnet", "team": "testing"},
    "phantom": {"port": 9010, "model": "sonnet", "team": "testing"},
    "lens": {"port": 9011, "model": "sonnet", "team": "testing"},
    "aria": {"port": 9012, "model": "sonnet", "team": "testing"},
    "forge": {"port": 9013, "model": "opus", "team": "ops"},
    "bolt": {"port": 9014, "model": "sonnet", "team": "ops"},
    "iris": {"port": 9015, "model": "sonnet", "team": "ops"},
    "kai": {"port": 9016, "model": "sonnet", "team": "ops"},
    "mona": {"port": 9017, "model": "opus", "team": "pm"},
    "atlas": {"port": 9018, "model": "sonnet", "team": "pm"},
    "echo": {"port": 9019, "model": "sonnet", "team": "pm"},
    "sage": {"port": 9020, "model": "opus", "team": "knowledge"},
    "quill": {"port": 9021, "model": "sonnet", "team": "knowledge"},
    "scout": {"port": 9022, "model": "sonnet", "team": "knowledge"},
    "archi": {"port": 9023, "model": "sonnet", "team": "knowledge"},
    "vigil": {"port": 9024, "model": "opus", "team": "audit"},
}


def _format_uptime(seconds: int | float) -> str:
    """Convert seconds to human-readable uptime: '3d 2h', '2h 15m', '45m', '30s'."""
    seconds = int(seconds)
    if seconds < 0:
        return "0s"

    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60

    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    if minutes > 0:
        return f"{minutes}m"
    return f"{secs}s"


class CommandHandler:
    """Handles Telegram admin commands for system monitoring."""

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        hub_base_url: str = "http://localhost:8000",
    ) -> None:
        self._http = http_client
        self._hub_url = hub_base_url.rstrip("/")

    # Docker service names differ from agent names for these agents
    _SERVICE_NAMES = {"swift": "swift-agent", "echo": "echo-agent"}

    async def _check_agent_health(self, agent_name: str) -> dict | None:
        """Check a single agent's /health endpoint. Returns JSON dict or None."""
        info = AGENT_SERVICES.get(agent_name)
        if not info:
            return None
        hostname = self._SERVICE_NAMES.get(agent_name, agent_name)
        try:
            resp = await self._http.get(
                f"http://{hostname}:{info['port']}/health",
                timeout=3.0,
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return None

    async def cmd_status(self) -> str:
        """Aggregate agent health + hub metrics into a compact dashboard."""
        # 1. Gather all agent health checks in parallel
        agent_names = list(AGENT_SERVICES.keys())
        tasks = [self._check_agent_health(name) for name in agent_names]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 2. Fetch hub metrics
        hub_metrics: dict | None = None
        try:
            resp = await self._http.get(f"{self._hub_url}/api/metrics", timeout=5.0)
            if resp.status_code == 200:
                hub_metrics = resp.json()
        except Exception:
            pass

        # 3. Build dashboard
        online_count = 0
        lines: list[str] = []
        lines.append("OpenClaw Status Dashboard")
        lines.append("=" * 40)

        # Hub uptime
        hub_uptime = ""
        if hub_metrics:
            hub_uptime = _format_uptime(hub_metrics.get("uptime_seconds", 0))

        # Agent grid header
        lines.append("")
        lines.append(f"{'Team':<9} {'Agent':<10} {'St':<4} {'Uptime':<7} {'Mem':<6}")
        lines.append("-" * 40)

        for i, name in enumerate(agent_names):
            info = AGENT_SERVICES[name]
            result = results[i]

            if isinstance(result, dict) and result is not None:
                online_count += 1
                status = "OK"
                uptime = _format_uptime(result.get("uptime_seconds", 0))
                mem_mb = result.get("memory_mb", 0)
                mem = f"{mem_mb:.0f}MB" if mem_mb else "-"
            else:
                status = "OFF"
                uptime = "-"
                mem = "-"

            lines.append(f"{info['team']:<9} {name:<10} {status:<4} {uptime:<7} {mem:<6}")

        # Summary line after agents
        lines[1] = f"Agents: {online_count}/{len(agent_names)} online"
        if hub_uptime:
            lines[1] += f" | Hub uptime: {hub_uptime}"

        # Hub metrics section
        if hub_metrics:
            lines.append("")
            lines.append("Hub Metrics")
            lines.append("-" * 40)

            fl = hub_metrics.get("fanout_latency", {})
            lines.append(
                f"Fan-out: p50={fl.get('p50', 0):.1f}s "
                f"p95={fl.get('p95', 0):.1f}s"
            )
            lines.append(f"Messages: {hub_metrics.get('messages_total', 0)} total")

            # Channel message counts (compact)
            mpc = hub_metrics.get("messages_per_channel", {})
            if mpc:
                parts = [f"{ch}({cnt})" for ch, cnt in mpc.items()]
                lines.append(f"Channels: {' '.join(parts)}")

            # Agent errors
            errors = hub_metrics.get("agent_errors", {})
            if errors:
                err_parts = [f"{a}({c})" for a, c in errors.items()]
                lines.append(f"Errors: {' '.join(err_parts)}")
        else:
            lines.append("")
            lines.append("Hub metrics: unavailable")

        return "<pre>" + "\n".join(lines) + "</pre>"

    async def cmd_health(self, agent_name: str) -> str:
        """Detailed single-agent health view."""
        if agent_name not in AGENT_SERVICES:
            return f"Unknown agent: {agent_name}\nAvailable: {', '.join(sorted(AGENT_SERVICES.keys()))}"

        info = AGENT_SERVICES[agent_name]
        health = await self._check_agent_health(agent_name)

        if health is None:
            return (
                f"<pre>Agent: {agent_name} ({info['team'].title()} Team)\n"
                f"Status: OFFLINE\n"
                f"Port: {info['port']}\n"
                f"Expected model: {info['model']}\n\n"
                f"Agent is not responding on port {info['port']}.</pre>"
            )

        uptime_raw = health.get("uptime_seconds", 0)
        # More detailed uptime for single agent view
        hours = int(uptime_raw) // 3600
        minutes = (int(uptime_raw) % 3600) // 60
        secs = int(uptime_raw) % 60
        uptime_str = f"{hours}h {minutes}m {secs}s" if hours else f"{minutes}m {secs}s"

        mem_mb = health.get("memory_mb", 0)
        session = health.get("session_id") or "none"
        if len(session) > 16:
            session = session[:16] + "..."
        model = health.get("model", "unknown")
        mem_enabled = health.get("memory_enabled", False)

        return (
            f"<pre>Agent: {agent_name} ({info['team'].title()} Team)\n"
            f"Model: {model}\n"
            f"Status: OK\n"
            f"Uptime: {uptime_str}\n"
            f"Memory: {mem_mb:.1f} MB\n"
            f"Session: {session}\n"
            f"Memory Store: {'enabled' if mem_enabled else 'disabled'}</pre>"
        )

    async def cmd_metrics(self) -> str:
        """Hub metrics view."""
        try:
            resp = await self._http.get(f"{self._hub_url}/api/metrics", timeout=5.0)
            if resp.status_code != 200:
                return f"Failed to fetch hub metrics (HTTP {resp.status_code})"
            metrics = resp.json()
        except Exception as e:
            return f"Failed to fetch hub metrics: {e}"

        uptime = _format_uptime(metrics.get("uptime_seconds", 0))
        fl = metrics.get("fanout_latency", {})
        msg_total = metrics.get("messages_total", 0)
        samples = metrics.get("fanout_samples", 0)

        lines = [
            "Hub Metrics",
            "=" * 40,
            f"Uptime: {uptime}",
            f"Messages: {msg_total} total",
            "",
            "Fan-out Latency:",
            f"  p50: {fl.get('p50', 0):.1f}s | p95: {fl.get('p95', 0):.1f}s | p99: {fl.get('p99', 0):.1f}s",
            f"  Samples: {samples}",
        ]

        # Messages per channel
        mpc = metrics.get("messages_per_channel", {})
        if mpc:
            lines.append("")
            lines.append("Messages per Channel:")
            for ch, cnt in sorted(mpc.items()):
                lines.append(f"  {ch}: {cnt}")

        # Agent errors
        errors = metrics.get("agent_errors", {})
        if errors:
            lines.append("")
            lines.append("Agent Errors:")
            for agent, cnt in sorted(errors.items()):
                lines.append(f"  {agent}: {cnt} errors")

        # Strategy usage
        strategy = metrics.get("strategy_usage", {})
        if strategy:
            lines.append("")
            lines.append("Strategy Usage:")
            for s, cnt in sorted(strategy.items()):
                lines.append(f"  {s}: {cnt}")

        # Webhook deliveries
        webhooks = metrics.get("webhook_deliveries", {})
        if webhooks:
            lines.append("")
            lines.append("Webhook Deliveries:")
            lines.append(f"  success: {webhooks.get('success', 0)}")
            lines.append(f"  failure: {webhooks.get('failure', 0)}")

        return "<pre>" + "\n".join(lines) + "</pre>"

    async def cmd_logs(self, agent_name: str) -> str:
        """Log viewing guidance — Docker socket not available from hub container."""
        if agent_name not in AGENT_SERVICES:
            return f"Unknown agent: {agent_name}\nAvailable: {', '.join(sorted(AGENT_SERVICES.keys()))}"

        return (
            f"<pre>Logs for: {agent_name}\n"
            f"Port: {AGENT_SERVICES[agent_name]['port']}\n\n"
            f"Log viewing requires server access:\n"
            f"  docker logs {agent_name} --tail 10\n\n"
            f"For structured JSON logs:\n"
            f"  docker logs {agent_name} --tail 10 | jq .\n\n"
            f"Log proxy endpoint planned for future\n"
            f"enhancement.</pre>"
        )
