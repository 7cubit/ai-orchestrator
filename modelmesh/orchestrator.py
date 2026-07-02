"""Recursive dispatcher: walks a Task down through ORCHESTRATOR -> MAIN ->
SECOND -> CODING, then walks the results back up through synthesis.

Two dispatch modes:
  ROUTE    - one provider per node (round-robin across the tier's specs)
  ENSEMBLE - call every provider configured for that tier, keep every output
             for the parent to reconcile (higher quota spend; useful when you
             want cross-checking rather than raw throughput)
"""
from __future__ import annotations

import itertools
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Lock, Semaphore
from typing import Optional

from .agents import build_agent
from .config import (
    AgentSpec,
    DispatchMode,
    MAX_FANOUT,
    MAX_QUALITY_RETRIES,
    PROVIDER_CONCURRENCY,
    REASONING_MAX_TURNS,
    REASONING_TIMEOUT_SECONDS,
    RUN_TIMEOUT_SECONDS,
    SPEED_RANK,
    TIER_CONFIG,
)
from .prompts import (
    CLARIFY_SCHEMA,
    REVIEW_SCHEMA,
    SUBTASK_SCHEMA,
    clarify_prompt,
    decompose_prompt,
    leaf_prompt,
    parse_clarify,
    parse_review,
    parse_subtasks,
    retry_task_description,
    review_prompt,
    synthesize_prompt,
)
from .tasks import Task, TaskResult, Tier, next_tier


