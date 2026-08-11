import argparse
import asyncio
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from mvp_reviewer.codex_runner import CodexExecutionError, CodexRunner
from mvp_reviewer.git_diff import DiffScope, GitError, collect_diff_scope, review_snapshot
from mvp_reviewer.github_pr import PullRequestError, prepare_github_pr
from mvp_reviewer.models import ReviewResult
from mvp_reviewer.pipeline import PipelineError, ReviewPipeline
from mvp_reviewer.report import write_error_report, write_report

EXIT_SUCCESS = 0
EXIT_FINDINGS = 1
EXIT_OPERATIONAL_ERROR = 2
FAIL_ON_LEVELS = ("none", "medium", "high", "critical")
SEVERITY_RANK = {"medium": 1, "high": 2, "critical": 3}


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="python -m mvp_reviewer",
        description="Run staged, high-signal code review passes with Codex CLI.",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--pr", help="Canonical GitHub pull request URL")
    source.add_argument("--repo", type=Path, help="Local Git repository to review (default: current directory)")
    parser.add_argument("--base", help="Base branch or commit for local repository mode, for example origin/main")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("codex-review-output"),
        help="Directory for findings.json and review.md",
    )
    parser.add_argument("--concurrency", type=_positive_integer, default=3, help="Maximum concurrent Codex processes")
    parser.add_argument(
        "--repeat-runs",
        type=_positive_integer,
        default=1,
        help="Review each flow/mission task this many times; later runs return only new candidates",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=_positive_integer,
        default=900,
        help="Timeout for each Codex process",
    )
    parser.add_argument(
        "--fail-on",
        choices=FAIL_ON_LEVELS,
        default="none",
        help="Return 1 when a confirmed finding meets this severity (default: none)",
    )
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help="Return 2 when any Codex review or verification task fails",
    )
    parser.add_argument(
        "--trusted-target",
        action="store_true",
        help="Acknowledge that the review target is trusted to run agent shell commands",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse and validate the two supported review source modes."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.pr:
        if args.base is not None:
            parser.error("--base cannot be used with --pr")
    else:
        if args.base is None:
            parser.error("--base is required unless --pr is used")
        if args.repo is None:
            args.repo = Path.cwd()
    return args


def gate_exit_code(result: ReviewResult, *, fail_on: str, require_complete: bool) -> int:
    """Return the delivery gate exit code for a completed review."""
    if fail_on not in FAIL_ON_LEVELS:
        raise ValueError(f"unsupported fail-on level: {fail_on}")
    if require_complete and (result.failed_tasks or result.coverage_gaps):
        return EXIT_OPERATIONAL_ERROR
    if fail_on == "none":
        return EXIT_SUCCESS
    threshold = SEVERITY_RANK[fail_on]
    severities = [finding.severity for finding in result.findings]
    severities.extend(root_cause.severity for root_cause in result.root_causes)
    if any(SEVERITY_RANK.get(severity, 0) >= threshold for severity in severities):
        return EXIT_FINDINGS
    return EXIT_SUCCESS


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""
    args = parse_args(argv)
    try:
        if not args.trusted_target and os.environ.get("OPEN_KRITT_EPHEMERAL_RUNNER") != "true":
            raise ValueError(
                "untrusted review targets require the ephemeral GitHub-hosted workflow; "
                "pass --trusted-target only for repositories you trust"
            )
        with _review_source(args) as (repo, base):
            scope = collect_diff_scope(repo, base)
            print(f"Reviewing {len(scope.files)} changed files at {scope.head}", file=sys.stderr)
            if scope.files:
                with review_snapshot(scope) as snapshot:
                    result = _run_pipeline(scope, snapshot, args)
            else:
                result = _run_pipeline(scope, scope.repo, args)
        json_path, markdown_path = write_report(result, args.output)
    except (CodexExecutionError, GitError, OSError, PipelineError, PullRequestError, ValueError) as exc:
        print(f"review failed: {exc}", file=sys.stderr)
        try:
            json_path, markdown_path = write_error_report(str(exc), args.output)
            print(f"JSON: {json_path}", file=sys.stderr)
            print(f"Markdown: {markdown_path}", file=sys.stderr)
        except OSError as report_exc:
            print(f"could not write failure report: {report_exc}", file=sys.stderr)
        return EXIT_OPERATIONAL_ERROR

    print(f"Confirmed findings: {len(result.findings)}")
    print(f"Root causes: {len(result.root_causes)}")
    print(f"JSON: {json_path}")
    print(f"Markdown: {markdown_path}")
    incomplete_count = len(result.failed_tasks) + len(result.coverage_gaps)
    if incomplete_count:
        print(f"Warning: {incomplete_count} coverage issues recorded; coverage is incomplete.", file=sys.stderr)
    exit_code = gate_exit_code(result, fail_on=args.fail_on, require_complete=args.require_complete)
    if exit_code == EXIT_OPERATIONAL_ERROR:
        print("Review gate failed because coverage was incomplete.", file=sys.stderr)
    elif exit_code == EXIT_FINDINGS:
        print(f"Review gate failed on {args.fail_on} or higher findings.", file=sys.stderr)
    return exit_code


@contextmanager
def _review_source(args: argparse.Namespace) -> Iterator[tuple[Path, str]]:
    if args.pr:
        with prepare_github_pr(
            args.pr,
            on_progress=lambda message: print(message, file=sys.stderr, flush=True),
        ) as target:
            yield target.repo, target.base
        return
    yield args.repo, args.base


def _run_pipeline(scope: DiffScope, repo: Path, args: argparse.Namespace) -> ReviewResult:
    runner = CodexRunner(repo, timeout_seconds=args.timeout_seconds)
    pipeline = ReviewPipeline(
        runner,
        concurrency=args.concurrency,
        repeat_runs=args.repeat_runs,
        on_progress=lambda message: print(message, file=sys.stderr, flush=True),
    )
    return asyncio.run(pipeline.run(scope))


if __name__ == "__main__":
    raise SystemExit(main())
