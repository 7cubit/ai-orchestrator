"""Prompt templates for decomposition (parent -> children) and synthesis
(children -> parent). Treat these as a first draft: prompt-tuning this layer
is the part of the project that never really finishes, not a one-time task.
"""
from __future__ import annotations

import json
import re

from .tasks import Task, TaskResult, Tier

# Forces Claude's decomposition calls into strict JSON: the orchestrator
# passes this to every decompose call, and ClaudeCodeAgent forwards it via
# `claude --json-schema`. Codex/Gemini accept and ignore it (JSON is still
# requested in the prompt text below, and parse_subtasks degrades gracefully).
CLAUDE_SUBTASK_SCHEMA = {
    "type": "object",
    "properties": {
        "subtasks": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
        }
    },
    "required": ["subtasks"],
}


def decompose_prompt(task: Task, lower_tier: Tier, max_fanout: int) -> str:
    return (
        f"You are coordinating a multi-agent build. Break the following task "
        f"into at most {max_fanout} concrete, independent subtasks suitable "
        f"for the '{lower_tier.value}' tier to execute. Each subtask should "
        f"be self-contained enough that an agent with no other context could "
        f"act on it directly.\n\n"
        f"Task:\n{task.description}\n\n"
        f'Respond with ONLY JSON in this shape: {{"subtasks": ["...", "..."]}}. '
        f"No prose before or after the JSON."
    )


def synthesize_prompt(task: Task, children: list[TaskResult]) -> str:
    child_block = "\n\n".join(
        f"--- Subtask {i + 1} ({c.provider}:{c.model}) ---\n{c.output}"
        for i, c in enumerate(children)
    )
    return (
        f"You delegated the task below and got back the results shown. "
        f"Integrate them into a single coherent result. Resolve any "
        f"contradictions between subtask outputs, and explicitly call out "
        f"any you couldn't resolve rather than silently picking one.\n\n"
        f"Original task:\n{task.description}\n\n"
        f"Subtask results:\n{child_block}"
    )


def parse_subtasks(raw_output: str) -> list[str]:
    """Best-effort extraction of a subtask list from a model response. Tries
    strict JSON first (including JSON wrapped in ```-fences, which models emit
    constantly even when told not to), then a numbered/bulleted list, then
    finally treats the whole response as one subtask -- so the tree never just
    dies because a model got chatty instead of returning JSON."""
    text = raw_output.strip()
    fenced = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    candidates = [fenced.group(1).strip()] if fenced else []
    candidates.append(text)
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        subtasks = data.get("subtasks") if isinstance(data, dict) else None
        if isinstance(subtasks, list) and subtasks:
            return [str(s) for s in subtasks]

    lines = [ln.strip(" -*\t") for ln in raw_output.splitlines()]
    bulleted = [ln for ln in lines if ln and (ln[0].isdigit() or ln.startswith(("-", "*")))]
    if bulleted:
        return [re.sub(r"^\d+[.)]\s*", "", ln) for ln in bulleted]

    return [raw_output.strip()] if raw_output.strip() else []
