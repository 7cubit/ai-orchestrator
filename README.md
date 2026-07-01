# modelmesh

Hierarchical multi-model orchestration across **Claude Code**, **Codex CLI**,
and **Gemini CLI** — riding your existing subscriptions rather than API keys.

```
ORCHESTRATOR   claude-fable-5 (high)
      |
MAIN           claude-opus-4-8 (max)  |  gpt-5.5 (xhigh)  |  gemini-3.1-pro (high)
      |
SECOND         claude-opus-4-8 (high) |  gpt-5.5 (high)   |  gemini-3.1-pro (high)
      |
CODING         claude-sonnet-5 (max)  |  gpt-5.4 (xhigh)  |  gemini-3.5-flash (high)
```

A "big task" enters at ORCHESTRATOR. That agent breaks it into pieces for
MAIN; MAIN breaks its pieces down for SECOND; SECOND breaks its pieces down
for CODING, which actually writes things (no further decomposition). Results
synthesize back up the same path. See `modelmesh/config.py` to change any of
the model/effort assignments — nothing else needs to know.

## Strength-aware routing

In `--mode route`, subtasks aren't handed out blindly. Every decompose prompt
carries `MODEL_STRENGTHS` from `config.py` — a plain-English description of
what each provider is best at (Claude: architecture, multi-file refactoring,
agentic multi-step work; Codex: algorithmically tricky logic, debugging,
tests; Gemini: huge-context digestion, multimodal, fast broad sweeps) — and
the decomposing agent assigns each subtask a `provider` along with its
description. The dispatcher honors that assignment when the provider is
configured at the child tier, and falls back to round-robin when the model
didn't pick one (or picked something unavailable), so routing never becomes a
failure mode. The knowledge lives in config as editable text, not in code:
when your opinion of a model changes, edit the sentence, not the dispatcher.
ENSEMBLE mode ignores hints by design — it exists to call everyone.

## Why this is buildable at all

Claude Code, Codex CLI, and Gemini CLI all officially support running
headless off a personal subscription login instead of a metered API key:

- Claude: `claude login`, then `claude -p "..." --model ... --effort ...`
- Codex: sign in with your ChatGPT account on first run, then `codex exec ...`
- Gemini: sign in with your Google account, then `gemini -p "..." -m ...`

None of this reverse-engineers a web UI the way some "free API" tricks for
consumer chat products do — these three are purpose-built CLIs designed for
exactly this kind of scripted/agentic use. That said, "supported" isn't the
same as "unlimited": see **Rate limits** below before you turn this loose.

## Setup

1. Install and authenticate each CLI you plan to use:
   - `claude` — https://code.claude.com (run `claude login`)
   - `codex` — `npm install -g @openai/codex`, then run `codex` once to sign in
   - `gemini` — `npm install -g @google/gemini-cli`, then run `gemini` once to sign in
2. `pip install -e .` from this directory (or just run it in place — there
   are no third-party dependencies).
3. Try it with no CLIs installed at all first:

   ```bash
   python -m modelmesh "Build a REST API for a todo app with auth" --dry-run
   ```

   This exercises the full tree — decomposition, dispatch, synthesis — using
   canned responses, so you can see the shape of the thing before spending a
   single real token.

4. Once a CLI is authenticated, drop `--dry-run` for that path. You don't
   need all three working to start; agents.py reports a clear "not found /
   not authenticated" error per-provider rather than crashing the run.

## Rate limits are the real constraint here, not the code

The code above is the easy part — maybe a day to get right, plus ongoing
prompt-tuning on the decompose/synthesize steps, which never really "finishes."
The thing that will actually bite you is fan-out.

Run the bundled dry-run in `--mode ensemble` (call every provider at every
tier, not just one) and count the leaf calls it generates from a single
one-sentence task:

```bash
python -m modelmesh "Build a REST API" --dry-run --mode ensemble
```

That one task produces **27 separate coding-tier model calls** (3 providers
x 3 second-tier subtasks x 3 coding-tier subtasks), on top of the decompose
and synthesis calls at every level above it. `--mode route` (the default)
avoids this by picking one provider per node instead of all three, and
`MAX_FANOUT` in `config.py` caps how wide any single node can branch — but
even a disciplined tree that's 3-wide and 4 tiers deep is dozens of calls
per run, and Opus/GPT-5.5/Gemini 3.1 Pro at high-to-max effort are not fast
or cheap in tokens even when the seat itself is flat-rate.

