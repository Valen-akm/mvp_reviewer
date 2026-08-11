import asyncio
import unicodedata
from collections.abc import Callable
from typing import Protocol

from mvp_reviewer.git_diff import DiffScope
from mvp_reviewer.models import (
    Candidate,
    ChangedLocation,
    Finding,
    ReviewFlow,
    ReviewMission,
    ReviewResult,
    ReviewUnit,
    RootCause,
)

SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}
MIN_CONFIDENCE = 0.75
MAX_CANDIDATES = 50
MAX_REVIEW_UNITS = 50
MAX_REVIEW_FLOWS = 100
MAX_FLOWS_PER_UNIT = 25
MAX_MISSIONS_PER_FLOW = 10


class PipelineError(RuntimeError):
    """Raised when the review pipeline cannot produce a meaningful result."""


class ReviewRunner(Protocol):
    """Execution boundary implemented by Codex and fakes in tests."""

    async def inventory(self, scope: DiffScope) -> list[ReviewUnit]: ...

    async def map_flows(self, scope: DiffScope, unit: ReviewUnit) -> list[ReviewFlow]: ...

    async def review(
        self,
        scope: DiffScope,
        flow: ReviewFlow,
        mission: ReviewMission,
        repeat_run: int,
        prior_candidates: tuple[Candidate, ...],
    ) -> list[Candidate]: ...

    async def verify(self, scope: DiffScope, candidate: Candidate) -> Finding: ...

    async def aggregate(self, scope: DiffScope, findings: tuple[Finding, ...]) -> list[RootCause]: ...


