"""Prompt templates for decomposition (parent -> children) and synthesis
(children -> parent). Treat these as a first draft: prompt-tuning this layer
is the part of the project that never really finishes, not a one-time task.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

from .config import MODEL_STRENGTHS
from .tasks import Task, TaskResult, Tier

# Forces decomposition calls into strict JSON: the orchestrator passes this
# to every decompose call, and ClaudeCodeAgent forwards it via
# `claude --json-schema`. Codex/Gemini accept and ignore it (JSON is still
# requested in the prompt text below, and parse_subtasks degrades gracefully).
SUBTASK_SCHEMA = {
    "type": "object",
    "properties": {
        "subtasks": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "provider": {
                        "type": "string",
                        "enum": list(MODEL_STRENGTHS),
                    },
                },
                "required": ["task"],
            },
        }
    },
    "required": ["subtasks"],
}


@dataclass(frozen=True)
class ParsedSubtask:
    description: str
    provider: Optional[str] = None  # routing hint; None -> round-robin


def decompose_prompt(
    task: Task,
    lower_tier: Tier,
    max_fanout: int,
    providers: Optional[list[str]] = None,
) -> str:
    routing = ""
    if providers:
        strengths = "\n".join(
            f"- {p}: {MODEL_STRENGTHS[p]}" for p in providers if p in MODEL_STRENGTHS
        )
        routing = (
            f"For each subtask, also pick the provider best suited to execute "
            f"it, from [{', '.join(providers)}], using these strengths:\n"
            f"{strengths}\n\n"
        )
    return (
        f"You are coordinating a multi-agent build. Break the following task "
        f"into at most {max_fanout} concrete, independent subtasks suitable "
        f"for the '{lower_tier.value}' tier to execute. Each subtask should "
        f"be self-contained enough that an agent with no other context could "
        f"act on it directly.\n\n"
        f"{routing}"
        f"Task:\n{task.description}\n\n"
        f'Respond with ONLY JSON in this shape: '
        f'{{"subtasks": [{{"task": "...", "provider": "..."}}, ...]}}. '
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


def parse_subtasks(raw_output: str) -> list[ParsedSubtask]:
    """Best-effort extraction of a subtask list from a model response. Tries
    strict JSON first (including JSON wrapped in ```-fences, which models emit
    constantly even when told not to), then a numbered/bulleted list, then
    finally treats the whole response as one subtask -- so the tree never just
    dies because a model got chatty instead of returning JSON.

    JSON items may be objects ({"task": ..., "provider": ...}) or plain
    strings; anything without a usable provider routes by round-robin."""
    text = raw_output.strip()
    fenced = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    candidates = [fenced.group(1).strip()] if fenced else []
    candidates.append(text)
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        items = data.get("subtasks") if isinstance(data, dict) else None
        if not (isinstance(items, list) and items):
            continue
        parsed: list[ParsedSubtask] = []
        for item in items:
            if isinstance(item, dict):
                desc = str(item.get("task") or item.get("description") or "").strip()
                provider = item.get("provider")
                if desc:
                    parsed.append(ParsedSubtask(
                        desc, provider if isinstance(provider, str) else None
                    ))
            elif str(item).strip():
                parsed.append(ParsedSubtask(str(item).strip()))
        if parsed:
            return parsed

    lines = [ln.strip(" -*\t") for ln in raw_output.splitlines()]
    bulleted = [ln for ln in lines if ln and (ln[0].isdigit() or ln.startswith(("-", "*")))]
    if bulleted:
        return [ParsedSubtask(re.sub(r"^\d+[.)]\s*", "", ln)) for ln in bulleted]

    return [ParsedSubtask(text)] if text else []
