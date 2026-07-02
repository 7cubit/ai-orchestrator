"""Entry point: `python -m modelmesh "some big task"` or, once installed as a
package, `modelmesh "some big task"`."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from typing import Optional

from .config import (
    DEFAULT_MAX_TURNS,
    DEFAULT_TIMEOUT_SECONDS,
    DispatchMode,
    MAX_FANOUT,
    MAX_QUALITY_RETRIES,
    RUN_TIMEOUT_SECONDS,
)
from .orchestrator import Orchestrator
from .tasks import TaskResult


def _print_tree(result: TaskResult, indent: int = 0) -> None:
    pad = "  " * indent
    status = "ok" if result.success else f"FAILED: {result.error}"
    failover = (
        f" (failed over from {', '.join(result.failover_from)})"
        if result.failover_from else ""
    )
    print(
        f"{pad}[{result.task.tier.value}] "
        f"{result.provider}:{result.model}@{result.effort} - {status}{failover}"
    )
    for child in result.children:
        _print_tree(child, indent + 1)


def _to_dict(r: TaskResult) -> dict:
    return {
        "tier": r.task.tier.value,
        "provider": r.provider,
        "model": r.model,
        "effort": r.effort,
        "success": r.success,
        "output": r.output,
        "error": r.error,
        "review": r.review,
        "failover_from": r.failover_from,
        "children": [_to_dict(c) for c in r.children],
    }


def _is_purgeable(path: str) -> bool:
    """True when `path` lives somewhere the OS or a harness may delete out
    from under a running agent: the system temp tree, or a session-scoped
    scratchpad. A --project run there once lost its worktree mid-run and a
    coding agent fell back into the operator's main checkout."""
    real = os.path.realpath(path)
    temp_roots = {
        os.path.realpath(tempfile.gettempdir()),
        "/tmp", "/private/tmp", "/var/folders", "/private/var/folders",
    }
    for root in temp_roots:
        root = os.path.realpath(root)
        if real == root or real.startswith(root + os.sep):
            return True
    return "scratchpad" in real.split(os.sep)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="modelmesh", description=__doc__)
    parser.add_argument("task", nargs="?", default=None,
                         help="Description of the big task to run through the "
                              "hierarchy; omit it to start an interactive "
                              "session and type tasks at a prompt")
    parser.add_argument("--dry-run", action="store_true",
                         help="Skip real CLI calls; use canned stub responses")
    parser.add_argument("--mode", choices=["route", "ensemble"], default="route")
    parser.add_argument("--max-fanout", type=int, default=MAX_FANOUT,
                         help=f"Max subtasks per node (hard-capped at "
                              f"MAX_FANOUT={MAX_FANOUT} in config)")
    parser.add_argument("--parallel-children", action="store_true",
                         help="Also fan out child tasks concurrently in "
                              "--project mode, where agents share one "
                              "working tree and edits can collide. Isolated "
                              "runs already parallelize automatically "
                              "(per-provider semaphores keep each seat at "
                              "its concurrency cap)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--run-timeout", type=int, default=RUN_TIMEOUT_SECONDS,
                         help="Aggregate wall-clock budget for the whole run "
                              "in seconds; past it, no new agent calls start "
                              "and partial results are returned as a failure "
                              "(0 = unlimited)")
    parser.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS,
                         help="Max agent turns per Claude Code call")
    parser.add_argument("--verbose", "-v", action="store_true",
                         help="Print one line per agent call (start/finish, "
                              "elapsed time) to stderr while the run works, "
                              "so long runs are distinguishable from hangs")
    parser.add_argument("--workdir", default=None,
                         help="Base directory for per-call agent workdirs "
                              "(default: a fresh temp directory per run)")
    parser.add_argument("--project", default=None,
                         help="Repo to work in (default: the current "
                              "directory, like claude/codex/agy). Pass a "
                              "path to target another repo without cd-ing")
    parser.add_argument("--allow-temp-project", action="store_true",
                         help="Allow the project to live under a temp/"
                              "scratchpad path that may be purged mid-run "
                              "(refused by default: agents whose workdir "
                              "vanishes can escape into other checkouts)")
    parser.add_argument("--isolated", action="store_true",
                         help="Don't touch the current directory: run every "
                              "agent in its own scratch dir instead (for "
                              "purely generative tasks)")
    parser.add_argument("--providers", default=None,
                         help="Comma-separated subset of providers to use, "
                              "e.g. 'claude' if codex/agy aren't "
                              "installed yet")
    parser.add_argument("--prefer", default=None, choices=["claude", "codex", "agy"],
                         help="Bias routing toward one provider wherever it's "
                              "configured at a tier (e.g. --prefer codex to "
                              "lean on abundant ChatGPT quota). Overrides "
                              "strength routing; failover still uses the "
                              "others on timeout/error")
    parser.add_argument("--no-review", action="store_true",
                         help="Skip the orchestrator's post-synthesis quality/"
                              "hallucination review (and its retries)")
    parser.add_argument("--max-retries", type=int, default=MAX_QUALITY_RETRIES,
                         help="Full re-dispatches allowed when review rejects "
                              "the result")
    parser.add_argument("--json", action="store_true",
                         help="Print the final result tree as JSON instead of text")
    args = parser.parse_args(argv)

    if args.isolated and args.project:
        parser.error("--isolated and --project are mutually exclusive")
    project = None
    if not args.isolated:
        # Like claude/codex/agy: the project is wherever you're standing,
        # unless --project points somewhere else. realpath (MM-09), so a
        # symlink can't route around the containment checks below.
        project = os.path.realpath(os.path.expanduser(args.project or os.getcwd()))
        if not os.path.isdir(project):
            parser.error(f"--project: no such directory: {project}")
        # MM-03: never point agents at modelmesh's own source. This is an
        # editable install, so the running code IS the checkout -- an agent
        # editing it plants changes that execute on the next invocation.
        # Refuse the package tree itself, anything inside it, and any
        # ancestor that contains it.
        # (--dry-run is exempt: it spawns no agents, and the README's first
        # smoke test runs from inside this very repo.)
        pkg_root = os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
        proj_parts, pkg_parts = project.split(os.sep), pkg_root.split(os.sep)
        shorter = min(len(proj_parts), len(pkg_parts))
        if proj_parts[:shorter] == pkg_parts[:shorter] and not args.dry_run:
            parser.error(
                f"project {project} contains (or is inside) modelmesh's own "
                f"source tree at {pkg_root}. A permission-bypassed agent "
                f"there could rewrite the orchestrator itself (audit MM-03). "
                f"Point --project elsewhere, or work on a copy."
            )
        if _is_purgeable(project) and not args.allow_temp_project:
            parser.error(
                f"project {project} is under a temp/scratchpad path that can "
                f"be purged mid-run, letting agents escape into other "
                f"checkouts. Use a durable location (e.g. a git worktree "
                f"under the repo), or pass --allow-temp-project to override."
            )
        if args.parallel_children:
            print(
                "warning: agents share one working tree under "
                "--parallel-children; their edits can collide (use "
                "--isolated for generative tasks)",
                file=sys.stderr,
            )

    orchestrator = Orchestrator(
        mode=DispatchMode(args.mode),
        dry_run=args.dry_run,
        max_fanout=args.max_fanout,
        timeout=args.timeout,
        max_turns=args.max_turns,
        parallel_children=args.parallel_children,
        workdir=args.workdir,
        review=not args.no_review,
        max_retries=args.max_retries,
        project=project,
        providers=[p.strip() for p in args.providers.split(",")] if args.providers else None,
        prefer=args.prefer,
        verbose=args.verbose,
        run_timeout=args.run_timeout,
    )

    if args.task is None:
        return _repl(orchestrator, args)

    result = orchestrator.run(args.task)
    _emit(result, as_json=args.json)
    return 0 if result.success else 1


