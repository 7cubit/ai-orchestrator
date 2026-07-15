# modelmesh

Hierarchical multi-model orchestration across **Claude Code**, **Codex CLI**,
**Antigravity (`agy`)**, and **Kimi Code (`kimi`)** — riding your existing
subscriptions rather than API keys.

```
ORCHESTRATOR   claude-opus-4-8 (max)                                    <- dispatch + root decompose
      |
      |        DECISION PANEL: claude-fable-5 (xhigh) + gpt-5.6-sol (xhigh)
      |        both must agree to proceed; either one can force ask/retry
      |
MAIN           claude-opus-4-8 (max)  |  gpt-5.6-sol (max)   |  kimi: K2.7 Code (high)
      |
SECOND         claude-opus-4-8 (high) |  gpt-5.6-terra (high)|  agy: Gemini 3.1 Pro (High)
      |
CODING         claude-sonnet-5 (max)  |  gpt-5.6-luna (high) |  agy: Gemini 3.5 Flash (High)
```

The two **verdict** gates — the pre-flight ambiguity triage and the
post-synthesis quality review — are not decided by a single model. Both go to
a cross-vendor panel, and unanimity is required to *proceed*: if either voter
calls the task ambiguous you get asked, and if either votes retry the tree is
re-dispatched. Disagreement resolves to the stricter branch, so a split never
needs a tie-breaker — only to be obeyed. A voter that errors or answers
unparseably abstains rather than vetoing, so a dead CLI can't wedge a run.
The panel sits on two different providers, so its calls run concurrently and
cost roughly one call of wall-clock rather than two.

The root **decompose** is deliberately *not* voted on: splitting a task into
a tree is a plan, not a verdict, and two models can't "agree" on one without a
reconciliation pass — which is what ENSEMBLE mode already does, at ENSEMBLE
prices. Opus 4.8 owns that alone.

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
  (gpt-5.6-sol/terra at the planning tiers, gpt-5.6-luna at coding), overriding the
  per-subtask strength routing. `--prefer kimi` leans on `kimi-code/kimi-for-coding`
  (K2.7 Code) wherever it's configured, which is the MAIN tier. The
  ORCHESTRATOR tier is Opus-4.8-only, so it is unaffected.
- **Failover still applies:** if the preferred provider times out or errors,
  the call falls over to the other providers as usual — so `--prefer` leans
  on a seat without becoming a single point of failure.
- Note which model is which tier: gpt-**5.5** only appears at MAIN/SECOND;
  the coding tier (most of the volume) is gpt-**5.4**. `--prefer codex`
  exercises both.

## Clarify first, slice vertically, verify each slice

Three quality mechanisms run before and inside every decomposition:

**Pre-flight clarifying questions.** Before the tree dispatches, one cheap
restricted reasoning call triages the task: is it specified well enough to
execute without guessing at intent? Material ambiguities only — things that
change what gets built or how success is judged. In a terminal, you get up
to 3 questions (blank answer = adopt the stated default); running headless
or backgrounded, the run never blocks — it adopts the model's default
assumptions, prints them to stderr, folds them into the task as binding
constraints, and instructs the final result to restate them. A failed or
unparseable triage proceeds as-is; `--no-clarify` skips it entirely.

**Sibling-slice context.** Each dispatched child carries a one-line digest
of its sibling slices ("context only — do NOT do their work"), so parallel
agents stop duplicating or contradicting each other's changes. Digests use
only each description's first line, so they never nest as trees deepen.

