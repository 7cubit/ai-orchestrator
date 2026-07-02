---
name: modelmesh
description: Delegate one genuinely big task (whole-repo bug-fix sweep, 100k+ LOC audit/report, work worth splitting across the Claude + ChatGPT + Antigravity seats) to the modelmesh hierarchical multi-model orchestrator CLI. Invoke ONLY on explicit /modelmesh, an explicit ask to "use modelmesh" / "orchestrate across models", or after the user approves your suggestion for an orchestrator-sized task. NEVER for ordinary questions, explanations, or small edits — handle those directly; every modelmesh run spawns a full tree of unattended agent calls on three paid subscriptions.
---

# modelmesh — hierarchical multi-model orchestration

`modelmesh` (binary: `~/.local/bin/modelmesh`; source: `~/Downloads/ai-orchestrator`,
editable install, GitHub `7cubit/ai-orchestrator`) dispatches one task down a
tier tree and synthesizes results back up:

```
ORCHESTRATOR  claude-fable-5
MAIN          claude-opus-4-8 | gpt-5.5 | agy "Gemini 3.1 Pro (High)"
SECOND        (engaged only for huge subtasks that need re-division)
CODING        claude-sonnet-5 | gpt-5.4 | agy "Gemini 3.5 Flash (High)"
```

It rides the user's existing `claude` / `codex` (ChatGPT) / `agy` (Antigravity)
subscription logins — all three are authenticated on this machine. Routing is
strength/cost-aware (MODEL_PROFILES in config.py), depth is adaptive (small
subtasks skip straight to a coding agent), and a post-synthesis review gate
rejects hallucinated reports and re-dispatches with feedback.

## When to use — and when not

- USE: the user explicitly invokes it (/modelmesh <task>), or approves your
  suggestion for work that is genuinely orchestrator-sized: multi-hundred-file
  sweeps, full-repo audit reports, tasks that benefit from cross-checking
  Claude vs GPT vs Gemini output.
- DO NOT use for anything you can do well yourself in this session — normal
  coding, questions, single-file fixes, small refactors. The user does not
  want the orchestrator invoked for everything.
- If a task seems borderline, do it yourself or ask one line: "This looks big
  enough for modelmesh (~N calls across your three seats) — want me to fire
  it?"

## How to call it

Always through Bash, and for real runs ALWAYS in the background — a run takes
10–30+ minutes and prints nothing until the tree finishes:

```bash
modelmesh "<one self-contained task description>" [flags]
```

Working directory: like claude/codex/agy, the project defaults to the
current directory — agents run (and edit files) wherever the command is
launched. REQUIRE a clean working tree and a dedicated branch first (agents
run unattended with permissions bypassed).

Key flags:

- `--project <abs-path>` — target another repo without cd-ing there.
- `--isolated` — no repo at all: each agent gets a scratch dir (use for
  purely generative tasks so agents never touch the user's cwd).
- `--providers claude,codex,agy` — restrict which seats are used.
- `--dry-run` — free structural preview (stub responses, no tokens).
- `--json` — machine-readable result tree (use when you need to parse).
- `--max-fanout N` (default 3), `--timeout S` (per call, default 600),
  `--max-retries N` (review-gate re-dispatches, default 1), `--no-review`,
  `--mode ensemble` (all providers per node — 3x cost, only when the user
  wants cross-model verification).
- Bare `modelmesh` opens a human REPL — never use that form from a session.

## Playbook

1. Confirm the task is orchestrator-sized (see above); otherwise just do it.
2. Repo work: `git status` must be clean; create a branch
   (`modelmesh/<topic>`); launch from inside the repo (or pass `--project`).
   For non-repo generative work, pass `--isolated`.
3. Launch in background via Bash `run_in_background`, note the output file,
   and keep working; read the result when it completes.
4. Verify before relaying: run the tests yourself for code changes; for
   reports, spot-check at least two cited file:line claims. A non-zero exit
   means the review gate rejected the result — report that honestly.
5. Relay the final output plus the call tree (it shows which provider ran
   each subtask, which the user cares about for quota reasons).

## Cost shape (why restraint matters)

Route mode with full fan-out is up to ~9 coding calls + decompose/synth calls
at every level; ensemble mode multiplies leaves by 3. Provider concurrency is
capped at 1 per seat (PROVIDER_CONCURRENCY in config.py) to avoid tripping
subscription rate limits — that's why runs are slow and why firing modelmesh
casually burns quota the user is saving for real work.
