# CodeRabbit integration — design

Two ways to put CodeRabbit's code review into a modelmesh code run. They are
complementary to modelmesh's built-in review, which only checks the
*synthesized text* for hallucination and never reads the diff.

## Path A — PR-based gate (implemented, no dependency)

Lives in the driving session, not in modelmesh core. See `claude-skill/SKILL.md`
Playbook step 5. After a `--project` code run's internal review accepts and
the session has verified tests, it opens a PR from `modelmesh/<topic>` into the
`.coderabbit.yaml` base branch (`main`), CodeRabbit auto-reviews the PR, and the
session folds real findings back in (direct fix, or a targeted follow-up run).
This is correct today because CodeRabbit here is the GitHub App — a PR-time,
cloud, async reviewer — with no local entry point.

## Path B — in-loop CLI gate (NOT implemented; needs the CodeRabbit CLI)

The tighter loop the user asked for: CodeRabbit reviews the working-tree diff
*inside* a run and its findings feed the retry loop, so modelmesh self-corrects
before it ever produces a branch. Requires the CodeRabbit CLI to be installed
and authenticated on the machine (interactive `auth login` — an operator step,
not something the tool can do unattended).

**BEFORE implementing, verify the real CLI interface** (`coderabbit --help` /
`coderabbit review --help`). Do not code against assumed flags — every value
below is a placeholder to confirm against the installed CLI:

- invocation to review the working tree (`coderabbit review …`)
- a machine/agent-friendly output mode (CodeRabbit documents a `--plain` /
  prompt-style output aimed at AI agents — confirm the exact flag and shape)
- how it scopes to the diff vs a base branch, and its exit code semantics
- auth/rate behavior when run repeatedly in a loop

**Hook point** — mirror the existing review gate in `orchestrator.py`:

1. New flag `--coderabbit` (default off; a run-level opt-in like `--no-review`
   is opt-out). Gate it on `self.project is not None` — a diff only exists for
   real repo runs, never `--isolated`.
2. After the internal review returns `accept` (in `Orchestrator._run_task`,
   where the quality loop decides to return), and only if there is a non-empty
   `git diff`, run one CodeRabbit review over the working tree via the same
   `agents._run`-style subprocess helper (list-form argv, env allowlist,
   timeout, durable-log the call as `kind="coderabbit"` so it shows in
   `--verbose` and the P7 cost table).
3. Parse findings into the same shape the internal reviewer emits
   (`{"verdict": "retry"|"accept", "issues": [...], "failed_task_ids": [...]}`).
   Map file:line findings to slice ids using the same verify-ledger walk P2/P4
   already do, so a CodeRabbit finding can drive the **partial-branch retry**
   (P4) — re-dispatch only the slices it flagged, not the whole tree.
4. Respect `--max-retries`, the aggregate `--run-timeout`, and the "unparseable
   review never wedges the run" rule (a failed/empty CodeRabbit call
   accepts-with-a-note, exactly like `_review`).

**Why it composes cleanly:** modelmesh already has review → issues →
partial-retry plumbing (P2/P4). CodeRabbit becomes a second reviewer feeding
the same loop — internal review catches hallucination, CodeRabbit catches
diff-level bugs/security/style. No new control flow, one new subprocess wrapper
and a findings parser, both written only after the CLI's real interface is
confirmed.