**Vertical-slice decomposition.** Decomposers are instructed to cut
subtasks as complete, independently verifiable behaviors end-to-end (code +
wiring + check), never as architectural layers ("all the models / all the
endpoints / all the UI") — a layer split means no subtask is testable alone
and parallel agents produce mismatched pieces. Constraints from the parent
task (error handling, security requirements, compatibility) must be carried
into the text of every subtask they apply to, since leaf agents see nothing
else.

**Per-slice `verify` + a standing quality charter.** Every decomposed
subtask carries a `"verify"` field — one concrete check (a command, a test,
an observable behavior) proving that slice is done — which rides down into
the coding agent's prompt as its definition of done, with an instruction to
actually run the check and report the real outcome (or say it couldn't).
Every coding-tier prompt also carries `CODING_QUALITY_CHARTER`
(`prompts.py`): root-cause before patching, deliberate error handling,
untrusted-input hygiene at boundaries, match the repo's idiom, and honest
reporting of what was and wasn't verified. Together with the review gate,
this closes the loop: the decomposer defines "done", the coder proves it,
the reviewer rejects claims that weren't.

## Adaptive depth (why SECOND exists but isn't always used)

Depth costs money: every extra tier adds a decompose call, a synthesis call,
and up to `MAX_FANOUT`x more leaves. So each decompose call also marks every
subtask `needs_decomposition: true|false`:

- **false** — one coding-tier agent can finish it in a focused session. The
  subtask skips every intermediate tier and goes straight to a coding agent
  (Sonnet 5 / GPT-5.6-Luna / Gemini 3.5 Flash). A small bug-fix can legitimately
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
tree. The ORCHESTRATOR tier has only one provider (Opus 4.8), so it has no
failover — keep its per-call `--timeout` generous. ENSEMBLE mode doesn't fail
over because it already calls every provider. The decision panel doesn't fail
over either; a voter that dies abstains and the remaining voters decide.

## Cross-provider parallelism (free wall-clock, no extra rate-limit risk)

Siblings routed to *different* providers have zero rate-limit contention —
your Claude, ChatGPT, and Google seats are independent accounts. So:

- **Isolated runs parallelize automatically.** Every call has its own
  scratch dir (nothing to collide in), and the per-provider semaphores do
  the scheduling: same-seat siblings still run one-at-a-time
  (`PROVIDER_CONCURRENCY`), different-seat siblings overlap. At most one
  call per seat is in flight — the same account pressure as a sequential
  run, finished sooner.
- **ENSEMBLE fan-out is concurrent.** A node that calls every provider
  now takes as long as the slowest one instead of the sum of all of them.
  Ensemble *decompose* calls are concurrent even in `--project` mode — they
  run read-only since the least-privilege change, so they can't collide in
  the tree.
- **ENSEMBLE merges by majority.** A merged leaf succeeds when strictly
  more than half its providers succeeded — one stalled provider no longer
  discards two good answers. Failed providers contribute a one-line error
  stub to the merged output, never their raw output (a crash transcript is
  pure token cost to every tier above); their full results stay attached
  as children for the `--json` record.
- **`--project` runs stay sequential by default.** Coding agents there
  share one real working tree; concurrent edits can collide, so concurrency
  remains opt-in via `--parallel-children`.

## Watching a run, and why planners get a shorter leash

`--verbose` (`-v`) prints one line per agent call to stderr as the run works
— `[coding] codex:gpt-5.6-luna work started` / `... done in 94s` — so a
20-minute run is distinguishable from a hung one and you can see exactly
which call is sitting on the clock. stdout is untouched, so `--json` output
stays parseable.

Every run ends with a **cost report on stderr** (never stdout, so `--json`
piping stays clean): calls, ok/fail, agent-time, and output tokens per
provider:model — exact where the CLI reported usage, otherwise estimated
at chars/4 and marked `~`. It covers every call including clarify,
decompose, synthesize, and review, so you can finally see where a run's
budget actually went.

Independently of `--verbose`, every run writes the same progress lines
(timestamped, plus the task and final status) to a durable per-run log at
`~/.local/state/modelmesh/logs/run-<stamp>-<pid>.log`, announced on stderr
at launch. Backgrounded runs often get their shell output redirected into
a temp path that may not outlive the session — the run log means
visibility never depends on where the caller pointed stderr.

Separately, decompose/synthesize/review calls are single-shot text→JSON
work, so they run under a tighter budget than real coding work:
`REASONING_TIMEOUT_SECONDS` (240s) and `REASONING_MAX_TURNS` (3) in
`config.py`, further clamped by `--timeout`/`--max-turns` when those are set
lower. A stalled planner now fails over in ~4 minutes instead of sitting out
the full `--timeout`, and a decompose agent can't spend 15 tool-use turns
wandering the repo before answering. Leaf (coding-tier) calls keep the full
budget.

## Quality review (anti-hallucination loop)

After the tree synthesizes, the ORCHESTRATOR model reviews the integrated
result against the original task with a skeptic's prompt: findings, files,
metrics, or benchmark figures that the work couldn't actually have verified
are grounds for rejection — an audit report that *sounds* authoritative but
invents specifics is a `retry`, not an `accept`.

The reviewer also sees a **verify ledger**: every slice's binding
`verify` definition-of-done paired with the last 1,500 characters of that
agent's report (checks run at the end — the tail carries the evidence, the
rest of the log is token waste). A slice that claims completion without
evidence its check actually ran is mechanically catchable, and the
reviewer can name the offending slices by id.

On `retry` with named `failed_task_ids`, the run performs a **surgical
retry**: only those branches re-dispatch (with the issues folded into
their descriptions), every other branch's cached result is reused, and
only the affected ancestor syntheses re-run — the biggest token/latency
saving in the loop, since one bad slice no longer costs a whole-tree
re-dispatch. Unmatched or absent ids fall back to the guaranteed
full-tree retry, up to `--max-retries` times (default 1;
`MAX_QUALITY_RETRIES` in config). If the reviewer still rejects after the
last retry, the run reports failure with the issues rather than shipping
flagged content. `--no-review` skips the loop; an unparseable or failed
review call accepts-with-a-note instead of wedging the run into retries.

## Why this is buildable at all

Claude Code, Codex CLI, the Antigravity CLI, and the Kimi Code CLI all
officially support running headless off a personal subscription login instead
of a metered API key:

- Claude: `claude login`, then `claude -p "..." --model ... --effort ...`
- Codex: sign in with your ChatGPT account on first run, then `codex exec ...`
- Antigravity: sign in with your Google account, then
  `agy -p "..." --model "Gemini 3.1 Pro (High)"` — the reasoning level is
  part of the model string, exactly as `agy models` lists it
- Kimi: run `kimi login` to authenticate, then
  `kimi -p "..." --model kimi-code/kimi-for-coding --output-format stream-json`

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
   - `kimi` — the Kimi Code CLI; run `kimi login` to authenticate via the
     device-code flow, then `kimi provider list` to confirm the default model
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
`agy`, and `kimi`:

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

modelmesh runs coding-tier agents unattended with their permission prompts
disabled (`claude --permission-mode bypassPermissions`, `agy
--dangerously-skip-permissions`, `codex --sandbox workspace-write`, `kimi
--yolo`). A self-audit (`docs/SECURITY_AUDIT.md`) documents the consequences;
the hardening it drove, and what still stands:

- **Least privilege by call kind (MM-01/02, MM-07).** Only leaf coding work
  runs with the permission bypass. Decompose/synthesize/review calls run
  restricted — claude in default mode (reads work headless, writes/bash are
  denied), `codex --sandbox read-only`, `agy --sandbox`, `kimi` without
  `--yolo` — verified live against all four CLIs.
- **Agents get an allowlisted environment, not your shell's (MM-08).**
  `ENV_ALLOWLIST` in `config.py` passes PATH/HOME/locale/proxy vars only;
  credentials like `ANTHROPIC_API_KEY` are stripped (verified with a canary
  secret). All four CLIs authenticate from their own state under HOME. If
  you want API-key burst billing, add the key to the allowlist deliberately.
- **The per-call working directory is still a starting cwd, not a sandbox,
  for the coding tier.** A bypassed coding agent can read/write outside it.
  Run against a branch, review the diff, and don't point `--project` at a
  tree you can't afford an agent to edit.
- **`--project` refuses modelmesh's own source tree (MM-03/09).** It's an
  editable install, so the running code *is* the checkout. The resolved
  (symlink-free) project path is rejected if it is, contains, or sits inside
  the package tree (`--dry-run` is exempt — it spawns nothing).
- **Cross-agent text is fenced as untrusted data (MM-04).** Child outputs,
  integrated results, and reviewer issues are wrapped in `<untrusted_data>`
  tags with a standing do-not-obey directive, closing tags in content are
  neutralized, and each child is size-capped in synthesis prompts. This
  mitigates prompt injection; it does not eliminate it.
- **Volume is bounded (MM-06/14).** Per-call output is capped at
  `MAX_OUTPUT_CHARS` (head+tail kept, truncation marked), and
  `--run-timeout` (default 3600s, 0 = off) is an aggregate wall-clock
  budget: past it no new agent calls start and partial results come back as
  an honest failure.
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
at CODING x 3 second-tier subtasks x 3 coding-tier subtasks), on top of the
decompose and synthesis calls at every level above it. `--mode route` (the
default) avoids this by picking one provider per node instead of all three, and
`MAX_FANOUT` in `config.py` caps how wide any single node can branch — but
even a disciplined tree that's 3-wide and 4 tiers deep is dozens of calls
per run, and Opus/GPT-5.6-Sol/Gemini 3.1 Pro/K2.7 Code at high-to-max effort are
not fast or cheap in tokens even when the seat itself is flat-rate.

