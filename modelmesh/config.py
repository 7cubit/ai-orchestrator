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
    provider: str   # "claude" | "codex" | "agy" | "kimi"
    model: str      # the CLI-facing model id/alias
    effort: str     # provider-specific reasoning/effort/thinking level
    speed: str = "medium"  # "fast" | "medium" | "slow" -- relative wall-clock;
    # used to order failover: after a timeout the next provider tried is the
    # fastest remaining one, so the retry is likelier to finish in time.


# Higher = faster. Failover orders fallback providers by this, descending.
SPEED_RANK: dict[str, int] = {"fast": 3, "medium": 2, "slow": 1}


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
        AgentSpec("claude", "claude-opus-4-8", "max", speed="slow"),
        AgentSpec("codex", "gpt-5.5", "xhigh", speed="medium"),
        AgentSpec("agy", "Gemini 3.1 Pro (High)", "high", speed="medium"),
        AgentSpec("kimi", "kimi-code/kimi-for-coding", "high", speed="medium"),
    ],
    Tier.SECOND: [
        AgentSpec("claude", "claude-opus-4-8", "high", speed="slow"),
        AgentSpec("codex", "gpt-5.5", "high", speed="medium"),
        AgentSpec("agy", "Gemini 3.1 Pro (High)", "high", speed="medium"),
        AgentSpec("kimi", "kimi-code/kimi-for-coding", "high", speed="medium"),
    ],
    Tier.CODING: [
        AgentSpec("claude", "claude-sonnet-5", "max", speed="medium"),
        AgentSpec("codex", "gpt-5.4", "xhigh", speed="medium"),
        AgentSpec("agy", "Gemini 3.5 Flash (High)", "high", speed="fast"),
        AgentSpec("kimi", "kimi-code/kimi-for-coding", "max", speed="fast"),
    ],
}
# Note on providers whose reasoning level lives in the model string:
# - agy: "Gemini 3.1 Pro (High)" is exactly what `agy models` prints.
# - kimi: "kimi-code/kimi-for-coding" is the installed alias for K2.7 Code;
#   thinking is configured model-side, so the effort field documents intent.

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
    "kimi": ModelProfile(
        strengths=(
            "Kimi K2.7 Code: strong coding and long-context work "
            "(262k context window), good at multi-file refactoring, "
            "debugging, and reasoning-heavy implementation tasks"
        ),
        weaknesses=(
            "smaller ecosystem of verified agentic benchmarks than Claude; "
            "prompt-mode output is less structured than Claude Code JSON"
        ),
        cost="medium (Kimi K2.7 Code at the coding tier)",
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
    "kimi": 1,
}

# Hard cap on how many subtasks any single node is allowed to spawn. Without
# this, a tree that's merely 3-wide and 4 tiers deep can fan out to 80+ leaf
# calls from one sentence of input.
MAX_FANOUT = 3

DEFAULT_TIMEOUT_SECONDS = 600
# Governs coding-tier calls (reasoning calls are separately capped at
# REASONING_MAX_TURNS below). 15 was too low: a real coding slice -- read an
# 800-line handler, study test patterns, write tests, run them, fix, re-run --
# needs 20-40 turns. A live tokylomail run showed Sonnet coding calls burning
# 70k tokens over 12 min then failing at the cap (error_max_turns), while
# failover salvaged the work on codex/agy/kimi. Headroom is bounded by the per-call
# --timeout and the aggregate --run-timeout, so a higher ceiling can't run away.
DEFAULT_MAX_TURNS = 40

# Decompose, synthesize, and review calls are single-shot text->JSON work;
# they don't need the coding tier's turn budget or timeout. Giving them the
# full --timeout/--max-turns means a stalled planner burns 10 minutes before
# failover, and a decompose agent with 15 turns of tool access can wander the
# repo before answering. These caps apply to every non-leaf call (further
# clamped by --timeout/--max-turns when those are set lower).
REASONING_TIMEOUT_SECONDS = 240
REASONING_MAX_TURNS = 3

# Aggregate wall-clock budget for a whole run (audit MM-06's last gap: no
# run-level deadline existed, only per-call timeouts). Past the deadline no
# new agent calls start -- in-flight calls finish, everything not yet started
# fails fast with a "deadline exceeded" error, and partial results propagate
# up honestly. --run-timeout overrides; 0 disables.
RUN_TIMEOUT_SECONDS = 3600

# MM-08: spawned agents get this env allowlist instead of the full parent
# environment, so credentials in the operator's shell (ANTHROPIC_API_KEY,
# cloud tokens, ...) are never visible to permission-bypassed agents. All
# three CLIs authenticate from their own state under HOME, verified live. If
# you *want* API-key burst billing, add the key name here deliberately.
ENV_ALLOWLIST = [
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TERM",
    "LANG", "LC_ALL", "TZ",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
]

# MM-14: caps on text volume, so one agent dumping a huge transcript can't
# blow out memory or the parent tier's context window. Truncation keeps head
# and tail (the end usually carries the conclusion).
MAX_OUTPUT_CHARS = 200_000        # per-call output cap, applied in agents.py
SYNTHESIS_CHILD_CHARS = 40_000    # per-child cap inside a synthesis prompt

# MM-11: effort strings are interpolated into a codex -c config override;
# validate against known tokens so the invariant "effort is always a static
# config literal" isn't one refactor away from an injection.
ALLOWED_EFFORTS = {"low", "medium", "high", "xhigh", "max"}

# After synthesis, the orchestrator model reviews the integrated result
# against the original task (checking specifically for hallucinated or
# unsupported content). A "retry" verdict re-dispatches the whole tree with
# the reviewer's issues fed back in, at most this many times.
MAX_QUALITY_RETRIES = 1