class ReviewPipeline:
    """Run parallel focused reviews followed by per-candidate verification."""

    def __init__(
        self,
        runner: ReviewRunner,
        *,
        concurrency: int = 3,
        repeat_runs: int = 1,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be positive")
        if repeat_runs < 1:
            raise ValueError("repeat_runs must be positive")
        self.runner = runner
        self.concurrency = concurrency
        self.semaphore = asyncio.Semaphore(concurrency)
        self.repeat_runs = repeat_runs
        self.on_progress = on_progress or (lambda _message: None)

    async def run(self, scope: DiffScope) -> ReviewResult:
        """Execute every stage and return canonical ranked findings."""
        if not scope.files:
            return ReviewResult(
                base=scope.base,
                head=scope.head,
                files=(),
                findings=(),
                review_tasks=0,
                candidate_count=0,
                verification_tasks=0,
                failed_tasks=(),
                repeat_runs=self.repeat_runs,
            )

        self.on_progress("[inventory] discovering logical review units")
        try:
            review_units = await self._inventory(scope)
        except Exception as exc:
            raise PipelineError(f"inventory failed: {exc}") from exc

        failures: list[str] = []
        coverage_gaps: list[str] = []
        review_units = _complete_inventory(scope, review_units, failures, coverage_gaps)
        self.on_progress(f"[inventory] discovered {len(review_units)} review units covering {len(scope.files)} files")

        self.on_progress(f"[flow-map] scheduled {len(review_units)} unit tasks (concurrency={self.concurrency})")
        flow_results = await asyncio.gather(
            *(self._map_flows(scope, unit) for unit in review_units),
            return_exceptions=True,
        )
        flows_by_unit: list[tuple[ReviewUnit, tuple[ReviewFlow, ...]]] = []
        for unit, result in zip(review_units, flow_results, strict=True):
            if isinstance(result, BaseException):
                failures.append(f"flow-map:{unit.id}: {result}")
                coverage_gaps.append(f"flow-map:{unit.id} failed; a generic fallback flow was reviewed")
                flows = (_fallback_flow(scope, unit),)
            else:
                flows = _complete_flow_inventory(scope, unit, result, coverage_gaps)
            flows_by_unit.append((unit, flows))
        review_flows = _bound_total_flows(flows_by_unit, coverage_gaps)
        mission_count = sum(len(flow.missions) for flow in review_flows)
        self.on_progress(f"[flow-map] discovered {len(review_flows)} flows and {mission_count} missions")

        candidates: list[Candidate] = []
        prior_by_task: dict[tuple[str, str], list[Candidate]] = {
            (flow.id, mission.id): [] for flow in review_flows for mission in flow.missions
        }
        succeeded_reviews = 0
        review_tasks = 0
        active_tasks = set(prior_by_task)
        for repeat_run in range(1, self.repeat_runs + 1):
            scheduled = [
                (flow, mission)
                for flow in review_flows
                for mission in flow.missions
                if (flow.id, mission.id) in active_tasks
            ]
            self.on_progress(
                f"[review {repeat_run}/{self.repeat_runs}] scheduled {len(scheduled)} tasks "
                f"(concurrency={self.concurrency})"
            )
            review_tasks += len(scheduled)
            review_results = await asyncio.gather(
                *(
                    self._review(
                        scope,
                        flow,
                        mission,
                        repeat_run,
                        tuple(prior_by_task[(flow.id, mission.id)]),
                    )
                    for flow, mission in scheduled
                ),
                return_exceptions=True,
            )
            for (flow, mission), result in zip(scheduled, review_results, strict=True):
                if isinstance(result, BaseException):
                    failures.append(f"review:{flow.id}:{mission.name}:repeat-{repeat_run}: {result}")
                    active_tasks.discard((flow.id, mission.id))
                    continue
                succeeded_reviews += 1
                candidates.extend(result)
                prior_by_task[(flow.id, mission.id)].extend(result)
        if not succeeded_reviews:
            details = "\n".join(failures)
            raise PipelineError(f"all focused Codex review passes failed:\n{details}")

        eligible_candidates = [
            candidate
            for candidate in candidates
            if candidate.severity != "low"
            and candidate.confidence >= MIN_CONFIDENCE
            and scope.contains_changed_line(candidate.file_path, candidate.line)
        ]
        eligible_candidates.sort(key=_candidate_sort_key)
        eligible_candidates = _deduplicate_candidates(eligible_candidates)
        if len(eligible_candidates) > MAX_CANDIDATES:
            omitted = len(eligible_candidates) - MAX_CANDIDATES
            failures.append(
                f"pipeline: omitted {omitted} eligible candidates after the {MAX_CANDIDATES}-candidate limit"
            )
            eligible_candidates = eligible_candidates[:MAX_CANDIDATES]

        self.on_progress(f"[verify] scheduled {len(eligible_candidates)} candidate tasks")
        verification_results = await asyncio.gather(
            *(self._verify(scope, candidate) for candidate in eligible_candidates),
            return_exceptions=True,
        )
        verified: list[Finding] = []
        for index, result in enumerate(verification_results, start=1):
            if isinstance(result, BaseException):
                failures.append(f"verify:{index}: {result}")
                continue
            if (
                result.confirmed
                and result.introduced_by_diff
                and result.actionable
                and result.severity != "low"
                and result.confidence >= MIN_CONFIDENCE
                and scope.contains_changed_line(result.file_path, result.line)
            ):
                verified.append(result)

        self.on_progress(f"[aggregate] ranking and deduplicating {len(verified)} confirmed candidates")
        findings = deduplicate_and_rank(verified)
        aggregation_tasks = 0
        if len(findings) > 1:
            aggregation_tasks = 1
            self.on_progress(f"[aggregate] clustering {len(findings)} findings into semantic root causes")
            try:
                root_causes = _validate_root_causes(scope, findings, tuple(await self._aggregate(scope, findings)))
            except Exception as exc:
                failures.append(f"aggregate: {exc}")
                root_causes = _singleton_root_causes(findings)
        else:
            root_causes = _singleton_root_causes(findings)
        self.on_progress(f"[aggregate] produced {len(root_causes)} root causes from {len(findings)} verified findings")
        incomplete_count = len(failures) + len(coverage_gaps)
        self.on_progress(f"[complete] produced {len(root_causes)} root causes with {incomplete_count} coverage issues")
        return ReviewResult(
            base=scope.base,
            head=scope.head,
            files=scope.files,
            findings=findings,
            review_tasks=review_tasks,
            candidate_count=len(candidates),
            verification_tasks=len(eligible_candidates),
            failed_tasks=tuple(failures),
            review_units=review_units,
            inventory_tasks=1,
            repeat_runs=self.repeat_runs,
            review_flows=review_flows,
            flow_inventory_tasks=len(review_units),
            root_causes=root_causes,
            aggregation_tasks=aggregation_tasks,
            coverage_gaps=tuple(coverage_gaps),
        )

    async def _inventory(self, scope: DiffScope) -> list[ReviewUnit]:
        async with self.semaphore:
            return await self.runner.inventory(scope)

    async def _map_flows(self, scope: DiffScope, unit: ReviewUnit) -> list[ReviewFlow]:
        async with self.semaphore:
            self.on_progress(f"[flow-map] running {unit.id} — {unit.label}")
            result = await self.runner.map_flows(scope, unit)
            self.on_progress(f"[flow-map] completed {unit.id} ({len(result)} flows)")
            return result

    async def _review(
        self,
        scope: DiffScope,
        flow: ReviewFlow,
        mission: ReviewMission,
        repeat_run: int,
        prior_candidates: tuple[Candidate, ...],
    ) -> list[Candidate]:
        async with self.semaphore:
            self.on_progress(
                f"[review {repeat_run}/{self.repeat_runs}] running {flow.id}/{mission.name} — {flow.label}"
            )
            result = await self.runner.review(scope, flow, mission, repeat_run, prior_candidates)
            self.on_progress(
                f"[review {repeat_run}/{self.repeat_runs}] completed {flow.id}/{mission.name} "
                f"({len(result)} new candidates)"
            )
            return result

    async def _verify(self, scope: DiffScope, candidate: Candidate) -> Finding:
        async with self.semaphore:
            self.on_progress(f"[verify] running {candidate.file_path}:{candidate.line} — {candidate.summary}")
            result = await self.runner.verify(scope, candidate)
            verdict = "confirmed" if result.confirmed else "rejected"
            self.on_progress(f"[verify] completed {candidate.file_path}:{candidate.line} ({verdict})")
            return result

    async def _aggregate(self, scope: DiffScope, findings: tuple[Finding, ...]) -> list[RootCause]:
        async with self.semaphore:
            return await self.runner.aggregate(scope, findings)


def _complete_inventory(
    scope: DiffScope,
    proposed: list[ReviewUnit],
    failures: list[str],
    coverage_gaps: list[str],
) -> tuple[ReviewUnit, ...]:
    """Bound model output and guarantee that every changed file belongs to a unit."""
    selected = list(proposed[:MAX_REVIEW_UNITS])
    if len(proposed) > MAX_REVIEW_UNITS:
        omitted = len(proposed) - MAX_REVIEW_UNITS
        failures.append(f"inventory: omitted {omitted} units after the {MAX_REVIEW_UNITS}-unit limit")

    covered = {path for unit in selected for path in unit.changed_files}
    missing = tuple(path for path in scope.files if path not in covered)
    if missing:
        coverage_gaps.append(
            f"inventory did not classify changed files: {', '.join(missing)}; a fallback unit was reviewed"
        )
        if len(selected) >= MAX_REVIEW_UNITS:
            fallback_files = tuple(dict.fromkeys((*selected[-1].changed_files, *missing)))
            fallback = ReviewUnit(
                id="",
                label="Unclassified changed files",
                changed_files=fallback_files,
                review_focus="Review every unclassified changed file and its integration boundaries.",
            )
            selected[-1] = fallback
            failures.append("inventory: replaced the final proposed unit to preserve changed-file coverage")
        else:
            selected.append(
                ReviewUnit(
                    id="",
                    label="Unclassified changed files",
                    changed_files=missing,
                    review_focus="Review every unclassified changed file and its integration boundaries.",
                )
            )

    return tuple(
        ReviewUnit(f"unit-{index:03d}", unit.label, unit.changed_files, unit.review_focus)
        for index, unit in enumerate(selected, start=1)
    )


def _complete_flow_inventory(
    scope: DiffScope,
    unit: ReviewUnit,
    proposed: list[ReviewFlow],
    coverage_gaps: list[str],
) -> tuple[ReviewFlow, ...]:
    """Bound one unit's map while ensuring it still receives a review task."""
    if not proposed:
        coverage_gaps.append(f"flow-map:{unit.id} returned no review flows; a generic fallback flow was reviewed")
        return (_fallback_flow(scope, unit),)

    selected = proposed[:MAX_FLOWS_PER_UNIT]
    if len(proposed) > MAX_FLOWS_PER_UNIT:
        coverage_gaps.append(
            f"flow-map:{unit.id} omitted {len(proposed) - MAX_FLOWS_PER_UNIT} flows after the "
            f"{MAX_FLOWS_PER_UNIT}-flow per-unit limit"
        )

    covered_files = {location.file_path for flow in selected for location in flow.changed_locations}
    missing_files = tuple(path for path in unit.changed_files if path not in covered_files)
    if missing_files:
        if len(selected) >= MAX_FLOWS_PER_UNIT:
            selected = selected[:-1]
            covered_files = {location.file_path for flow in selected for location in flow.changed_locations}
            missing_files = tuple(path for path in unit.changed_files if path not in covered_files)
            coverage_gaps.append(
                f"flow-map:{unit.id} replaced the final mapped flow to preserve changed-file evidence coverage"
            )
        coverage_gaps.append(
            f"flow-map:{unit.id} did not anchor changed files: {', '.join(missing_files)}; "
            "a generic fallback flow was reviewed"
        )
        selected = [*selected, _fallback_flow(scope, unit, changed_files=missing_files)]

    bounded: list[ReviewFlow] = []
    for flow in selected:
        missions = flow.missions[:MAX_MISSIONS_PER_FLOW]
        if len(flow.missions) > MAX_MISSIONS_PER_FLOW:
            coverage_gaps.append(
                f"flow-map:{flow.id} omitted {len(flow.missions) - MAX_MISSIONS_PER_FLOW} missions after the "
                f"{MAX_MISSIONS_PER_FLOW}-mission limit"
            )
        bounded.append(
            ReviewFlow(
                id=flow.id,
                unit_id=flow.unit_id,
                label=flow.label,
                entrypoint=flow.entrypoint,
                actor=flow.actor,
                controlled_inputs=flow.controlled_inputs,
                preconditions=flow.preconditions,
                trace=flow.trace,
                terminal_effect=flow.terminal_effect,
                invariants=flow.invariants,
                changed_locations=flow.changed_locations,
                missions=missions,
            )
        )
    return tuple(bounded)


def _bound_total_flows(
    flows_by_unit: list[tuple[ReviewUnit, tuple[ReviewFlow, ...]]],
    coverage_gaps: list[str],
) -> tuple[ReviewFlow, ...]:
    """Apply a global flow limit while preserving at least one flow per review unit."""
    all_flows = [flow for _unit, flows in flows_by_unit for flow in flows]
    if len(all_flows) <= MAX_REVIEW_FLOWS:
        return tuple(all_flows)

    selected = [flows[0] for _unit, flows in flows_by_unit]
    remaining = [flow for _unit, flows in flows_by_unit for flow in flows[1:]]
    selected.extend(remaining[: MAX_REVIEW_FLOWS - len(selected)])
    omitted = len(all_flows) - len(selected)
    coverage_gaps.append(f"flow-map omitted {omitted} flows after the {MAX_REVIEW_FLOWS}-flow global limit")
    return tuple(selected)


def _fallback_flow(
    scope: DiffScope,
    unit: ReviewUnit,
    *,
    changed_files: tuple[str, ...] | None = None,
) -> ReviewFlow:
    fallback_files = changed_files or unit.changed_files
    locations = tuple(
        ChangedLocation(path, min(scope.changed_lines[path]), "unmapped changed behavior")
        if scope.changed_lines.get(path)
        else ChangedLocation(path, 1, "unmapped changed behavior")
        for path in fallback_files
    )
    flow_id = f"{unit.id}-flow-fallback"
    return ReviewFlow(
        id=flow_id,
        unit_id=unit.id,
        label=f"Fallback review for {unit.label}",
        entrypoint="unmapped changed behavior",
        actor="unknown caller or component",
        controlled_inputs=("changed input or state",),
        preconditions=("the changed behavior is reached",),
        trace=tuple(f"{location.file_path}:{location.line} {location.symbol}" for location in locations),
        terminal_effect="changed production behavior or contract",
        invariants=("the changed behavior preserves its callers' observable contract",),
        changed_locations=locations,
        missions=(
            ReviewMission(
                id=f"{flow_id}-mission-001",
                name="fallback-behavioral-review",
                objective="Determine the affected behavior and check its concrete correctness, security, and resource risks.",
            ),
        ),
    )


def _singleton_root_causes(findings: tuple[Finding, ...]) -> tuple[RootCause, ...]:
    return tuple(
        RootCause(
            id=f"root-{index:03d}",
            summary=finding.summary,
            explanation=finding.explanation,
            severity=finding.severity,
            confidence=finding.confidence,
            file_path=finding.file_path,
            line=finding.line,
            finding_ids=(f"finding-{index:03d}",),
            evidence=finding.evidence,
            suggested_fix=finding.suggested_fix,
        )
        for index, finding in enumerate(findings, start=1)
    )


def _validate_root_causes(
    scope: DiffScope,
    findings: tuple[Finding, ...],
    root_causes: tuple[RootCause, ...],
) -> tuple[RootCause, ...]:
    expected_ids = {f"finding-{index:03d}" for index in range(1, len(findings) + 1)}
    seen_ids = [finding_id for root_cause in root_causes for finding_id in root_cause.finding_ids]
    missing = sorted(expected_ids - set(seen_ids))
    duplicates = sorted({finding_id for finding_id in seen_ids if seen_ids.count(finding_id) > 1})
    if missing or duplicates:
        raise ValueError(
            f"root causes must partition every finding exactly once; missing={missing}, duplicates={duplicates}"
        )
    finding_by_id = {f"finding-{index:03d}": finding for index, finding in enumerate(findings, start=1)}
    downgraded = next(
        (
            root_cause.id
            for root_cause in root_causes
            if SEVERITY_RANK[root_cause.severity]
            < max(SEVERITY_RANK[finding_by_id[finding_id].severity] for finding_id in root_cause.finding_ids)
        ),
        None,
    )
    if downgraded:
        raise ValueError(f"root cause {downgraded} lowers the severity of an owned finding")
    invalid_anchor = next(
        (
            f"{root_cause.file_path}:{root_cause.line}"
            for root_cause in root_causes
            if not scope.contains_changed_line(root_cause.file_path, root_cause.line)
        ),
        None,
    )
    if invalid_anchor:
        raise ValueError(f"root cause is not anchored to the diff: {invalid_anchor}")
    return root_causes


def deduplicate_and_rank(findings: list[Finding]) -> tuple[Finding, ...]:
    """Apply deterministic severity ranking and conservative local deduplication."""
    ordered = sorted(findings, key=_finding_sort_key)
    canonical: list[Finding] = []
    for finding in ordered:
        if any(_duplicates(finding, existing) for existing in canonical):
            continue
        canonical.append(finding)
    return tuple(canonical)


def _deduplicate_candidates(candidates: list[Candidate]) -> list[Candidate]:
    canonical: list[Candidate] = []
    fingerprints: set[tuple[str, int, str, str, tuple[str, ...]]] = set()
    for candidate in candidates:
        fingerprint = _issue_fingerprint(candidate)
        if fingerprint in fingerprints:
            continue
        fingerprints.add(fingerprint)
        canonical.append(candidate)
    return canonical


def _candidate_sort_key(candidate: Candidate) -> tuple[int, float, str, int]:
    return (-SEVERITY_RANK[candidate.severity], -candidate.confidence, candidate.file_path, candidate.line)


def _finding_sort_key(finding: Finding) -> tuple[int, float, str, int]:
    return (-SEVERITY_RANK[finding.severity], -finding.confidence, finding.file_path, finding.line)


def _duplicates(left: Finding, right: Finding) -> bool:
    return _issue_fingerprint(left) == _issue_fingerprint(right)


def _issue_fingerprint(issue: Candidate | Finding) -> tuple[str, int, str, str, tuple[str, ...]]:
    return (
        issue.file_path,
        issue.line,
        _normalize_text(issue.summary),
        _normalize_text(issue.explanation),
        tuple(_normalize_text(item) for item in issue.evidence),
    )


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.split())
