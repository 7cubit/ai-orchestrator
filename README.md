# modelmesh

Hierarchical multi-model orchestration across **Claude Code**, **Codex CLI**,
and **Antigravity (`agy`)** — riding your existing subscriptions rather than
API keys.

```
ORCHESTRATOR   claude-fable-5 (high)
      |
MAIN           claude-opus-4-8 (max)  |  gpt-5.5 (xhigh)  |  agy: Gemini 3.1 Pro (High)
      |
SECOND         claude-opus-4-8 (high) |  gpt-5.5 (high)   |  agy: Gemini 3.1 Pro (High)
      |
CODING         claude-sonnet-5 (max)  |  gpt-5.4 (xhigh)  |  agy: Gemini 3.5 Flash (High)
```

A "big task" enters at ORCHESTRATOR and results synthesize back up the same
path it was divided along. How deep the division goes is decided per subtask,
not fixed by the diagram — see **Adaptive depth** below. See
`modelmesh/config.py` to change any of the model/effort assignments — nothing
else needs to know.

## Strength-aware routing

In `--mode route`, subtasks aren't handed out blindly. Every decompose prompt
carries `MODEL_PROFILES` from `config.py` — a plain-English,
benchmark-informed description of each provider's strengths, weaknesses, and
cost (Claude: architecture, multi-file refactoring, agentic multi-step work;
Codex: algorithmically tricky logic, debugging, tests; agy — Gemini via
Antigravity: huge-context digestion, broad sweeps, and the cheapest coding
tier in Flash) — and the
decomposing agent assigns each subtask a `provider`, told to route toward
strengths, away from weaknesses, and to the cheaper option when quality would
be equal. The dispatcher honors that assignment when the provider is
configured at the child tier, and falls back to round-robin when the model
didn't pick one (or picked something unavailable), so routing never becomes a
failure mode. The knowledge lives in config as editable text, not in code:
when the benchmarks move, edit the sentences, not the dispatcher. ENSEMBLE
mode ignores hints by design — it exists to call everyone.

## Leaning on one subscription's quota (`--prefer`)

By default routing is strength/cost-aware, so a provider is only used when the
work fits its profile — a summarize task goes to Gemini, a tricky-logic task
to Codex, and so on. If one of your seats has abundant quota you'd rather
spend (say a ChatGPT Plus plan with tokens to burn), `--prefer <provider>`
biases routing toward it wherever it's configured at a tier:

```bash
modelmesh "Refactor the billing module and add tests" --prefer codex
```

- `--prefer codex` routes MAIN, SECOND, and CODING to the ChatGPT models
  (gpt-5.5 at the planning tiers, gpt-5.4 at coding), overriding the
  per-subtask strength routing. The ORCHESTRATOR tier is Fable-5-only, so it
  is unaffected.
- **Failover still applies:** if the preferred provider times out or errors,
  the call falls over to the other providers as usual — so `--prefer` leans
  on a seat without becoming a single point of failure.
- Note which model is which tier: gpt-**5.5** only appears at MAIN/SECOND;
  the coding tier (most of the volume) is gpt-**5.4**. `--prefer codex`
  exercises both.

## Adaptive depth (why SECOND exists but isn't always used)

Depth costs money: every extra tier adds a decompose call, a synthesis call,
and up to `MAX_FANOUT`x more leaves. So each decompose call also marks every
subtask `needs_decomposition: true|false`:

- **false** — one coding-tier agent can finish it in a focused session. The
  subtask skips every intermediate tier and goes straight to a coding agent
  (Sonnet 5 / GPT-5.4 / Gemini 3.5 Flash). A small bug-fix can legitimately
  run ORCHESTRATOR -> CODING with nothing in between.
- **true** — the work genuinely spans many files, components, or steps (a
  bug-fix pass or audit over a 200k–500k-line codebase). It drops one tier,
  where it gets divided again across multiple cheaper agents — this is what
  the SECOND tier is *for*.

So "fix the bugs in this project" on a big repo naturally engages all four
tiers, while "rename this flag" costs three calls total. The default is the
deep path when a reply omits the field, so ambiguity degrades toward quality,
not toward cheap.

## Failover (a timed-out agent hands off, it doesn't sink the branch)

In `route` mode, every node call has a provider failover chain. If the chosen
provider times out, its CLI is missing, or it exits with an error, the *same*
subtask is retried on another provider at that tier instead of failing the
branch. The fallbacks are ordered **fastest-first** — each model carries a
`speed` rating in `config.py` (`fast`/`medium`/`slow`), and since a timeout
usually means "too slow to finish in the window," the next attempt uses a
quicker model (e.g. a stalled Opus coding call fails over to Gemini Flash).

It's bounded by the number of providers at the tier, so a genuinely broken
task can't fan out forever — once everyone has failed, the branch fails with
the full chain recorded (`all providers failed -> claude(timed out...);
codex(...)`), and a recovered call is tagged `failed over from ...` in the
tree. The ORCHESTRATOR tier has only one provider (Fable 5), so it has no
failover — keep its per-call `--timeout` generous. ENSEMBLE mode doesn't fail
over because it already calls every provider.

