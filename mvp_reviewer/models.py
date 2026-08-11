from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

CATEGORIES = frozenset({"security", "performance", "correctness"})
SEVERITIES = frozenset({"critical", "high", "medium", "low"})


class ModelOutputError(ValueError):
    """Raised when Codex returns data outside the review contract."""


def _required_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ModelOutputError(f"{key} must be a non-empty string")
    return value.strip()


def _required_boolean(data: dict[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ModelOutputError(f"{key} must be a boolean")
    return value


def _confidence(data: dict[str, Any]) -> float:
    value = data.get("confidence")
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ModelOutputError("confidence must be a number")
    confidence = float(value)
    if not 0 <= confidence <= 1:
        raise ModelOutputError("confidence must be between 0 and 1")
    return confidence


def _line(data: dict[str, Any]) -> int:
    value = data.get("line")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ModelOutputError("line must be a positive integer")
    return value


def _file_path(data: dict[str, Any]) -> str:
    value = data.get("file_path")
    if not isinstance(value, str) or not value:
        raise ModelOutputError("file_path must be a non-empty string")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ModelOutputError("file_path must be repository-relative")
    return str(path)


def _evidence(data: dict[str, Any]) -> tuple[str, ...]:
    value = data.get("evidence")
    if not isinstance(value, list) or not value:
        raise ModelOutputError("evidence must be a non-empty array")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ModelOutputError("each evidence item must be a non-empty string")
    return tuple(item.strip() for item in value)


def _string_array(data: dict[str, Any], key: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    value = data.get(key)
    if not isinstance(value, list) or (not value and not allow_empty):
        requirement = "an array" if allow_empty else "a non-empty array"
        raise ModelOutputError(f"{key} must be {requirement}")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ModelOutputError(f"each {key} item must be a non-empty string")
    return tuple(dict.fromkeys(item.strip() for item in value))


def _changed_files(data: dict[str, Any], scope_files: frozenset[str]) -> tuple[str, ...]:
    value = data.get("changed_files")
    if not isinstance(value, list) or not value:
        raise ModelOutputError("changed_files must be a non-empty array")
    if any(not isinstance(item, str) or not item for item in value):
        raise ModelOutputError("each changed_files item must be a non-empty string")
    unknown = [item for item in value if item not in scope_files]
    if unknown:
        raise ModelOutputError(f"review unit references unchanged file: {unknown[0]}")
    return tuple(dict.fromkeys(value))


def _category(data: dict[str, Any], expected: str | None = None) -> str:
    value = _required_string(data, "category").lower()
    if value not in CATEGORIES:
        raise ModelOutputError(f"unsupported category: {value}")
    if expected is not None and value != expected:
        raise ModelOutputError(f"expected category {expected}, got {value}")
    return value


def _severity(data: dict[str, Any]) -> str:
    value = _required_string(data, "severity").lower()
    if value not in SEVERITIES:
        raise ModelOutputError(f"unsupported severity: {value}")
    return value


@dataclass(frozen=True, slots=True)
class ReviewUnit:
    """One logical group of changed files that receives focused review tasks."""

    id: str
    label: str
    changed_files: tuple[str, ...]
    review_focus: str

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, index: int, scope_files: frozenset[str]) -> "ReviewUnit":
        if not isinstance(data, dict):
            raise ModelOutputError("review unit must be an object")
        return cls(
            id=f"unit-{index:03d}",
            label=_required_string(data, "label"),
            changed_files=_changed_files(data, scope_files),
            review_focus=_required_string(data, "review_focus"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "changed_files": list(self.changed_files),
            "review_focus": self.review_focus,
        }


@dataclass(frozen=True, slots=True)
class ChangedLocation:
    """One objective changed-line anchor supporting a mapped review flow."""

    file_path: str
    line: int
    symbol: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChangedLocation":
        if not isinstance(data, dict):
            raise ModelOutputError("changed location must be an object")
        return cls(
            file_path=_file_path(data),
            line=_line(data),
            symbol=_required_string(data, "symbol"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"file_path": self.file_path, "line": self.line, "symbol": self.symbol}


@dataclass(frozen=True, slots=True)
class ReviewMission:
    """One flow-specific failure mode that receives an independent review task."""

    id: str
    name: str
    objective: str

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "objective": self.objective}


@dataclass(frozen=True, slots=True)
class ReviewFlow:
    """One materially distinct execution, data, state, or contract flow."""

    id: str
    unit_id: str
    label: str
    entrypoint: str
    actor: str
    controlled_inputs: tuple[str, ...]
    preconditions: tuple[str, ...]
    trace: tuple[str, ...]
    terminal_effect: str
    invariants: tuple[str, ...]
    changed_locations: tuple[ChangedLocation, ...]
    missions: tuple[ReviewMission, ...]

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        index: int,
        unit: ReviewUnit,
        contains_changed_line: Callable[[str, int], bool],
    ) -> "ReviewFlow":
        if not isinstance(data, dict):
            raise ModelOutputError("review flow must be an object")
        flow_id = f"{unit.id}-flow-{index:03d}"
        raw_locations = data.get("changed_locations")
        if not isinstance(raw_locations, list) or not raw_locations:
            raise ModelOutputError("changed_locations must be a non-empty array")
        locations = tuple(ChangedLocation.from_dict(item) for item in raw_locations)
        for location in locations:
            if location.file_path not in unit.changed_files:
                raise ModelOutputError(f"review flow references a file outside {unit.id}: {location.file_path}")
            if not contains_changed_line(location.file_path, location.line):
                raise ModelOutputError(
                    f"review flow location is not an added/modified line: {location.file_path}:{location.line}"
                )

        raw_missions = data.get("missions")
        if not isinstance(raw_missions, list) or not raw_missions:
            raise ModelOutputError("missions must be a non-empty array")
        missions = tuple(
            ReviewMission(
                id=f"{flow_id}-mission-{mission_index:03d}",
                name=_required_string(item, "name"),
                objective=_required_string(item, "objective"),
            )
            for mission_index, item in enumerate(raw_missions, start=1)
            if isinstance(item, dict)
        )
        if len(missions) != len(raw_missions):
            raise ModelOutputError("each mission must be an object")
        mission_names = [mission.name.casefold() for mission in missions]
        if len(mission_names) != len(set(mission_names)):
            raise ModelOutputError("mission names must be unique within one review flow")

        return cls(
            id=flow_id,
            unit_id=unit.id,
            label=_required_string(data, "label"),
            entrypoint=_required_string(data, "entrypoint"),
            actor=_required_string(data, "actor"),
            controlled_inputs=_string_array(data, "controlled_inputs"),
            preconditions=_string_array(data, "preconditions"),
            trace=_string_array(data, "trace"),
            terminal_effect=_required_string(data, "terminal_effect"),
            invariants=_string_array(data, "invariants"),
            changed_locations=locations,
            missions=missions,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "unit_id": self.unit_id,
            "label": self.label,
            "entrypoint": self.entrypoint,
            "actor": self.actor,
            "controlled_inputs": list(self.controlled_inputs),
            "preconditions": list(self.preconditions),
            "trace": list(self.trace),
            "terminal_effect": self.terminal_effect,
            "invariants": list(self.invariants),
            "changed_locations": [location.to_dict() for location in self.changed_locations],
            "missions": [mission.to_dict() for mission in self.missions],
        }


@dataclass(frozen=True, slots=True)
class Candidate:
    """One unverified issue proposed by a focused review pass."""

    category: str
    severity: str
    confidence: float
    file_path: str
    line: int
    summary: str
    explanation: str
    evidence: tuple[str, ...]
    suggested_fix: str

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, expected_category: str | None = None) -> "Candidate":
        """Validate and construct a candidate from Codex structured output."""
        if not isinstance(data, dict):
            raise ModelOutputError("candidate must be an object")
        return cls(
            category=_category(data, expected_category),
            severity=_severity(data),
            confidence=_confidence(data),
            file_path=_file_path(data),
            line=_line(data),
            summary=_required_string(data, "summary"),
            explanation=_required_string(data, "explanation"),
            evidence=_evidence(data),
            suggested_fix=_required_string(data, "suggested_fix"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable candidate."""
        return {
            "category": self.category,
            "severity": self.severity,
            "confidence": self.confidence,
            "file_path": self.file_path,
            "line": self.line,
            "summary": self.summary,
            "explanation": self.explanation,
            "evidence": list(self.evidence),
            "suggested_fix": self.suggested_fix,
        }


@dataclass(frozen=True, slots=True)
class RootCause:
    """One semantic root cause that owns one or more verified findings."""

    id: str
    summary: str
    explanation: str
    severity: str
    confidence: float
    file_path: str
    line: int
    finding_ids: tuple[str, ...]
    evidence: tuple[str, ...]
    suggested_fix: str

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        index: int,
        allowed_finding_ids: frozenset[str],
    ) -> "RootCause":
        if not isinstance(data, dict):
            raise ModelOutputError("root cause must be an object")
        finding_ids = _string_array(data, "finding_ids")
        if len(finding_ids) != len(data["finding_ids"]):
            raise ModelOutputError("finding_ids must not contain duplicates")
        unknown = [finding_id for finding_id in finding_ids if finding_id not in allowed_finding_ids]
        if unknown:
            raise ModelOutputError(f"root cause references an unknown finding: {unknown[0]}")
        return cls(
            id=f"root-{index:03d}",
            summary=_required_string(data, "summary"),
            explanation=_required_string(data, "explanation"),
            severity=_severity(data),
            confidence=_confidence(data),
            file_path=_file_path(data),
            line=_line(data),
            finding_ids=finding_ids,
            evidence=_evidence(data),
            suggested_fix=_required_string(data, "suggested_fix"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "summary": self.summary,
            "explanation": self.explanation,
            "severity": self.severity,
            "confidence": self.confidence,
            "file_path": self.file_path,
            "line": self.line,
            "finding_ids": list(self.finding_ids),
            "evidence": list(self.evidence),
            "suggested_fix": self.suggested_fix,
        }


@dataclass(frozen=True, slots=True)
class Finding:
    """A candidate after an independent Codex verification pass."""

    category: str
    severity: str
    confidence: float
    file_path: str
    line: int
    summary: str
    explanation: str
    evidence: tuple[str, ...]
    suggested_fix: str
    verification: str
    confirmed: bool
    introduced_by_diff: bool
    actionable: bool

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, expected_category: str) -> "Finding":
        """Validate and construct a finding from Codex verification output."""
        if not isinstance(data, dict):
            raise ModelOutputError("verification must be an object")
        return cls(
            category=_category(data, expected_category),
            severity=_severity(data),
            confidence=_confidence(data),
            file_path=_file_path(data),
            line=_line(data),
            summary=_required_string(data, "summary"),
            explanation=_required_string(data, "explanation"),
            evidence=_evidence(data),
            suggested_fix=_required_string(data, "suggested_fix"),
            verification=_required_string(data, "verification"),
            confirmed=_required_boolean(data, "confirmed"),
            introduced_by_diff=_required_boolean(data, "introduced_by_diff"),
            actionable=_required_boolean(data, "actionable"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable finding."""
        return {
            "category": self.category,
            "severity": self.severity,
            "confidence": self.confidence,
            "file_path": self.file_path,
            "line": self.line,
            "summary": self.summary,
            "explanation": self.explanation,
            "evidence": list(self.evidence),
            "suggested_fix": self.suggested_fix,
            "verification": self.verification,
            "confirmed": self.confirmed,
            "introduced_by_diff": self.introduced_by_diff,
            "actionable": self.actionable,
        }


@dataclass(frozen=True, slots=True)
class ReviewResult:
    """Final pipeline output plus coverage and failure metadata."""

    base: str
    head: str
    files: tuple[str, ...]
    findings: tuple[Finding, ...]
    review_tasks: int
    candidate_count: int
    verification_tasks: int
    failed_tasks: tuple[str, ...]
    review_units: tuple[ReviewUnit, ...] = ()
    inventory_tasks: int = 0
    repeat_runs: int = 1
    review_flows: tuple[ReviewFlow, ...] = ()
    flow_inventory_tasks: int = 0
    root_causes: tuple[RootCause, ...] = ()
    aggregation_tasks: int = 0
    coverage_gaps: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return the complete result as JSON-compatible data."""
        return {
            "schema_version": 3,
            "status": "complete",
            "base": self.base,
            "head": self.head,
            "files": list(self.files),
            "summary": {
                "complete": not self.failed_tasks and not self.coverage_gaps,
                "findings": len(self.findings),
                "root_causes": len(self.root_causes),
                "review_tasks": self.review_tasks,
                "review_units": len(self.review_units),
                "review_flows": len(self.review_flows),
                "review_missions": sum(len(flow.missions) for flow in self.review_flows),
                "inventory_tasks": self.inventory_tasks,
                "flow_inventory_tasks": self.flow_inventory_tasks,
                "repeat_runs": self.repeat_runs,
                "candidates": self.candidate_count,
                "verification_tasks": self.verification_tasks,
                "aggregation_tasks": self.aggregation_tasks,
                "failed_tasks": len(self.failed_tasks),
                "coverage_gaps": len(self.coverage_gaps),
            },
            "review_units": [unit.to_dict() for unit in self.review_units],
            "review_flows": [flow.to_dict() for flow in self.review_flows],
            "findings": [
                {"finding_id": f"finding-{index:03d}", **finding.to_dict()}
                for index, finding in enumerate(self.findings, start=1)
            ],
            "root_causes": [root_cause.to_dict() for root_cause in self.root_causes],
            "coverage_gaps": list(self.coverage_gaps),
            "errors": list(self.failed_tasks),
        }
