"""Thin subprocess wrappers around the three vendor CLIs.

Each wrapper shells out to an *already installed, already logged-in* CLI --
`claude`, `codex`, or `agy` (Antigravity) -- using the subscription session
created by `claude login` / `codex` (ChatGPT sign-in) / `agy` (Google
sign-in). None of this talks to a pay-per-token API key; it rides the same
seat you already pay for.

Flags below are accurate as of the docs pulled while building this scaffold,
but all three CLIs ship updates fast. Anywhere you see `# VERIFY:` is worth
re-checking against `<cli> --help` before you lean on it for real work.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
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
        # The prompt goes in via stdin, never as an argv element, so a
        # model-generated task that starts with "-" can't be reparsed as a
        # flag (argument injection). Same pattern in every wrapper below.
        cmd = [
            "claude", "-p",
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
        return _run(cmd, self.timeout, result_key="result", cwd=self.workdir,
                    stdin_input=prompt)


class CodexAgent(Agent):
    """Wraps `codex exec` -- Codex CLI's non-interactive automation mode.

    Flags verified against codex-cli 0.142.5: `--full-auto` is gone from
    exec; sandboxing is `-s/--sandbox` (workspace-write confines writes to
    the call's cwd, which pairs with the per-call workdir isolation), and
    `--output-last-message` captures the final message cleanly -- stdout
    carries the whole transcript plus token accounting, so parsing stdout
    alone is noisy. (`schema` is Claude-only for now; JSON is requested in
    the prompt, and parse_subtasks degrades gracefully.)"""

    def run(self, prompt: str, schema: Optional[dict] = None) -> RunOutcome:
        if not shutil.which("codex"):
            return self._missing_binary("codex")
        fd, last_message_path = tempfile.mkstemp(prefix="codex-last-", suffix=".txt")
        os.close(fd)
        try:
            cmd = [
                "codex", "exec",
                "-m", self.spec.model,
                "-c", f'model_reasoning_effort="{self.spec.effort}"',
                "--sandbox", "workspace-write",  # unattended, writes confined to cwd
                "--skip-git-repo-check",  # scratch workdirs aren't git repos
                "--output-last-message", last_message_path,
                # no positional prompt -> codex reads it from stdin
            ]
            outcome = _run(cmd, self.timeout, result_key="result",
                           json_optional=True, cwd=self.workdir,
                           stdin_input=prompt)
            try:
                with open(last_message_path) as f:
                    last_message = f.read().strip()
                if last_message:
                    outcome.output = last_message
            except OSError:
                pass  # keep the (noisier) stdout we already captured
            return outcome
        finally:
            try:
                os.unlink(last_message_path)
            except OSError:
                pass


class AgyAgent(Agent):
    """Wraps `agy --print` -- the Antigravity CLI's headless mode, serving
    Gemini models off an Antigravity subscription.

    Flags verified against agy 1.0.14: the model string is exactly what
    `agy models` prints (e.g. "Gemini 3.1 Pro (High)"), with the reasoning
    level baked in -- so spec.effort documents intent rather than adding a
    flag. Output is plain text (no JSON output flag), so parsing is
    best-effort and `schema` is prompt-only here."""

    def run(self, prompt: str, schema: Optional[dict] = None) -> RunOutcome:
        if not shutil.which("agy"):
            return self._missing_binary("agy")
        cmd = [
            "agy", "--print",
            "--model", self.spec.model,
            "--print-timeout", f"{self.timeout}s",
            # unattended, confined to its own workdir
            "--dangerously-skip-permissions",
        ]
        return _run(cmd, self.timeout, result_key="response", json_optional=True,
                    cwd=self.workdir, stdin_input=prompt)


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
    stdin_input: Optional[str] = None,
) -> RunOutcome:
    # A workdir can be deleted out from under a run (a purged temp dir, a
    # removed worktree). Fail this call cleanly -- which routes into the
    # normal failover / branch-failure path -- rather than letting a vendor
    # CLI improvise a fallback directory and operate somewhere it shouldn't.
    if cwd is not None and not os.path.isdir(cwd):
        return RunOutcome(
            output="", success=False,
            error=f"working directory no longer exists: {cwd}",
        )
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
            input=stdin_input,
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
        "agy": AgyAgent,
    }
    return provider_map[spec.provider](
        spec, timeout=timeout, max_turns=max_turns, workdir=workdir
    )
