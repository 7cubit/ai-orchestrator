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
import tempfile
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
    TIER_CONFIG,
)
from .prompts import (
    REVIEW_SCHEMA,
    SUBTASK_SCHEMA,
    decompose_prompt,
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
            description = retry_task_description(big_task, review["issues"])
        # Retries exhausted with the reviewer still unconvinced: surface
        # that rather than shipping content flagged as weak or fabricated.
        result.success = False
        result.error = (
            f"review rejected the result after {attempts} attempt(s): "
            + ("; ".join(result.review["issues"]) or "quality below the bar")
        )
        return result

    def _review(self, big_task: str, result: TaskResult) -> dict:
        spec = self.tier_config[Tier.ORCHESTRATOR][0]
        call = self._call(
            spec, result.task, review_prompt(big_task, result.output),
            schema=REVIEW_SCHEMA,
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

    def _pick_specs(self, tier: Tier, preferred: Optional[str] = None) -> list[AgentSpec]:
        if self.mode is DispatchMode.ENSEMBLE:
            return self.tier_config[tier]
        # Strength-aware routing: honor the provider the parent's decompose
        # call assigned to this subtask, when that provider is configured at
        # this tier. Otherwise fall back to round-robin.
        if preferred:
            for spec in self.tier_config[tier]:
                if spec.provider == preferred:
                    return [spec]
        with self._rr_lock:
            return [next(self._round_robin[tier])]

    def _call(
        self, spec: AgentSpec, task: Task, prompt: str, schema: Optional[dict] = None
    ) -> TaskResult:
        if self.project:
            call_dir = self.project
        else:
            call_dir = os.path.join(
                self._run_dir, f"{task.tier.value}-{task.task_id}-{spec.provider}"
            )
            os.makedirs(call_dir, exist_ok=True)
        agent = build_agent(
            spec,
            dry_run=self.dry_run,
            timeout=self.timeout,
            max_turns=self.max_turns,
            workdir=call_dir,
        )
        with self._sems[spec.provider]:
            outcome = agent.run(prompt, schema=schema)
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
        specs = self._pick_specs(task.tier, task.preferred_provider)
        lower = next_tier(task.tier)

        if lower is None:
            # CODING tier: leaf. No decomposition, just do the work.
            results = [self._call(spec, task, task.description) for spec in specs]
            return self._merge_leaf_results(task, results)

        # Non-leaf tier: ask the agent(s) to decompose, run children, synthesize.
        # The decompose prompt carries MODEL_STRENGTHS for the providers
        # configured at the child tier, so the decomposing agent routes each
        # subtask to whichever model is strongest for it.
        lower_providers = list(dict.fromkeys(s.provider for s in self.tier_config[lower]))
        decompose_calls = [
            self._call(
                spec,
                task,
                decompose_prompt(task, lower, self.max_fanout, providers=lower_providers),
                schema=SUBTASK_SCHEMA,
            )
            for spec in specs
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

        # ROUTE: the same spec that decomposed also integrates.
        # ENSEMBLE: arbitrarily use the first configured provider as
        # integrator -- swap for real reconciliation logic if you want one
        # tier's agent to judge/pick between the others rather than just
        # merge their output.
        synth_spec = specs[0]
        synth_result = self._call(synth_spec, task, synthesize_prompt(task, all_children))
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
        if not self.parallel_children or len(child_tasks) <= 1:
            return [self._dispatch(t) for t in child_tasks]
        with ThreadPoolExecutor(max_workers=len(child_tasks)) as pool:
            return list(pool.map(self._dispatch, child_tasks))

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