Concretely, for Claude specifically: **1-3 agents at steady use is where a
Max subscription is cheapest; 5+ concurrent agents will hit rate limits
within hours**, at which point the usual pattern is to keep the subscription
for your primary/interactive path and fail over to `ANTHROPIC_API_KEY`
billing for burst capacity. `PROVIDER_CONCURRENCY` in `config.py` defaults
every provider to 1 concurrent call for exactly this reason — raise it only
after you've watched a real run and know where your account's ceiling is.
Codex (ChatGPT plan) and Gemini (5-hour/weekly usage windows) have the same
shape of limit even though the exact numbers differ.

**Practical starting point:** run in `--mode route` (one provider per node)
until the plumbing is solid, and only reach for `--mode ensemble` on the
handful of subtasks where cross-checking three models is actually worth the
3x spend — not as the default for every node in the tree.

## What's real vs. what needs verifying

Built and tested in this scaffold (dry-run passes end-to-end, including the
ensemble fan-out case above):

- Recursive dispatch through all 4 tiers, both `route` and `ensemble` modes
- Per-provider concurrency limiting and a hard fan-out cap
- Graceful "CLI not installed / not authenticated" errors instead of crashes
- JSON-first subtask parsing (fenced-JSON tolerant, schema-enforced on the
  Claude side via `--json-schema`) with a fallback so a chatty model reply
  doesn't kill the run
- Per-call isolated working directories, so unattended agents never share or
  trash the caller's cwd (`--workdir` pins the base; default is a fresh temp
  dir per run)
- Honest exit status: a CLI that exits non-zero or reports `is_error` is a
  failure even if it printed JSON, and child failures propagate to the root
  result instead of being papered over by a clean synthesis call

Marked `# VERIFY:` in `agents.py` — confirm against `<cli> --help` on your
installed versions before depending on these, since all three CLIs ship
updates weekly:

- Exact JSON-output flag for `codex exec` (used `--full-auto` + best-effort
  JSON-or-text parsing so it degrades safely either way)
- A per-call CLI flag for Gemini's thinking level (low/medium/high) — as of
  writing this looked like a `~/.gemini/settings.json` per-profile setting
  rather than a flag on `gemini` itself
- The exact Gemini 3.1 Pro model string for your account (`gemini-3.1-pro`
  vs. a `-preview` suffix) — the 3.1 rollout is still staged per-account, so
  run `gemini` then `/model` once to see what's actually available to you

Claude's side needs the least hand-holding: if you ask for an effort level a
given model doesn't support, Claude Code silently falls back to the closest
one it does — so the config's `"max"` requests are already safe on models
that top out lower.

## An alternative worth knowing about: let Claude Code orchestrate natively

This package treats all three CLIs symmetrically, as leaf subprocess calls
under one Python dispatcher. There's a second, lower-effort architecture: run
Fable 5 interactively (or via `claude -p`) as the actual orchestrator, give it
Bash access, and let its own native subagent system (the `Task` tool) handle
the Claude-side fan-out — with Python only stepping in at the points where a
Claude subagent needs to shell out to `codex` or `gemini`. Claude Code
already provides the agent loop, retries, and context isolation for the
Claude tiers for free; you'd only be writing the Codex/Gemini bridge, not the
whole tree. Worth prototyping alongside this if you want to compare effort
before committing to one.

## Files

- `modelmesh/tasks.py` — `Tier`, `Task`, `TaskResult`
- `modelmesh/config.py` — the tier -> model/effort map, `MODEL_STRENGTHS` routing knowledge, concurrency & fan-out limits
- `modelmesh/agents.py` — subprocess wrappers for `claude` / `codex` / `gemini`, plus the dry-run mock
- `modelmesh/prompts.py` — decompose/synthesize prompt templates + subtask parsing
- `modelmesh/orchestrator.py` — the recursive dispatcher
- `modelmesh/cli.py` — `python -m modelmesh "..."` entry point
