import json
from pathlib import Path

from mvp_reviewer.models import Finding, ReviewResult, RootCause


def write_report(result: ReviewResult, output_dir: Path) -> tuple[Path, Path]:
    """Write machine-readable JSON and a concise human-readable Markdown review."""
    destination = output_dir.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    json_path = destination / "findings.json"
    markdown_path = destination / "review.md"
    _write_utf8(json_path, json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n")
    _write_utf8(markdown_path, _markdown(result))
    return json_path, markdown_path


def write_error_report(error: str, output_dir: Path) -> tuple[Path, Path]:
    """Write stable artifacts for an operational review failure."""
    destination = output_dir.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    json_path = destination / "findings.json"
    markdown_path = destination / "review.md"
    payload = {"schema_version": 3, "status": "failed", "error": error}
    _write_utf8(json_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    _write_utf8(markdown_path, f"# Codex Code Review\n\n> Review failed: {error}\n")
    return json_path, markdown_path


def _write_utf8(path: Path, content: str) -> None:
    path.write_bytes(content.encode("utf-8", errors="backslashreplace"))


def _markdown(result: ReviewResult) -> str:
    lines = [
        "# Codex Code Review",
        "",
        f"- Base: `{result.base}`",
        f"- Head: `{result.head}`",
        f"- Changed files: {len(result.files)}",
        f"- Confirmed findings: {len(result.findings)}",
        f"- Root causes: {len(result.root_causes)}",
        f"- Inventory tasks: {result.inventory_tasks}",
        f"- Review units: {len(result.review_units)}",
        f"- Flow inventory tasks: {result.flow_inventory_tasks}",
        f"- Review flows: {len(result.review_flows)}",
        f"- Review missions: {sum(len(flow.missions) for flow in result.review_flows)}",
        f"- Repeat runs: {result.repeat_runs}",
        f"- Review tasks: {result.review_tasks}",
        f"- Verification tasks: {result.verification_tasks}",
        f"- Aggregation tasks: {result.aggregation_tasks}",
        "",
    ]
    if result.failed_tasks or result.coverage_gaps:
        lines.extend(
            [
                "> Review coverage was incomplete because a task failed or a safety limit was reached. "
                "See `findings.json` for details.",
                "",
            ]
        )
    if not result.root_causes and not result.findings:
        lines.extend(["No confirmed actionable findings met the MVP evidence threshold.", ""])
        return "\n".join(lines)

    if result.root_causes:
        for index, root_cause in enumerate(result.root_causes, start=1):
            lines.extend(_root_cause_markdown(index, root_cause))
        return "\n".join(lines)

    for index, finding in enumerate(result.findings, start=1):
        lines.extend(_finding_markdown(index, finding))
    return "\n".join(lines)


def _root_cause_markdown(index: int, root_cause: RootCause) -> list[str]:
    evidence = [f"  - {item}" for item in root_cause.evidence]
    finding_ids = ", ".join(f"`{finding_id}`" for finding_id in root_cause.finding_ids)
    return [
        f"## {index}. [{root_cause.severity.upper()}] {root_cause.summary}",
        "",
        f"**Location:** `{root_cause.file_path}:{root_cause.line}`  ",
        f"**Confidence:** `{root_cause.confidence:.2f}`  ",
        f"**Source findings:** {finding_ids}",
        "",
        root_cause.explanation,
        "",
        "**Evidence:**",
        *evidence,
        "",
        f"**Suggested fix:** {root_cause.suggested_fix}",
        "",
    ]


def _finding_markdown(index: int, finding: Finding) -> list[str]:
    evidence = [f"  - {item}" for item in finding.evidence]
    return [
        f"## {index}. [{finding.severity.upper()}] {finding.summary}",
        "",
        f"**Location:** `{finding.file_path}:{finding.line}`  ",
        f"**Category:** `{finding.category}`  ",
        f"**Confidence:** `{finding.confidence:.2f}`",
        "",
        finding.explanation,
        "",
        "**Evidence:**",
        *evidence,
        "",
        f"**Suggested fix:** {finding.suggested_fix}",
        "",
        f"**Verification:** {finding.verification}",
        "",
    ]