## Quality review (anti-hallucination loop)

After the tree synthesizes, the ORCHESTRATOR model reviews the integrated
result against the original task with a skeptic's prompt: findings, files,
metrics, or benchmark figures that the work couldn't actually have verified
are grounds for rejection — an audit report that *sounds* authoritative but
invents specifics is a `retry`, not an `accept`. On `retry`, the whole tree
re-dispatches with the reviewer's concrete issues folded into the task
("do not repeat these failures"), up to `--max-retries` times (default 1;
`MAX_QUALITY_RETRIES` in config). If the reviewer still rejects after the
last retry, the run reports failure with the issues rather than shipping
flagged content. `--no-review` skips the loop; an unparseable or failed
review call accepts-with-a-note instead of wedging the run into retries.

## Why this is buildable at all

Claude Code, Codex CLI, and the Antigravity CLI all officially support
running headless off a personal subscription login instead of a metered API
key:

- Claude: `claude login`, then `claude -p "..." --model ... --effort ...`
- Codex: sign in with your ChatGPT account on first run, then `codex exec ...`
- Antigravity: sign in with your Google account, then
  `agy -p "..." --model "Gemini 3.1 Pro (High)"` — the reasoning level is
  part of the model string, exactly as `agy models` lists it

None of this reverse-engineers a web UI the way some "free API" tricks for
consumer chat products do — these three are purpose-built CLIs designed for
exactly this kind of scripted/agentic use. That said, "supported" isn't the
same as "unlimited": see **Rate limits** below before you turn this loose.

## Setup (any machine)

Everything needed lives in this repo; per-machine state is just the CLI
logins.

```bash
git clone https://github.com/7cubit/ai-orchestrator.git
cd ai-orchestrator
uv tool install --editable .        # -> `modelmesh` on PATH
# no uv? curl -LsSf https://astral.sh/uv/install.sh | sh   (or use pipx)

# let Claude Code drive it via /modelmesh:
mkdir -p ~/.claude/skills/modelmesh
cp claude-skill/SKILL.md ~/.claude/skills/modelmesh/SKILL.md
```

1. Install and authenticate each CLI you plan to use:
   - `claude` — https://code.claude.com (run `claude login`)
   - `codex` — `npm install -g @openai/codex`, then run `codex` once to sign in
   - `agy` — the Antigravity CLI; run `agy` once to sign in with your Google
     account, then `agy models` to see which models your seat serves
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

## Using it from Claude Code (`/modelmesh`)

Once the skill is installed (see Setup), you don't run the CLI by hand — you
drive it from a Claude Code session. Open `claude` inside the repo you want to
work on and invoke the skill with your task:

```
/modelmesh fix the failing tests --prefer codex
```

Claude *interprets* that line (it isn't a raw shell pass-through), so:

- The task text and any flags (`--prefer codex`, `--mode ensemble`, …) are
  understood and passed through to the real `modelmesh` command.
- Plain English works too — `/modelmesh fix the failing tests, use my GPT
  quota` gets the same `--prefer codex`.
- Claude adds the safe scaffolding for you: it runs in the repo you're in,
  branches first when the task writes files, launches in the background,
  verifies the result, and reports back — so you don't have to remember
  `--project`, `--timeout`, or to check the diff.

The skill only fires for genuinely orchestrator-sized work; ordinary
questions and small edits are answered by the Claude session directly, not
by spinning up the whole tree.

## Working directory: it behaves like claude/codex/agy

The project is wherever you're standing. Open a terminal inside a repo (or a
VS Code terminal, which drops you there already) and every agent call runs
with that directory as its cwd — same muscle memory as `claude`, `codex`,
and `agy`:

```bash
cd ~/code/myapp && git checkout -b modelmesh/bugfix
modelmesh "Fix the failing auth tests and any bugs you find on the way"

modelmesh "Produce a security audit report of this codebase" --max-retries 2
```

- Coding-tier agents edit real files, so work on a branch and review the
  diff afterwards — these agents run unattended with permissions bypassed.
  Avoid `--parallel-children` here (agents share one working tree); the
  default is sequential anyway.
- `--project <path>` targets another repo without cd-ing there.
- `--isolated` opts out entirely: every agent gets its own scratch directory
  and your cwd is never touched — the right mode for purely generative tasks
  ("draft a design doc") run from somewhere that isn't a project.
- `--providers` restricts the run to the CLIs you actually have installed
  and authenticated, without editing `TIER_CONFIG`.

## Prompt ideas

The prompt is the whole interface, so shape it like a work order: say what
"done" means, name the evidence you want (files, lines, passing tests), and
put constraints in the prompt itself — subtasks inherit them. All of these
work identically as `/modelmesh <prompt>` inside a Claude Code session.

Bug-fix sweep (run from inside the repo, on a branch):

```bash
modelmesh "Find and fix every failing test in this repo. Do not modify the
tests themselves. For each fix, state the root cause in one sentence. Run
the full test suite at the end and report the pass count."
```

Security audit (the review gate rejects invented findings):

