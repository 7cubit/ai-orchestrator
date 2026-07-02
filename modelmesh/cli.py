"""Entry point: `python -m modelmesh "some big task"` or, once installed as a
package, `modelmesh "some big task"`."""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

from .config import DispatchMode, MAX_QUALITY_RETRIES
from .orchestrator import Orchestrator
from .tasks import TaskResult


def _print_tree(result: TaskResult, indent: int = 0) -> None:
    pad = "  " * indent
    status = "ok" if result.success else f"FAILED: {result.error}"
    print(
        f"{pad}[{result.task.tier.value}] "
        f"{result.provider}:{result.model}@{result.effort} - {status}"
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
        "children": [_to_dict(c) for c in r.children],
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="modelmesh", description=__doc__)
    parser.add_argument("task", help="Description of the big task to run through the hierarchy")
    parser.add_argument("--dry-run", action="store_true",
                         help="Skip real CLI calls; use canned stub responses")
    parser.add_argument("--mode", choices=["route", "ensemble"], default="route")
    parser.add_argument("--max-fanout", type=int, default=3)
    parser.add_argument("--parallel-children", action="store_true",
                         help="Fan out child tasks concurrently (mind provider rate limits)")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--max-turns", type=int, default=15,
                         help="Max agent turns per Claude Code call")
    parser.add_argument("--workdir", default=None,
                         help="Base directory for per-call agent workdirs "
                              "(default: a fresh temp directory per run)")
    parser.add_argument("--project", default=None,
                         help="Path to a real repo to work in: every agent "
                              "call runs there instead of an isolated scratch "
                              "dir (avoid --parallel-children with this)")
    parser.add_argument("--providers", default=None,
                         help="Comma-separated subset of providers to use, "
                              "e.g. 'claude' if codex/gemini aren't "
                              "installed yet")
    parser.add_argument("--no-review", action="store_true",
                         help="Skip the orchestrator's post-synthesis quality/"
                              "hallucination review (and its retries)")
    parser.add_argument("--max-retries", type=int, default=MAX_QUALITY_RETRIES,
                         help="Full re-dispatches allowed when review rejects "
                              "the result")
    parser.add_argument("--json", action="store_true",
                         help="Print the final result tree as JSON instead of text")
    args = parser.parse_args(argv)

    project = None
    if args.project:
        project = os.path.abspath(os.path.expanduser(args.project))
        if not os.path.isdir(project):
            parser.error(f"--project: no such directory: {project}")
        if args.parallel_children:
            print(
                "warning: --project with --parallel-children means concurrent "
                "agents share one working tree; their edits can collide",
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
    )
    result = orchestrator.run(args.task)

    if args.json:
        print(json.dumps(_to_dict(result), indent=2))
    else:
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

    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