def _emit(result: TaskResult, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(_to_dict(result), indent=2))
        return
    _print_tree(result)
    if result.review:
        note = result.review.get("note")
        print(
            f"\nreview: {result.review['verdict']} "
            f"(attempt {result.review.get('attempts', 1)})"
            + (f" - {note}" if note else "")
        )
        for issue in result.review.get("issues", []):
            print(f"  - {issue}")
    print("\n--- final output ---\n")
    print(result.output)


def _repl(orchestrator: Orchestrator, args: argparse.Namespace) -> int:
    """Interactive session: type a task, get a run, repeat. All flags given
    at launch (mode, project, providers, review, ...) apply to every task."""
    try:
        import readline  # noqa: F401  -- line editing + up-arrow history
    except ImportError:
        pass
    providers = ",".join(sorted(
        {s.provider for specs in orchestrator.tier_config.values() for s in specs}
    ))
    print(f"modelmesh {'DRY-RUN ' if orchestrator.dry_run else ''}interactive "
          f"-- mode={args.mode}, providers={providers}"
          + (f", project={orchestrator.project}" if orchestrator.project else ""))
    print("Type a task and press enter. /exit (or Ctrl-D) to quit.")
    while True:
        try:
            line = input("\nmodelmesh> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in ("/exit", "/quit", "exit", "quit"):
            return 0
        try:
            result = orchestrator.run(line)
        except KeyboardInterrupt:
            print("\n[run interrupted -- back at the prompt]")
            continue
        _emit(result, as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
