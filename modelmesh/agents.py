"""Thin subprocess wrappers around the three vendor CLIs.

Each wrapper shells out to an *already installed, already logged-in* CLI --
`claude`, `codex`, or `gemini` -- using the subscription session created by
`claude login` / `codex` (ChatGPT sign-in) / `gemini` (Google sign-in). None
of this talks to a pay-per-token API key; it rides the same seat you already
pay for.

Flags below are accurate as of the docs pulled while building this scaffold,
but all three CLIs ship updates fast. Anywhere you see `# VERIFY:` is worth
re-checking against `<cli> --help` before you lean on it for real work.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from .config import AgentSpec


@dataclass
class RunOutcome:
    output: str
    success: bool
    error: Optional[str] = None
    raw: Optional[dict] = None


class Agent(ABC):
    def __init__(
        self,
        spec: AgentSpec,
        *,
        timeout: int,
        max_turns: int,
        workdir: Optional[str] = None,
    ):
        self.spec = spec
        self.timeout = timeout
        self.max_turns = max_turns
        # Each call gets its own working directory so unattended agents
        # (bypassPermissions / --full-auto) can't stomp each other's files
        # when children run in parallel.
        self.workdir = workdir

    @abstractmethod
    def run(self, prompt: str, schema: Optional[dict] = None) -> RunOutcome: ...

    def _missing_binary(self, binary: str) -> RunOutcome:
        return RunOutcome(
            output="",
            success=False,
            error=(
                f"`{binary}` not found on PATH. Install and authenticate it "
                f"(see README) before running this tier for real, or pass "
                f"--dry-run to exercise the tree without it."
            ),
        )


class ClaudeCodeAgent(Agent):
    """Wraps `claude -p` -- Claude Code's headless mode."""

    def run(self, prompt: str, schema: Optional[dict] = None) -> RunOutcome:
        if not shutil.which("claude"):
            return self._missing_binary("claude")
        cmd = [
            "claude", "-p", prompt,
            "--model", self.spec.model,
            # Claude Code falls back to the closest supported level if the
            # active model doesn't support the exact one requested, so it's
            # safe to ask for e.g. "max" even on a model that caps at "high".
            "--effort", self.spec.effort,
            "--output-format", "json",
            "--max-turns", str(self.max_turns),
            # Unattended, but each call is confined to its own workdir.
            "--permission-mode", "bypassPermissions",
        ]
        if schema is not None:
            cmd += ["--json-schema", json.dumps(schema)]
        return _run(cmd, self.timeout, result_key="result", cwd=self.workdir)


class CodexAgent(Agent):
    """Wraps `codex exec` -- Codex CLI's non-interactive automation mode."""

    def run(self, prompt: str, schema: Optional[dict] = None) -> RunOutcome:
        if not shutil.which("codex"):
            return self._missing_binary("codex")
        cmd = [
            "codex", "exec",
            "-m", self.spec.model,
            "-c", f'model_reasoning_effort="{self.spec.effort}"',
            "--full-auto",  # unattended, confined to its own workdir
            prompt,
        ]
        # VERIFY: confirm the current JSON-output flag for `codex exec` on your
        # installed version (`codex exec --help`). json_optional=True means we
        # parse plain stdout as a fallback, so this degrades safely either way.
        # (`schema` is Claude-only for now; JSON is requested in the prompt.)
        return _run(cmd, self.timeout, result_key="result", json_optional=True,
                    cwd=self.workdir)


class GeminiAgent(Agent):
    """Wraps `gemini -p` -- Gemini CLI's headless mode."""

    def run(self, prompt: str, schema: Optional[dict] = None) -> RunOutcome:
        if not shutil.which("gemini"):
            return self._missing_binary("gemini")
        cmd = [
            "gemini", "-p", prompt,
            "-m", self.spec.model,
            "--output-format", "json",
        ]
        # VERIFY: there's no confirmed per-call CLI flag for thinking level
        # (low/medium/high) as of this writing. Until one is, "effort" on this
        # tier documents intent more than it enforces it -- set thinking level
        # via a per-profile ~/.gemini/settings.json if you need it locked in.
        # (`schema` is Claude-only for now; JSON is requested in the prompt.)
        return _run(cmd, self.timeout, result_key="response", cwd=self.workdir)


class MockAgent(Agent):
    """Stand-in used for --dry-run. Returns instantly so you can exercise the
    whole tree -- fan-out, aggregation, error paths -- with no installed
    CLIs, no auth, and no tokens spent."""

    def run(self, prompt: str, schema: Optional[dict] = None) -> RunOutcome:
        stub = (
            f"[dry-run stub from {self.spec.provider}:{self.spec.model} "
            f"@ {self.spec.effort}] Would have processed:\n{prompt[:200]}"
        )
        return RunOutcome(output=stub, success=True, raw={"dry_run": True})


def _run(
    cmd: list[str],
    timeout: int,
    *,
    result_key: str,
    json_optional: bool = False,
    cwd: Optional[str] = None,
) -> RunOutcome:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd
        )
    except subprocess.TimeoutExpired:
        return RunOutcome(output="", success=False, error=f"timed out after {timeout}s")
    except OSError as exc:
        return RunOutcome(output="", success=False, error=str(exc))

    stdout = proc.stdout.strip()
    if proc.returncode != 0 and not stdout:
        return RunOutcome(
            output="", success=False,
            error=proc.stderr.strip() or "non-zero exit, no output",
        )

    try:
        data = json.loads(stdout)
        text = data.get(result_key, stdout)
        if not isinstance(text, str):
            # e.g. structured output from --json-schema arrives as an object
            text = json.dumps(text)
        # A CLI can exit non-zero -- or flag the failure in-band, the way
        # Claude Code sets "is_error" -- while still printing well-formed
        # JSON. That's a failure, not a result.
        ok = proc.returncode == 0 and not data.get("is_error", False)
        return RunOutcome(
            output=text,
            success=ok,
            error=None if ok else (
                proc.stderr.strip() or f"CLI reported failure (exit {proc.returncode})"
            ),
            raw=data,
        )
    except json.JSONDecodeError:
        if json_optional:
            return RunOutcome(output=stdout, success=proc.returncode == 0)
        return RunOutcome(
            output=stdout, success=proc.returncode == 0,
            error=None if proc.returncode == 0 else proc.stderr.strip(),
        )


def build_agent(
    spec: AgentSpec,
    *,
    dry_run: bool,
    timeout: int,
    max_turns: int,
    workdir: Optional[str] = None,
) -> Agent:
    if dry_run:
        return MockAgent(spec, timeout=timeout, max_turns=max_turns, workdir=workdir)
    provider_map = {
        "claude": ClaudeCodeAgent,
        "codex": CodexAgent,
        "gemini": GeminiAgent,
    }
    return provider_map[spec.provider](
        spec, timeout=timeout, max_turns=max_turns, workdir=workdir
    )
