"""Core data structures shared across the orchestration layer."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Tier(Enum):
    """The four levels in the delegation hierarchy, top to bottom."""
    ORCHESTRATOR = "orchestrator"
    MAIN = "main"
    SECOND = "second"
    CODING = "coding"


# Order matters: this is the delegation path a big task follows.
TIER_ORDER = [Tier.ORCHESTRATOR, Tier.MAIN, Tier.SECOND, Tier.CODING]


def next_tier(tier: Tier) -> Optional[Tier]:
    """Return the tier one level below `tier`, or None if `tier` is the leaf (CODING)."""
    idx = TIER_ORDER.index(tier)
    if idx + 1 >= len(TIER_ORDER):
        return None
    return TIER_ORDER[idx + 1]


@dataclass
class Task:
    """A unit of work at a given tier, optionally the child of a parent task."""
    description: str
    tier: Tier
    parent_id: Optional[str] = None
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    depth: int = 0
    # Routing hint assigned by the parent's decompose call, based on
    # MODEL_STRENGTHS. None (or a provider not configured at this tier)
    # falls back to round-robin. Ignored in ENSEMBLE mode.
    preferred_provider: Optional[str] = None
    # Definition of done assigned by the parent's decompose call: one
    # concrete check (a command, a test, an observable behavior) that
    # proves this slice is complete. Rides down to the leaf prompt so the
    # coding agent verifies its own work instead of just claiming success.
    verify: Optional[str] = None


@dataclass
class TaskResult:
    """What an agent call (or a whole subtree under it) produced for a task."""
    task: Task
    provider: str
    model: str
    effort: str
    output: str
    success: bool
    children: list["TaskResult"] = field(default_factory=list)
    error: Optional[str] = None
    raw: Optional[dict] = None  # parsed JSON from the CLI, when available
    # Wall-clock seconds this call spent inside the provider semaphore
    # (queue wait excluded). Feeds the end-of-run cost report.
    elapsed: Optional[float] = None
    # Root result only: the orchestrator's post-synthesis quality review
    # ({"verdict": "accept"|"retry", "issues": [...], "attempts": N}).
    review: Optional[dict] = None
    # Set when this call succeeded only after failing over from other
    # providers: the list of "provider(error)" attempts that preceded it.
    failover_from: Optional[list[str]] = None