```bash
modelmesh "Produce a security audit of this codebase. Cite file and line for
every finding, rate each Critical/High/Medium/Low, and propose a concrete
fix per finding. Do not report anything you cannot point to in the code." \
    --max-retries 2 --json > audit.json
```

Big-codebase comprehension (plays to the cheap huge-context tier):

```bash
modelmesh "Map this repo's architecture: entry points, data flow, external
services, and the five files a new engineer must read first. Cite paths."
```

Cross-model design review (ensemble = every provider answers, then the tree
reconciles disagreements — worth 3x the spend only on decisions):

```bash
modelmesh "Design a multi-tenant rate limiter for an SMTP API: token bucket
vs sliding window, storage choice, and failure behavior under Redis loss.
State trade-offs explicitly." --mode ensemble --isolated
```

Mechanical migration (large but shallow — say so, and the decomposers will
fan it out to cheap coding agents instead of over-planning):

```bash
modelmesh "Rename the config key 'smtp_host' to 'relay_host' across this
entire repo -- code, tests, docs, and example configs. This is mechanical:
no redesign, keep every change minimal."
```

Report/document generation (no repo needed):

```bash
modelmesh "Write a runbook for recovering a Postgres primary from pgBackRest
when the WAL archive is 6 hours behind: preconditions, exact commands,
verification steps, and rollback." --isolated
```

Anti-patterns: don't send one-liners ("what does this function do?") — a
single `claude -p` call answers those for a fraction of the cost; don't
bundle unrelated asks in one run (each gets shallower attention than it
would alone); and don't leave success undefined ("make it better") — the
review gate can only reject what the prompt lets it measure.

## Security model & known limitations

modelmesh runs vendor CLIs unattended with their permission prompts disabled
(`claude --permission-mode bypassPermissions`, `agy
--dangerously-skip-permissions`; only `codex --sandbox workspace-write` is
OS-enforced, and only for writes). A self-audit
(`docs/SECURITY_AUDIT.md`) documents the consequences; the load-bearing ones:

- **The per-call working directory is a starting cwd, not a sandbox.** With
  permissions bypassed, `claude` and `agy` agents can read/write outside it.
  Treat every run as "this could touch anything the invoking user can" —
  run against a branch, review the diff, and don't point `--project` at a
  tree you can't afford an agent to edit.
- **Don't point `--project` at modelmesh's own source.** It's an editable
  install, so the running code *is* the checkout; an agent editing it plants
  changes that run on the next invocation.
- **Prompts now go to the CLIs via stdin, not argv**, so a model-generated
  subtask beginning with `-` can no longer be reparsed as a flag (the former
  argument-injection gap). Repo/LLM text still flows between agent prompts
  unsanitized — a known tradeoff of the decompose→synthesize design.
- **Run repo-mutating tasks from a durable directory, not a temp path.** A
  workdir that gets purged mid-run (session scratchpads, `/tmp`) leaves a
  permission-bypassed agent stranded — and one such agent once fell back
  into the operator's main checkout and committed there. Three guardrails
  now target this: a vanished workdir fails the call cleanly into the
  failover path instead of reaching the vendor CLI; every coding-tier
  prompt carries a stay-in-your-workdir constraint; and `--project` under a
  temp/scratchpad path is refused unless you pass `--allow-temp-project`.
  For repo runs, a dedicated git worktree is the right durable home.

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
Codex (ChatGPT plan) and Antigravity (rolling usage windows on the Google
seat) have the same shape of limit even though the exact numbers differ.

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

All three wrappers are now verified against live installed CLIs (flags drift
as these ship weekly — re-check `<cli> --help` after updates):

The `codex` side is verified against codex-cli 0.142.5: `--full-auto` is
gone from `codex exec`; the wrapper uses `--sandbox workspace-write` and
captures the final message via `--output-last-message` (stdout carries the
whole transcript plus token accounting). `gpt-5.5` confirmed live on a
ChatGPT seat.

The `agy` side is verified against agy 1.0.14: `--print`, `--model` with the
display-name string from `agy models` (reasoning level included, e.g.
"Gemini 3.1 Pro (High)"), `--print-timeout`, and
`--dangerously-skip-permissions` for unattended runs. Output is plain text —
there's no JSON flag — so parsing on that path is best-effort by design.
Re-run `agy models` after updates; the model list is what moves.

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
Claude subagent needs to shell out to `codex` or `agy`. Claude Code
already provides the agent loop, retries, and context isolation for the
Claude tiers for free; you'd only be writing the Codex/Antigravity bridge, not the
whole tree. Worth prototyping alongside this if you want to compare effort
before committing to one.

## Files

- `modelmesh/tasks.py` — `Tier`, `Task`, `TaskResult`
- `modelmesh/config.py` — the tier -> model/effort map, `MODEL_PROFILES` routing knowledge, concurrency & fan-out limits, review retry cap
- `modelmesh/agents.py` — subprocess wrappers for `claude` / `codex` / `agy`, plus the dry-run mock
- `modelmesh/prompts.py` — decompose/synthesize prompt templates + subtask parsing
- `modelmesh/orchestrator.py` — the recursive dispatcher
- `modelmesh/cli.py` — `python -m modelmesh "..."` entry point
