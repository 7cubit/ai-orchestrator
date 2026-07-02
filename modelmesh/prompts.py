"""Prompt templates for decomposition (parent -> children) and synthesis
(children -> parent). Treat these as a first draft: prompt-tuning this layer
is the part of the project that never really finishes, not a one-time task.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

from .config import MODEL_PROFILES
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
                        "enum": list(MODEL_PROFILES),
                    },
                    "needs_decomposition": {"type": "boolean"},
                },
                "required": ["task", "provider", "needs_decomposition"],
            },
        }
    },
    "required": ["subtasks"],
}

# Verdict schema for the orchestrator's post-synthesis quality review.
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["accept", "retry"]},
        "issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict"],
}


@dataclass(frozen=True)
class ParsedSubtask:
    description: str
    provider: Optional[str] = None  # routing hint; None -> round-robin
    # True -> break down at the next tier before coding; False -> hand
    # straight to a coding-tier agent (the cheap path). Defaults to True so a
    # reply that omits it behaves like the pre-adaptive-depth pipeline.
    needs_decomposition: bool = True


def decompose_prompt(
    task: Task,
    lower_tier: Tier,
    max_fanout: int,
    providers: Optional[list[str]] = None,
) -> str:
    routing = ""
    if providers:
        profiles = "\n".join(
            f"- {p}: strengths: {MODEL_PROFILES[p].strengths}. "
            f"weaknesses: {MODEL_PROFILES[p].weaknesses}. "
            f"cost: {MODEL_PROFILES[p].cost}."
            for p in providers
            if p in MODEL_PROFILES
        )
        routing = (
            f"For each subtask, pick the provider best suited to execute it, "
            f"from [{', '.join(providers)}]. Route toward strengths, away "
            f"from weaknesses, and when two providers would do equally well, "
            f"pick the cheaper one:\n{profiles}\n\n"
            f"For each subtask also set \"needs_decomposition\": false if a "
            f"single coding-tier agent can complete it in one focused "
            f"session -- this is the cheap path and skips straight to the "
            f"coding tier -- or true only if it genuinely spans many files, "
            f"components, or steps (e.g. a bug-fix or audit across a "
            f"100k+-line codebase) and must be broken down again at the "
            f"'{lower_tier.value}' tier first. Every extra level of "
            f"decomposition multiplies calls and cost, so pay for depth only "
            f"where the size of the work demands it.\n\n"
        )
    return (
        f"You are coordinating a multi-agent build whose goals are quality "
        f"first, cost second. Break the following task into at most "
        f"{max_fanout} concrete, independent subtasks. Each subtask should "
        f"be self-contained enough that an agent with no other context could "
        f"act on it directly.\n\n"
        f"{routing}"
        f"Task:\n{task.description}\n\n"
        f'Respond with ONLY JSON in this shape: {{"subtasks": [{{"task": '
        f'"...", "provider": "...", "needs_decomposition": true}}, ...]}}. '
        f"No prose before or after the JSON."
    )


def review_prompt(original_task: str, integrated_output: str) -> str:
    return (
        f"You are the orchestrator reviewing work you delegated to a tree of "
        f"agents. Judge whether the integrated result below actually fulfills "
        f"the original task at a quality you would ship.\n\n"
        f"Be specifically skeptical of hallucination: files, functions, line "
        f"numbers, metrics, benchmark figures, or findings that are asserted "
        f"but could not have been verified from the work actually done. An "
        f"audit or report that sounds authoritative but invents specifics is "
        f"a 'retry', not an 'accept'. Also flag unresolved contradictions "
        f"between subtask results and silent gaps in coverage.\n\n"
        f"Original task:\n{original_task}\n\n"
        f"Integrated result:\n{integrated_output}\n\n"
        f'Respond with ONLY JSON: {{"verdict": "accept"}} if it clears the '
        f'bar, or {{"verdict": "retry", "issues": ["...", "..."]}} with '
        f"concrete, actionable issues if it does not. No prose before or "
        f"after the JSON."
    )


def retry_task_description(original_task: str, issues: list[str]) -> str:
    issue_block = "\n".join(f"- {issue}" for issue in issues) or "- quality below the bar"
    return (
        f"{original_task}\n\n"
        f"A previous attempt at this task was rejected by review. Do not "
        f"repeat these failures -- address every one of them, and do not "
        f"assert anything you cannot support from work actually performed:\n"
        f"{issue_block}"
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

    JSON items may be objects ({"task": ..., "provider": ...,
    "needs_decomposition": ...}) or plain strings; anything without a usable
    provider routes by round-robin, and a missing needs_decomposition
    defaults to True (the deep path)."""
    for data in _json_candidates(raw_output):
        items = data.get("subtasks") if isinstance(data, dict) else None
        if not (isinstance(items, list) and items):
            continue
        parsed: list[ParsedSubtask] = []
        for item in items:
            if isinstance(item, dict):
                desc = str(item.get("task") or item.get("description") or "").strip()
                provider = item.get("provider")
                needs = item.get("needs_decomposition")
                if desc:
                    parsed.append(ParsedSubtask(
                        desc,
                        provider if isinstance(provider, str) else None,
                        needs if isinstance(needs, bool) else True,
                    ))
            elif str(item).strip():
                parsed.append(ParsedSubtask(str(item).strip()))
        if parsed:
            return parsed
    text = raw_output.strip()

    lines = [ln.strip(" -*\t") for ln in raw_output.splitlines()]
    bulleted = [ln for ln in lines if ln and (ln[0].isdigit() or ln.startswith(("-", "*")))]
    if bulleted:
        return [ParsedSubtask(re.sub(r"^\d+[.)]\s*", "", ln)) for ln in bulleted]

    return [ParsedSubtask(text)] if text else []


def parse_review(raw_output: str) -> Optional[dict]:
    """Extract the reviewer's verdict. Returns {"verdict": ..., "issues": [...]}
    or None when no verdict can be found -- callers should treat None as
    'accept' so a chatty reviewer can't wedge the run into endless retries."""
    for data in _json_candidates(raw_output):
        if isinstance(data, dict) and data.get("verdict") in ("accept", "retry"):
            issues = data.get("issues")
            return {
                "verdict": data["verdict"],
                "issues": [str(i) for i in issues] if isinstance(issues, list) else [],
            }
    return None


def _json_candidates(raw_output: str):
    """Yield parsed JSON from the response text, trying a ```-fenced block
    first (models emit fences constantly even when told not to), then the
    whole response."""
    text = raw_output.strip()
    fenced = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    candidates = [fenced.group(1).strip()] if fenced else []
    candidates.append(text)
    for candidate in candidates:
        try:
            yield json.loads(candidate)
        except json.JSONDecodeError:
            continue
