---
name: modelmesh
description: Delegate one genuinely big task (whole-repo bug-fix sweep, 100k+ LOC audit/report, work worth splitting across the Claude + ChatGPT + Antigravity seats) to the modelmesh hierarchical multi-model orchestrator CLI. Invoke ONLY on explicit /modelmesh, an explicit ask to "use modelmesh" / "orchestrate across models", or after the user approves your suggestion for an orchestrator-sized task. NEVER for ordinary questions, explanations, or small edits — handle those directly; every modelmesh run spawns a full tree of unattended agent calls on three paid subscriptions.
---

# modelmesh — hierarchical multi-model orchestration

`modelmesh` (the `modelmesh` binary on PATH; an editable install of GitHub
`7cubit/ai-orchestrator`) dispatches one task down a tier tree and
synthesizes results back up. The source checkout's location varies per
machine — never assume a path. If you need the source (maintenance only;
running tasks never requires it), resolve it from the install itself, e.g.:

```bash
grep -rho "/[^']*ai-orchestrator[^']*" \
  ~/.local/share/uv/tools/modelmesh/lib/python*/site-packages/__editable__*.py | head -1
```

```
ORCHESTRATOR  claude-opus-4-8 (dispatch + decompose)
              decision panel: claude-fable-5 + gpt-5.6-sol, both must agree
MAIN          claude-opus-4-8 | gpt-5.6-sol | grok-4.5
SECOND        claude-opus-4-8 | gpt-5.6-terra | agy "Gemini 3.1 Pro (High)"
              (engaged only for huge subtasks that need re-division)
CODING        claude-sonnet-5 | gpt-5.6-luna | agy "Gemini 3.5 Flash (High)"
```

It rides the user's existing `claude` / `codex` (ChatGPT) / `agy`
(Antigravity) / `grok` (grok.com) subscription logins. (A `kimi` wrapper
exists but is unseated by default — its quota exhausts too fast for
fan-out.) Routing is
strength/cost-aware (MODEL_PROFILES in config.py), depth is adaptive (small
subtasks skip straight to a coding agent), and a post-synthesis review gate
rejects hallucinated reports and re-dispatches with feedback.

## When to use — and when not

- USE: the user explicitly invokes it (/modelmesh <task>), or approves your
  suggestion for work that is genuinely orchestrator-sized: multi-hundred-file
  sweeps, full-repo audit reports, tasks that benefit from cross-checking
  Claude vs GPT vs Gemini vs Grok output.
- DO NOT use for anything you can do well yourself in this session — normal
  coding, questions, single-file fixes, small refactors. The user does not
  want the orchestrator invoked for everything.
- If a task seems borderline, do it yourself or ask one line: "This looks big
  enough for modelmesh (~N calls across your seats) — want me to fire
  it?"

## How to call it

Always through Bash, and for real runs ALWAYS in the background — a run
takes 10–30+ minutes. Pass `--verbose` so the log shows live per-call
progress instead of silence:

```bash
modelmesh "<one self-contained task description>" --verbose [flags]
```

Every run also writes its own durable progress log to
`~/.local/state/modelmesh/logs/run-<stamp>-<pid>.log` (path printed on
stderr at launch) regardless of --verbose — if the shell's output file is
lost (e.g. it lived in a purged session scratchpad), read that instead.

Clarify-first behavior: a pre-flight triage call may find the task
ambiguous. Backgrounded runs never block on questions — they adopt stated
default assumptions and print them to stderr as "proceeding under explicit
assumptions". Check the log for that block and ALWAYS relay adopted
assumptions to the user with the result. Better: resolve obvious
ambiguities yourself (ask the user one line if needed) BEFORE launching, so
the prompt you pass is already concrete — what "done" means, which files
are in scope, how to verify.

Working directory: like claude/codex/agy, the project defaults to the
current directory — agents run (and edit files) wherever the command is
launched. REQUIRE a clean working tree and a dedicated branch first (agents
run unattended with permissions bypassed).

Key flags:

- `--project <abs-path>` — target another repo without cd-ing there.
- `--isolated` — no repo at all: each agent gets a scratch dir (use for
  purely generative tasks so agents never touch the user's cwd).
- `--providers claude,codex,agy,grok` — restrict which seats are used.
- `--dry-run` — free structural preview (stub responses, no tokens).
- `--json` — machine-readable result tree (use when you need to parse).
- `--max-fanout N` (default 3), `--timeout S` (per call, default 600),
  `--run-timeout S` (whole-run budget, default 3600, 0 = off),
  `--max-retries N` (review-gate re-dispatches, default 1), `--no-review`,
  `--no-clarify` (skip the pre-flight ambiguity triage),
  `--prefer claude|codex|agy|grok` (lean on one seat's quota),
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
5. Code review gate (code runs into a repo that uses CodeRabbit — check for
   a `.coderabbit.yaml`): modelmesh's own review only checks the synthesized
   *text* for hallucination; it never reads the diff. So for real code
   changes, add an external code review before calling the task done:
   - Open a PR from the `modelmesh/<topic>` branch into the CodeRabbit base
     branch (`main` per `.coderabbit.yaml`); its auto-review fires on the PR.
     (Re-trigger on demand by commenting `@coderabbitai review`.)
   - Wait for CodeRabbit's review, then read its findings. Fold any real
     ones back in: fix directly if small, or fire a *targeted* follow-up
     `modelmesh` run scoped to just those findings (feed the exact
     file:line comments in as the task). Don't merge past unaddressed
     high-severity findings.
   - This complements the internal review (anti-hallucination) with actual
     diff-level code review; the two are not redundant.
6. Relay the final output plus the call tree (it shows which provider ran
   each subtask, which the user cares about for quota reasons), and note the
   CodeRabbit outcome (clean / findings addressed / findings outstanding).

## Cost shape (why restraint matters)

Route mode with full fan-out is up to ~9 coding calls + decompose/synth calls
at every level; ensemble mode multiplies leaves by 3. Provider concurrency is
capped at 1 per seat (PROVIDER_CONCURRENCY in config.py) to avoid tripping
subscription rate limits — that's why runs are slow and why firing modelmesh
casually burns quota the user is saving for real work.