Concretely, for Claude specifically: **1-3 agents at steady use is where a
Max subscription is cheapest; 5+ concurrent agents will hit rate limits
within hours**, at which point the usual pattern is to keep the subscription
for your primary/interactive path and fail over to `ANTHROPIC_API_KEY`
billing for burst capacity. `PROVIDER_CONCURRENCY` in `config.py` defaults
every provider to 1 concurrent call for exactly this reason — raise it only
after you've watched a real run and know where your account's ceiling is.
Codex (ChatGPT plan), Antigravity (rolling usage windows on the Google
seat), and Kimi have the same shape of limit even though the exact numbers
differ.

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

All four wrappers are intended to be verified against live installed CLIs
(flags drift as these ship weekly — re-check `<cli> --help` after updates):

The `codex` side is verified against codex-cli 0.142.5: `--full-auto` is
gone from `codex exec`; the wrapper uses `--sandbox workspace-write` and
captures the final message via `--output-last-message` (stdout carries the
whole transcript plus token accounting). `gpt-5.6-sol` (max), `gpt-5.6-terra`
(high), and `gpt-5.6-luna` (high) each confirmed live on a ChatGPT seat.

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

The `kimi` side uses `kimi -p <prompt> --output-format stream-json`, with
`--model kimi-code/kimi-for-coding` and `--yolo` for unattended coding work.
The wrapper parses the newline-delimited JSON stream and keeps only
`"role":"assistant"` content lines. There is no native CLI timeout, so the
Python subprocess timeout enforces the budget; there is no `--json-schema`
flag, so structured output is prompt-based like Codex/Gemini. Re-check
`kimi --help` after updates.

## An alternative worth knowing about: let Claude Code orchestrate natively

This package treats all four CLIs symmetrically, as leaf subprocess calls
under one Python dispatcher. There's a second, lower-effort architecture: run
Opus 4.8 interactively (or via `claude -p`) as the actual orchestrator, give it
Bash access, and let its own native subagent system (the `Task` tool) handle
the Claude-side fan-out — with Python only stepping in at the points where a
Claude subagent needs to shell out to `codex`, `agy`, or `kimi`. Claude Code
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
