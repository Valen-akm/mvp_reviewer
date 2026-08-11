import unittest
from pathlib import Path

from mvp_reviewer.git_diff import DiffScope
from mvp_reviewer.models import (
    Candidate,
    ChangedLocation,
    Finding,
    ReviewFlow,
    ReviewMission,
    ReviewUnit,
    RootCause,
)
from mvp_reviewer.pipeline import ReviewPipeline


def _flow(
    unit: ReviewUnit,
    *,
    flow_index: int = 1,
    missions: tuple[tuple[str, str], ...] = (("behavioral-correctness", "Check the stated invariants."),),
) -> ReviewFlow:
    flow_id = f"{unit.id}-flow-{flow_index:03d}"
    return ReviewFlow(
        id=flow_id,
        unit_id=unit.id,
        label=f"Flow {flow_index}",
        entrypoint="changed handler",
        actor="authenticated caller",
        controlled_inputs=("request payload",),
        preconditions=("the changed handler is reached",),
        trace=("app.py:10 handler - processes the request",),
        terminal_effect="returns or mutates application state",
        invariants=("the changed behavior preserves its public contract",),
        changed_locations=(ChangedLocation("app.py", 10, "changed handler"),),
        missions=tuple(
            ReviewMission(f"{flow_id}-mission-{index:03d}", name, objective)
            for index, (name, objective) in enumerate(missions, start=1)
        ),
    )


class DynamicRunner:
    def __init__(self) -> None:
        self.review_calls: list[tuple[str, str, int, tuple[Candidate, ...]]] = []

    async def inventory(self, scope: DiffScope) -> list[ReviewUnit]:
        return [ReviewUnit("unit-001", "Changed behavior", ("app.py",), "Review the changed behavior.")]

    async def map_flows(self, scope: DiffScope, unit: ReviewUnit) -> list[ReviewFlow]:
        return [
            _flow(
                unit,
                missions=(
                    ("tenant-isolation", "Check tenant predicates across the flow."),
                    ("pagination-ordering", "Check filtering, ordering, and pagination invariants."),
                ),
            )
        ]

    async def review(
        self,
        scope: DiffScope,
        flow: ReviewFlow,
        mission: ReviewMission,
        repeat_run: int,
        prior_candidates: tuple[Candidate, ...],
    ) -> list[Candidate]:
        self.review_calls.append((flow.id, mission.name, repeat_run, prior_candidates))
        return []

    async def verify(self, scope: DiffScope, candidate: Candidate) -> Finding:
        raise AssertionError("verification must not run")

    async def aggregate(self, scope: DiffScope, findings: tuple[Finding, ...]) -> list[RootCause]:
        raise AssertionError("aggregation must not run without findings")


class EmptyFlowRunner(DynamicRunner):
    async def map_flows(self, scope: DiffScope, unit: ReviewUnit) -> list[ReviewFlow]:
        return []


class PartiallyAnchoredFlowRunner(DynamicRunner):
    async def inventory(self, scope: DiffScope) -> list[ReviewUnit]:
        return [
            ReviewUnit(
                "unit-001",
                "Changed behavior",
                ("app.py", "helper.py"),
                "Review the changed behavior and helper contract.",
            )
        ]


class AggregatingRunner(DynamicRunner):
    async def review(
        self,
        scope: DiffScope,
        flow: ReviewFlow,
        mission: ReviewMission,
        repeat_run: int,
        prior_candidates: tuple[Candidate, ...],
    ) -> list[Candidate]:
        line = 10 if mission.name == "tenant-isolation" else 11
        return [
            Candidate(
                category="correctness",
                severity="high",
                confidence=0.9,
                file_path="app.py",
                line=line,
                summary=f"Tenant filter is missing at line {line}",
                explanation="Two symptoms originate from the same missing tenant predicate.",
                evidence=(f"app.py:{line} omits the tenant predicate",),
                suggested_fix="Apply the tenant predicate before reading related records.",
            )
        ]

    async def verify(self, scope: DiffScope, candidate: Candidate) -> Finding:
        return Finding(
            **candidate.to_dict(),
            verification="Confirmed from the changed query and caller.",
            confirmed=True,
            introduced_by_diff=True,
            actionable=True,
        )

    async def aggregate(self, scope: DiffScope, findings: tuple[Finding, ...]) -> list[RootCause]:
        return [
            RootCause(
                id="root-001",
                summary="Related queries omit the tenant predicate",
                explanation="Both findings are consequences of the same changed join condition.",
                severity="high",
                confidence=0.92,
                file_path="app.py",
                line=10,
                finding_ids=("finding-001", "finding-002"),
                evidence=("app.py:10-11 contain the two affected query paths",),
                suggested_fix="Apply the tenant predicate in the shared query construction.",
            )
        ]


