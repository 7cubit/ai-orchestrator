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
    provider: str  # "claude" | "codex" | "agy"
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
        AgentSpec("agy", "Gemini 3.1 Pro (High)", "high"),
    ],
    Tier.SECOND: [
        AgentSpec("claude", "claude-opus-4-8", "high"),
        AgentSpec("codex", "gpt-5.5", "high"),
        AgentSpec("agy", "Gemini 3.1 Pro (High)", "high"),
    ],
    Tier.CODING: [
        AgentSpec("claude", "claude-sonnet-5", "max"),
        AgentSpec("codex", "gpt-5.4", "xhigh"),
        AgentSpec("agy", "Gemini 3.5 Flash (High)", "high"),
    ],
}
# Note on agy (Antigravity CLI): the reasoning level is baked into the model
# string -- "Gemini 3.1 Pro (High)" -- exactly as `agy models` lists it, so
# the effort field above documents intent rather than adding a flag.

@dataclass(frozen=True)
class ModelProfile:
    strengths: str
    weaknesses: str
    cost: str  # relative cost at the coding tier, where the volume is


# What each provider is strong at, weak at, and costs. This is routing
# knowledge, not code: it's injected verbatim into every decompose prompt, so
# the decomposing agent itself -- not a Python heuristic -- assigns each
# subtask to the provider that fits it best and decides how deep to
# decompose (ROUTE mode only; ENSEMBLE always calls everyone).
#
# The claims below are qualitative summaries of where each family sat on
# public benchmarks (SWE-bench-style agentic coding, competitive-programming
# suites, long-context evals) when this was written. Models move monthly:
# refresh these against current numbers, because decompose agents take them
# at face value.
MODEL_PROFILES: dict[str, ModelProfile] = {
    "claude": ModelProfile(
        strengths=(
            "architecture and system design, multi-file refactoring, "
            "long-horizon agentic coding (strongest on SWE-bench-style "
            "fix-a-real-repo tasks), careful instruction-following, prose"
        ),
        weaknesses=(
            "tends to over-engineer simple asks; not the cheapest choice for "
            "bulk mechanical edits"
        ),
        cost="medium (Sonnet 5 at the coding tier)",
    ),
    "codex": ModelProfile(
        strengths=(
            "algorithmically tricky logic, debugging gnarly failures, "
            "math-heavy problems, dense single-file implementations, test "
            "generation (strongest on competitive-programming-style suites)"
        ),
        weaknesses=(
            "weaker at broad multi-file navigation; terse plans that can "
            "drop stated constraints"
        ),
        cost="medium (GPT-5.4 at the coding tier)",
    ),
    "agy": ModelProfile(
        strengths=(
            "Gemini models via the Antigravity seat: very large context "
            "(whole-repo or long-document digestion), multimodal inputs, "
            "fast broad sweeps, extraction and summarization; Flash is the "
            "cheapest, fastest coding-tier option for mechanical or "
            "repetitive edits"
        ),
        weaknesses=(
            "less reliable on subtle multi-step logic; quality drops on "
            "long agentic chains"
        ),
        cost="low (Gemini 3.5 Flash at the coding tier)",
    ),
}

# How many *concurrent* subprocess calls each provider account is allowed
# across the whole run. Subscriptions rate-limit per account, not per tier, so
# fast fan-out at MAIN + SECOND + CODING can starve itself within minutes if
# this is set too high. Start conservative; raise it once you've watched a
# real run and know where your account's ceiling actually is.
PROVIDER_CONCURRENCY: dict[str, int] = {
    "claude": 1,
    "codex": 1,
    "agy": 1,
}

# Hard cap on how many subtasks any single node is allowed to spawn. Without
# this, a tree that's merely 3-wide and 4 tiers deep can fan out to 80+ leaf
# calls from one sentence of input.
MAX_FANOUT = 3

DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_MAX_TURNS = 15

# After synthesis, the orchestrator model reviews the integrated result
# against the original task (checking specifically for hallucinated or
# unsupported content). A "retry" verdict re-dispatches the whole tree with
# the reviewer's issues fed back in, at most this many times.
MAX_QUALITY_RETRIES = 1
