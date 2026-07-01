"""Tier -> provider/model/effort mapping, plus knobs that bound fan-out and
concurrency.

This is deliberately just data. Nothing here talks to a CLI; see agents.py
for that. Edit TIER_CONFIG to change which models back which tier without
touching orchestration logic.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .tasks import Tier


@dataclass(frozen=True)
class AgentSpec:
    provider: str  # "claude" | "codex" | "gemini"
    model: str      # the CLI-facing model id/alias
    effort: str      # provider-specific reasoning/effort/thinking level


class DispatchMode(Enum):
    ROUTE = "route"        # one provider per node, picked round-robin
    ENSEMBLE = "ensemble"  # call every provider at that tier; parent reconciles


# --- Your spec, translated directly into config -----------------------------
# Orchestrator: Fable 5. Fable/Mythos-class models use adaptive thinking only
# (no manual budget the way Opus/Sonnet have), so "effort" here just carries
# through Claude Code's own high/max dial rather than a separately-invented one.
TIER_CONFIG: dict[Tier, list[AgentSpec]] = {
    Tier.ORCHESTRATOR: [
        AgentSpec("claude", "claude-fable-5", "high"),
    ],
    Tier.MAIN: [
        AgentSpec("claude", "claude-opus-4-8", "max"),
        AgentSpec("codex", "gpt-5.5", "xhigh"),
        AgentSpec("gemini", "gemini-3.1-pro", "high"),
    ],
    Tier.SECOND: [
        AgentSpec("claude", "claude-opus-4-8", "high"),
        AgentSpec("codex", "gpt-5.5", "high"),
        AgentSpec("gemini", "gemini-3.1-pro", "high"),
    ],
    Tier.CODING: [
        AgentSpec("claude", "claude-sonnet-5", "max"),
        AgentSpec("codex", "gpt-5.4", "xhigh"),
        AgentSpec("gemini", "gemini-3.5-flash", "high"),
    ],
}

# How many *concurrent* subprocess calls each provider account is allowed
# across the whole run. Subscriptions rate-limit per account, not per tier, so
# fast fan-out at MAIN + SECOND + CODING can starve itself within minutes if
# this is set too high. Start conservative; raise it once you've watched a
# real run and know where your account's ceiling actually is.
PROVIDER_CONCURRENCY: dict[str, int] = {
    "claude": 1,
    "codex": 1,
    "gemini": 1,
}

# Hard cap on how many subtasks any single node is allowed to spawn. Without
# this, a tree that's merely 3-wide and 4 tiers deep can fan out to 80+ leaf
# calls from one sentence of input.
MAX_FANOUT = 3

DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_MAX_TURNS = 15