class InvalidAggregationRunner(AggregatingRunner):
    async def aggregate(self, scope: DiffScope, findings: tuple[Finding, ...]) -> list[RootCause]:
        root_cause = (await super().aggregate(scope, findings))[0]
        return [
            RootCause(
                id=root_cause.id,
                summary=root_cause.summary,
                explanation=root_cause.explanation,
                severity=root_cause.severity,
                confidence=root_cause.confidence,
                file_path=root_cause.file_path,
                line=root_cause.line,
                finding_ids=("finding-001",),
                evidence=root_cause.evidence,
                suggested_fix=root_cause.suggested_fix,
            )
        ]


class DowngradingAggregationRunner(AggregatingRunner):
    async def aggregate(self, scope: DiffScope, findings: tuple[Finding, ...]) -> list[RootCause]:
        root_cause = (await super().aggregate(scope, findings))[0]
        return [
            RootCause(
                id=root_cause.id,
                summary=root_cause.summary,
                explanation=root_cause.explanation,
                severity="medium",
                confidence=root_cause.confidence,
                file_path=root_cause.file_path,
                line=root_cause.line,
                finding_ids=root_cause.finding_ids,
                evidence=root_cause.evidence,
                suggested_fix=root_cause.suggested_fix,
            )
        ]


class DynamicPipelineTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.scope = DiffScope(
            repo=Path("/tmp/example"),
            base="base",
            head="head",
            files=("app.py",),
            changed_lines={"app.py": frozenset({10, 11})},
        )

    async def test_dynamic_missions_determine_review_fanout(self) -> None:
        runner = DynamicRunner()
        progress: list[str] = []

        result = await ReviewPipeline(runner, on_progress=progress.append).run(self.scope)

        self.assertEqual(result.flow_inventory_tasks, 1)
        self.assertEqual(len(result.review_flows), 1)
        self.assertEqual(result.review_tasks, 2)
        self.assertEqual(
            {(flow_id, mission_name) for flow_id, mission_name, _repeat, _prior in runner.review_calls},
            {
                ("unit-001-flow-001", "tenant-isolation"),
                ("unit-001-flow-001", "pagination-ordering"),
            },
        )
        self.assertTrue(any("[flow-map] discovered 1 flows and 2 missions" in message for message in progress))

    async def test_empty_flow_inventory_uses_fallback_and_records_coverage_gap(self) -> None:
        result = await ReviewPipeline(EmptyFlowRunner()).run(self.scope)

        self.assertEqual(len(result.review_flows), 1)
        self.assertEqual(result.review_tasks, 1)
        self.assertEqual(len(result.coverage_gaps), 1)
        self.assertIn("returned no review flows", result.coverage_gaps[0])

    async def test_unanchored_changed_file_receives_fallback_flow(self) -> None:
        scope = DiffScope(
            repo=Path("/tmp/example"),
            base="base",
            head="head",
            files=("app.py", "helper.py"),
            changed_lines={"app.py": frozenset({10}), "helper.py": frozenset({5})},
        )

        result = await ReviewPipeline(PartiallyAnchoredFlowRunner()).run(scope)

        self.assertEqual(len(result.review_flows), 2)
        self.assertEqual(result.review_tasks, 3)
        self.assertEqual(result.review_flows[-1].changed_locations[0].file_path, "helper.py")
        self.assertTrue(any("did not anchor changed files: helper.py" in gap for gap in result.coverage_gaps))

    async def test_consumes_all_aggregation_preserves_finding_lineage(self) -> None:
        result = await ReviewPipeline(AggregatingRunner()).run(self.scope)

        self.assertEqual(len(result.findings), 2)
        self.assertEqual(result.aggregation_tasks, 1)
        self.assertEqual(len(result.root_causes), 1)
        self.assertEqual(result.root_causes[0].finding_ids, ("finding-001", "finding-002"))
        payload = result.to_dict()
        self.assertEqual([finding["finding_id"] for finding in payload["findings"]], ["finding-001", "finding-002"])

    async def test_invalid_aggregation_falls_back_without_losing_findings(self) -> None:
        result = await ReviewPipeline(InvalidAggregationRunner()).run(self.scope)

        self.assertEqual(len(result.findings), 2)
        self.assertEqual(len(result.root_causes), 2)
        self.assertTrue(any(error.startswith("aggregate:") for error in result.failed_tasks))

    async def test_aggregation_cannot_lower_verified_severity(self) -> None:
        result = await ReviewPipeline(DowngradingAggregationRunner()).run(self.scope)

        self.assertEqual([root.severity for root in result.root_causes], ["high", "high"])
        self.assertTrue(any("lowers the severity" in error for error in result.failed_tasks))


if __name__ == "__main__":
    unittest.main()