class Orchestrator:
    def __init__(
        self,
        *,
        mode: DispatchMode = DispatchMode.ROUTE,
        dry_run: bool = False,
        max_fanout: int = 3,
        timeout: int = 600,
        max_turns: int = 15,
        parallel_children: bool = False,
        workdir: Optional[str] = None,
        review: bool = True,
        max_retries: int = MAX_QUALITY_RETRIES,
        project: Optional[str] = None,
        providers: Optional[list[str]] = None,
        prefer: Optional[str] = None,
        verbose: bool = False,
        run_timeout: int = RUN_TIMEOUT_SECONDS,
        clarify: bool = True,
    ):
        self.mode = mode
        self.dry_run = dry_run
        # MAX_FANOUT is the hard ceiling; a caller can ask for less but not
        # more, so one node can never fan out past it however --max-fanout
        # is set. (Previously MAX_FANOUT was defined but never enforced.)
        self.max_fanout = max(1, min(max_fanout, MAX_FANOUT))
        self.timeout = timeout
        self.max_turns = max_turns
        self.parallel_children = parallel_children
        self.workdir = workdir
        # Live progress: one line per call start/finish on stderr, so a
        # 20-minute run is distinguishable from a hung one.
        self.verbose = verbose
        # Aggregate wall-clock budget: past the deadline, calls not yet
        # started fail fast instead of spawning (0 = unlimited). In-flight
        # subprocess calls run to their own --timeout; partial results
        # propagate up through the normal failure path.
        self.run_timeout = run_timeout
        self._deadline: Optional[float] = None
        # Pre-flight ambiguity triage: one cheap reasoning call before the
        # tree dispatches. Interactive terminal -> ask the operator;
        # headless -> adopt the model's stated default assumptions and
        # surface them. Never blocks or fails a run; --no-clarify skips it.
        self.clarify = clarify
        self.review = review
        self.max_retries = max_retries
        # A real repo to work in: every agent call runs with this as its cwd
        # instead of an isolated scratch dir. Mind --parallel-children here --
        # concurrent agents then share one working tree.
        self.project = project
        # Restrict the run to a subset of providers (e.g. only the CLIs you
        # actually have installed/authenticated) without editing TIER_CONFIG.
        if providers:
            self.tier_config: dict[Tier, list[AgentSpec]] = {}
            for tier, specs in TIER_CONFIG.items():
                kept = [s for s in specs if s.provider in providers]
                if not kept:
                    raise ValueError(
                        f"providers {providers} leave no model at tier '{tier.value}'"
                    )
                self.tier_config[tier] = kept
        else:
            self.tier_config = TIER_CONFIG
        # Bias routing toward one provider wherever it's configured at a tier
        # (e.g. --prefer codex to lean on an abundant ChatGPT quota). It
        # overrides the model's per-subtask routing but not failover, so the
        # other providers still catch timeouts/errors. Tiers without this
        # provider (e.g. ORCHESTRATOR, which is claude-only) are unaffected.
        if prefer:
            all_providers = {s.provider for specs in self.tier_config.values() for s in specs}
            if prefer not in all_providers:
                raise ValueError(
                    f"--prefer {prefer!r}: not a configured provider "
                    f"({sorted(all_providers)})"
                )
        self.prefer = prefer
        self._run_dir: Optional[str] = None
        self._round_robin = {
            tier: itertools.cycle(specs) for tier, specs in self.tier_config.items()
        }
        # itertools.cycle isn't documented thread-safe; guard it so
        # --parallel-children can't hand two children the same spec.
        self._rr_lock = Lock()
        # In dry-run there's no real account to rate-limit, so don't let the
        # semaphore serialize a demo run for no reason.
        self._sems = {
            provider: Semaphore(999 if dry_run else n)
            for provider, n in PROVIDER_CONCURRENCY.items()
        }

    def run(self, big_task: str) -> TaskResult:
        # Every agent call gets its own directory under here, so unattended
        # agents never share (or trash) the caller's working directory.
        self._run_dir = self.workdir or tempfile.mkdtemp(prefix="modelmesh-")
        os.makedirs(self._run_dir, exist_ok=True)
        self._deadline = (
            time.monotonic() + self.run_timeout if self.run_timeout else None
        )

        # Pin down intent before spending the whole tree on a guess. The
        # clarified task (with operator answers or adopted assumptions
        # folded in) is what every tier -- including review -- works from.
        big_task = self._clarify(big_task)

        # Quality loop: dispatch, have the orchestrator model review the
        # integrated result for hallucinated/unsupported content and gaps,
        # and re-dispatch with the reviewer's issues folded into the task
        # until it accepts or retries run out.
        description = big_task
        attempts = 1 + (self.max_retries if self.review else 0)
        result: TaskResult
        for attempt in range(1, attempts + 1):
            root = Task(description=description, tier=Tier.ORCHESTRATOR)
            result = self._dispatch(root)
            if not self.review or not result.success:
                return result
            review = self._review(big_task, result)
            result.review = {**review, "attempts": attempt}
            if review["verdict"] == "accept":
                return result
            if self._deadline is not None and time.monotonic() > self._deadline:
                result.success = False
                result.error = (
                    f"review requested a retry, but the run deadline of "
                    f"{self.run_timeout}s is exceeded; returning the "
                    f"rejected result: "
                    + ("; ".join(review["issues"]) or "quality below the bar")
                )
                return result
            description = retry_task_description(big_task, review["issues"])
        # Retries exhausted with the reviewer still unconvinced: surface
        # that rather than shipping content flagged as weak or fabricated.
        result.success = False
        result.error = (
            f"review rejected the result after {attempts} attempt(s): "
            + ("; ".join(result.review["issues"]) or "quality below the bar")
        )
        return result

    def _clarify(self, big_task: str) -> str:
        """Pre-flight triage: one restricted reasoning call decides whether
        the task is executable without guessing at intent. Materially
        ambiguous + interactive terminal -> ask the operator (blank answer
        adopts the stated default). Headless -> adopt the model's default
        assumptions, print them to stderr, and fold them into the task as
        binding constraints. A failed or unparseable triage never blocks."""
        if not self.clarify:
            return big_task
        spec = self.tier_config[Tier.ORCHESTRATOR][0]
        probe = Task(description=big_task, tier=Tier.ORCHESTRATOR)
        call = self._call(spec, probe, clarify_prompt(big_task),
                          schema=CLARIFY_SCHEMA, kind="clarify")
        if not call.success:
            self._progress("clarify call failed; proceeding with the task as given")
            return big_task
        parsed = parse_clarify(call.output)
        if parsed is None or parsed["clear"] or not parsed["questions"]:
            return big_task
        questions, assumptions = parsed["questions"], parsed["assumptions"]
        if sys.stdin.isatty() and sys.stdout.isatty():
            print(
                "\nBefore dispatching, pin down intent (blank answer = "
                "adopt the stated default):"
            )
            lines = []
            for i, q in enumerate(questions):
                default = assumptions[i] if i < len(assumptions) else None
                hint = f"\n     [default: {default}]" if default else ""
                try:
                    answer = input(f"  {i + 1}. {q}{hint}\n     > ").strip()
                except EOFError:
                    answer = ""
                if not answer:
                    answer = default or (
                        "unanswered; use your best judgment and state the "
                        "choice made"
                    )
                lines.append(f"- Q: {q}\n  A: {answer}")
            return (
                big_task
                + "\n\nClarifications from the operator (binding):\n"
                + "\n".join(lines)
            )
        adopted = [
            f"- {assumptions[i]}" if i < len(assumptions)
            else f"- unresolved question, use best judgment: {q}"
            for i, q in enumerate(questions)
        ]
        print(
            "modelmesh: task is ambiguous and no terminal is attached; "
            "proceeding under explicit assumptions:\n" + "\n".join(adopted),
            file=sys.stderr, flush=True,
        )
        return (
            big_task
            + "\n\nNo operator was available to answer clarifying "
              "questions. Proceed under these explicit assumptions, treat "
              "them as binding constraints, and restate them in the final "
              "result:\n"
            + "\n".join(adopted)
        )

    def _review(self, big_task: str, result: TaskResult) -> dict:
        spec = self.tier_config[Tier.ORCHESTRATOR][0]
        call = self._call(
            spec, result.task, review_prompt(big_task, result.output),
            schema=REVIEW_SCHEMA, kind="review",
        )
        # A reviewer that failed or answered unparseably must not wedge the
        # run into endless retries -- accept and note it.
        if not call.success:
            return {"verdict": "accept", "issues": [], "note": "review call failed; accepted unreviewed"}
        parsed = parse_review(call.output)
        if parsed is None:
            return {"verdict": "accept", "issues": [], "note": "review verdict unparseable; accepted unreviewed"}
        return parsed

    # -- internals ------------------------------------------------------

    def _progress(self, message: str) -> None:
        # stderr, so --json output on stdout stays parseable.
        if self.verbose:
            print(f"modelmesh: {message}", file=sys.stderr, flush=True)

    def _pick_specs(self, tier: Tier, preferred: Optional[str] = None) -> list[AgentSpec]:
        if self.mode is DispatchMode.ENSEMBLE:
            return self.tier_config[tier]
        # Provider selection, in order of precedence:
        #   1. --prefer (operator override, e.g. lean on ChatGPT quota)
        #   2. the provider the parent's decompose assigned to this subtask
        #      (strength-aware routing)
        #   3. round-robin
        # ...whichever is actually configured at this tier.
        for choice in (self.prefer, preferred):
            if choice:
                for spec in self.tier_config[tier]:
                    if spec.provider == choice:
                        return [spec]
        with self._rr_lock:
            return [next(self._round_robin[tier])]

    def _candidate_specs(self, tier: Tier, preferred: Optional[str]) -> list[AgentSpec]:
        """The primary spec for this node, followed by failover candidates
        (the other providers at this tier) ordered fastest-first -- so if the
        primary times out, the next attempt uses a quicker model."""
        primary = self._pick_specs(tier, preferred)[0]
        fallbacks = sorted(
            (s for s in self.tier_config[tier] if s.provider != primary.provider),
            key=lambda s: SPEED_RANK.get(s.speed, 2),
            reverse=True,
        )
        return [primary, *fallbacks]

    def _call_with_failover(
        self, task: Task, prompt: str, preferred: Optional[str] = None,
        schema: Optional[dict] = None, kind: str = "work",
    ) -> TaskResult:
        """ROUTE-mode call with provider failover: try the primary, and on any
        failure (timeout, missing CLI, error exit) hand the SAME subtask to the
        next provider at this tier instead of failing the branch. Bounded by
        the number of providers, so a genuinely bad task can't fan out
        forever."""
        candidates = self._candidate_specs(task.tier, preferred)
        last: Optional[TaskResult] = None
        tried: list[str] = []
        for spec in candidates:
            result = self._call(spec, task, prompt, schema, kind=kind)
            if result.success:
                if last is not None:  # we recovered after >=1 failover
                    result.failover_from = tried
                return result
            tried.append(f"{spec.provider}({result.error})")
            last = result
        # Everyone at this tier failed; return the last attempt, annotated
        # with the full failover chain so the failure is legible.
        if last is not None and len(tried) > 1:
            last.error = "all providers failed -> " + "; ".join(tried)
        return last if last is not None else self._call(candidates[0], task, prompt, schema, kind=kind)

    def _call(
        self, spec: AgentSpec, task: Task, prompt: str,
        schema: Optional[dict] = None, *, kind: str = "work",
    ) -> TaskResult:
        # Run deadline: fail fast instead of spawning. The normal failure
        # propagation carries the partial results up honestly.
        if self._deadline is not None and time.monotonic() > self._deadline:
            self._progress(
                f"[{task.tier.value}] {spec.provider}:{spec.model} {kind} "
                f"skipped (run deadline of {self.run_timeout}s exceeded)"
            )
            return TaskResult(
                task=task, provider=spec.provider, model=spec.model,
                effort=spec.effort, output="", success=False,
                error=f"not started: run deadline of {self.run_timeout}s exceeded",
            )
        if self.project:
            call_dir = self.project
        else:
            call_dir = os.path.join(
                self._run_dir, f"{task.tier.value}-{task.task_id}-{spec.provider}"
            )
            os.makedirs(call_dir, exist_ok=True)
        # Decompose/synthesize/review are single-shot text->JSON calls; they
        # get a tighter timeout and turn budget than real (leaf) work, so a
        # stalled planner fails over in minutes rather than sitting out the
        # full --timeout, and can't spend 15 tool turns wandering the repo.
        reasoning = kind != "work"
        agent = build_agent(
            spec,
            dry_run=self.dry_run,
            timeout=min(self.timeout, REASONING_TIMEOUT_SECONDS) if reasoning else self.timeout,
            max_turns=min(self.max_turns, REASONING_MAX_TURNS) if reasoning else self.max_turns,
            workdir=call_dir,
            # Least privilege (MM-01/MM-02): reasoning calls run without the
            # vendor permission bypass; only leaf coding work is unattended.
            restricted=reasoning,
        )
        label = f"[{task.tier.value}] {spec.provider}:{spec.model} {kind}"
        with self._sems[spec.provider]:
            # Inside the semaphore, so a call queued behind the provider's
            # concurrency slot doesn't read as already running.
            self._progress(f"{label} started")
            started = time.monotonic()
            outcome = agent.run(prompt, schema=schema)
            elapsed = time.monotonic() - started
        self._progress(
            f"{label} {'done' if outcome.success else f'FAILED ({outcome.error})'} "
            f"in {elapsed:.0f}s"
        )
        return TaskResult(
            task=task,
            provider=spec.provider,
            model=spec.model,
            effort=spec.effort,
            output=outcome.output,
            success=outcome.success,
            error=outcome.error,
            raw=outcome.raw,
        )

    def _dispatch(self, task: Task) -> TaskResult:
        ensemble = self.mode is DispatchMode.ENSEMBLE
        lower = next_tier(task.tier)

        if lower is None:
            # CODING tier: leaf. No decomposition, just do the work.
            if ensemble:
                # Distinct providers -> concurrent, unless they'd share a
                # real working tree without the operator opting in.
                thunks = [
                    (lambda s=spec: self._call(s, task, leaf_prompt(task)))
                    for spec in self.tier_config[task.tier]
                ]
                if self.project is None or self.parallel_children:
                    results = self._fanout(thunks)
                else:
                    results = [t() for t in thunks]
                return self._merge_leaf_results(task, results)
            # ROUTE: one provider, with failover to another on timeout/error.
            return self._call_with_failover(task, leaf_prompt(task), task.preferred_provider)

        # Non-leaf tier: ask the agent(s) to decompose, run children, synthesize.
        # The decompose prompt carries MODEL_PROFILES for the providers
        # configured at the child tier, so the decomposing agent routes each
        # subtask to whichever model is strongest for it.
        lower_providers = list(dict.fromkeys(s.provider for s in self.tier_config[lower]))
        decompose_prompt_text = decompose_prompt(
            task, lower, self.max_fanout, providers=lower_providers
        )
        if ensemble:
            # Decompose calls are restricted (read-only) since the
            # least-privilege change, so concurrent planners are safe even
            # in --project mode -- they can't write the shared tree.
            decompose_calls = self._fanout([
                (lambda s=spec: self._call(s, task, decompose_prompt_text,
                                           schema=SUBTASK_SCHEMA, kind="decompose"))
                for spec in self.tier_config[task.tier]
            ])
        else:
            # ROUTE: decompose with failover, so a timed-out planner at this
            # tier hands off to another provider instead of killing the branch.
            decompose_calls = [
                self._call_with_failover(
                    task, decompose_prompt_text, task.preferred_provider,
                    schema=SUBTASK_SCHEMA, kind="decompose",
                )
            ]

        all_children: list[TaskResult] = []
        for call in decompose_calls:
            if not call.success:
                continue  # a failed decompose has no output worth parsing
            subtasks = parse_subtasks(call.output)[: self.max_fanout]
            # Adaptive depth: a subtask one coding agent can finish skips
            # straight to the (cheap) coding tier; only work that genuinely
            # needs another round of division goes to the next tier down.
            #
            # Exception: the ORCHESTRATOR's own split always feeds MAIN, even
            # if the model marked a subtask "no decomposition needed".
            # Otherwise a whole-repo task could collapse to a single coding
            # call -- wasting the orchestrator pass entirely and never
            # engaging the MAIN/SECOND tiers where the other providers live.
            child_tasks = [
                Task(
                    description=st.description,
                    tier=(
                        lower
                        if (st.needs_decomposition or task.tier is Tier.ORCHESTRATOR)
                        else Tier.CODING
                    ),
                    parent_id=task.task_id,
                    depth=task.depth + 1,
                    preferred_provider=st.provider,
                    verify=st.verify,
                )
                for st in subtasks
            ]
            all_children.extend(self._run_children(child_tasks))

        if not all_children:
            # Every decompose call failed (or produced nothing): there is
            # nothing to synthesize, so surface the failure instead of asking
            # an agent to integrate an empty set.
            failed = decompose_calls[0]
            failed.success = False
            failed.error = failed.error or "decomposition produced no subtasks"
            failed.children = decompose_calls[1:]
            return failed

        # Integrate the children back into one result.
        # ROUTE: prefer the provider that decomposed (with failover if it now
        # times out). ENSEMBLE: the first configured provider integrates --
        # swap for real reconciliation logic if you want one tier's agent to
        # judge/pick between the others rather than just merge their output.
        synth_prompt = synthesize_prompt(task, all_children)
        if ensemble:
            synth_result = self._call(
                self.tier_config[task.tier][0], task, synth_prompt,
                kind="synthesize",
            )
        else:
            synth_result = self._call_with_failover(
                task, synth_prompt, decompose_calls[0].provider,
                kind="synthesize",
            )
        synth_result.children = all_children

        # Propagate failure upward: a synthesis over failed subtrees is not a
        # successful run, even if the synthesis call itself came back clean.
        failures = [d for d in decompose_calls if not d.success] + [
            c for c in all_children if not c.success
        ]
        if failures and synth_result.success:
            synth_result.success = False
            reasons = "; ".join(sorted({f.error for f in failures if f.error}))
            synth_result.error = (
                f"{len(failures)} downstream call(s) failed"
                + (f": {reasons}" if reasons else "")
            )
        return synth_result

    def _run_children(self, child_tasks: list[Task]) -> list[TaskResult]:
        # Cross-provider auto-parallelism: in isolated runs every call has
        # its own scratch dir, so there is no shared working tree to collide
        # in -- children can always run concurrently. The per-provider
        # semaphores do the real scheduling: siblings on the same account
        # still serialize (rate limits are per seat), while siblings routed
        # to different providers overlap for free. In --project mode agents
        # share one tree, so concurrency stays opt-in (--parallel-children).
        concurrent = self.parallel_children or self.project is None
        if not concurrent or len(child_tasks) <= 1:
            return [self._dispatch(t) for t in child_tasks]
        with ThreadPoolExecutor(max_workers=len(child_tasks)) as pool:
            return list(pool.map(self._dispatch, child_tasks))

    def _fanout(self, thunks: list) -> list[TaskResult]:
        """Run independent call thunks, concurrently when there are several.
        Used for ENSEMBLE's per-node fan-out, where each call goes to a
        *different* provider by construction -- zero rate-limit contention,
        so wall-clock drops from sum-of-providers to slowest-provider."""
        if len(thunks) <= 1:
            return [t() for t in thunks]
        with ThreadPoolExecutor(max_workers=len(thunks)) as pool:
            return [f.result() for f in [pool.submit(t) for t in thunks]]

    def _merge_leaf_results(self, task: Task, results: list[TaskResult]) -> TaskResult:
        if len(results) == 1:
            return results[0]
        # ENSEMBLE at the coding tier: concatenate rather than silently drop
        # data. Build a *new* wrapper result instead of reusing results[0] --
        # reusing it would make that result its own child and recurse forever
        # the moment anything walks the tree (learned this one the hard way).
        combined = "\n\n".join(f"[{r.provider}:{r.model}]\n{r.output}" for r in results)
        providers = "+".join(sorted({r.provider for r in results}))
        return TaskResult(
            task=task,
            provider=providers,
            model="ensemble",
            effort="ensemble",
            output=combined,
            success=all(r.success for r in results),
            children=results,
        )
