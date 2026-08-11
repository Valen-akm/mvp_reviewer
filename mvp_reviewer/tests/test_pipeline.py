import unittest
from pathlib import Path

from mvp_reviewer.codex_runner import CodexRunner
from mvp_reviewer.git_diff import DiffScope
from mvp_reviewer.models import Candidate, ChangedLocation, Finding, ReviewFlow, ReviewMission, ReviewUnit
from mvp_reviewer.pipeline import PipelineError, ReviewPipeline, deduplicate_and_rank
from mvp_reviewer.prompts import verification_prompt


def _mapped_flow(
    scope: DiffScope,
    unit: ReviewUnit,
    mission_names: tuple[str, ...] = ("security", "performance", "correctness"),
) -> ReviewFlow:
    path = unit.changed_files[0]
    line = min(scope.changed_lines[path])
    flow_id = f"{unit.id}-flow-001"
    return ReviewFlow(
        id=flow_id,
        unit_id=unit.id,
        label=unit.label,
        entrypoint="changed behavior",
        actor="application caller",
        controlled_inputs=("changed input",),
        preconditions=("the changed path is reached",),
        trace=(f"{path}:{line} changed behavior",),
        terminal_effect="changed application behavior",
        invariants=(unit.review_focus,),
        changed_locations=(ChangedLocation(path, line, "changed behavior"),),
        missions=tuple(
            ReviewMission(
                f"{flow_id}-mission-{index:03d}",
                name,
                f"Review the {name} failure mode for this flow.",
            )
            for index, name in enumerate(mission_names, start=1)
        ),
    )


class FakeCodexRunner:
    def __init__(self) -> None:
        self.verification_calls: list[Candidate] = []

    async def inventory(self, scope: DiffScope) -> list[ReviewUnit]:
        return [ReviewUnit("unit-001", "Changed application", ("app.py",), "Review the changed behavior.")]

    async def map_flows(self, scope: DiffScope, unit: ReviewUnit) -> list[ReviewFlow]:
        return [_mapped_flow(scope, unit)]

    async def review(
        self,
        scope: DiffScope,
        flow: ReviewFlow,
        mission: ReviewMission,
        repeat_run: int,
        prior_candidates: tuple[Candidate, ...],
    ) -> list[Candidate]:
        category = mission.name
        if category == "security":
            return [
                Candidate(
                    category="security",
                    severity="medium",
                    confidence=0.82,
                    file_path="app.py",
                    line=10,
                    summary="Project deletion skips ownership authorization check",
                    explanation="The changed handler deletes projects for any authenticated caller.",
                    evidence=("app.py:10 reaches delete_project without an ownership predicate",),
                    suggested_fix="Check project ownership before deletion.",
                )
            ]
        if category == "correctness":
            return [
                Candidate(
                    category="correctness",
                    severity="high",
                    confidence=0.91,
                    file_path="app.py",
                    line=10,
                    summary="Project deletion skips ownership authorization check",
                    explanation="The changed handler deletes projects for any authenticated caller.",
                    evidence=("app.py:10 reaches delete_project without an ownership predicate",),
                    suggested_fix="Require ownership in the delete operation.",
                )
            ]
        return [
            Candidate(
                category="performance",
                severity="high",
                confidence=0.95,
                file_path="app.py",
                line=30,
                summary="Unbounded database loop",
                explanation="This candidate is outside the changed lines and must be rejected.",
                evidence=("app.py:30 loops over every project",),
                suggested_fix="Batch the query.",
            )
        ]

    async def verify(self, scope: DiffScope, candidate: Candidate) -> Finding:
        self.verification_calls.append(candidate)
        return Finding(
            category=candidate.category,
            severity=candidate.severity,
            confidence=candidate.confidence,
            file_path=candidate.file_path,
            line=candidate.line,
            summary=candidate.summary,
            explanation=candidate.explanation,
            evidence=candidate.evidence,
            suggested_fix=candidate.suggested_fix,
            verification="Confirmed from the changed handler and its caller.",
            confirmed=True,
            introduced_by_diff=True,
            actionable=True,
        )


class FailingCodexRunner:
    async def inventory(self, scope: DiffScope) -> list[ReviewUnit]:
        return [ReviewUnit("unit-001", "Changed application", ("app.py",), "Review the changed behavior.")]

    async def map_flows(self, scope: DiffScope, unit: ReviewUnit) -> list[ReviewFlow]:
        return [_mapped_flow(scope, unit)]

    async def review(
        self,
        scope: DiffScope,
        flow: ReviewFlow,
        mission: ReviewMission,
        repeat_run: int,
        prior_candidates: tuple[Candidate, ...],
    ) -> list[Candidate]:
        raise RuntimeError(f"{mission.name} unavailable")

    async def verify(self, scope: DiffScope, candidate: Candidate) -> Finding:
        raise AssertionError("verification must not run")


class PartialCodexRunner:
    async def inventory(self, scope: DiffScope) -> list[ReviewUnit]:
        return [ReviewUnit("unit-001", "Changed application", ("app.py",), "Review the changed behavior.")]

    async def map_flows(self, scope: DiffScope, unit: ReviewUnit) -> list[ReviewFlow]:
        return [_mapped_flow(scope, unit)]

    async def review(
        self,
        scope: DiffScope,
        flow: ReviewFlow,
        mission: ReviewMission,
        repeat_run: int,
        prior_candidates: tuple[Candidate, ...],
    ) -> list[Candidate]:
        if mission.name == "security":
            raise RuntimeError("security timed out")
        return []

    async def verify(self, scope: DiffScope, candidate: Candidate) -> Finding:
        raise AssertionError("verification must not run")


class ManyCandidatesRunner:
    async def inventory(self, scope: DiffScope) -> list[ReviewUnit]:
        return [ReviewUnit("unit-001", "Changed application", ("app.py",), "Review the changed behavior.")]

    async def map_flows(self, scope: DiffScope, unit: ReviewUnit) -> list[ReviewFlow]:
        return [_mapped_flow(scope, unit)]

    async def review(
        self,
        scope: DiffScope,
        flow: ReviewFlow,
        mission: ReviewMission,
        repeat_run: int,
        prior_candidates: tuple[Candidate, ...],
    ) -> list[Candidate]:
        category = mission.name
        if category != "correctness":
            return []
        return [
            Candidate(
                category=category,
                severity="high",
                confidence=0.9,
                file_path="app.py",
                line=line,
                summary=f"Defect {line}",
                explanation=f"Explanation {line}",
                evidence=(f"app.py:{line}",),
                suggested_fix="Fix it.",
            )
            for line in range(1, 52)
        ]

    async def verify(self, scope: DiffScope, candidate: Candidate) -> Finding:
        return Finding(
            **candidate.to_dict(),
            verification="Not confirmed.",
            confirmed=False,
            introduced_by_diff=True,
            actionable=True,
        )


class ReviewPipelineTest(unittest.IsolatedAsyncioTestCase):
    async def test_flow_inventory_fans_out_every_dynamic_mission(self) -> None:
        scope = DiffScope(
            repo=Path("/tmp/example"),
            base="base",
            head="head",
            files=("api.py", "db.py"),
            changed_lines={"api.py": frozenset({10}), "db.py": frozenset({20})},
        )
        runner = FanoutCodexRunner()
        progress: list[str] = []

        result = await ReviewPipeline(runner, on_progress=progress.append).run(scope)

        self.assertEqual(result.review_tasks, 6)
        self.assertEqual(len(result.review_units), 2)
        self.assertEqual(
            {(unit_id, category, repeat_run) for unit_id, category, repeat_run, _prior in runner.review_calls},
            {
                ("unit-001", "security", 1),
                ("unit-001", "performance", 1),
                ("unit-001", "correctness", 1),
                ("unit-002", "security", 1),
                ("unit-002", "performance", 1),
                ("unit-002", "correctness", 1),
            },
        )
        self.assertTrue(any("[inventory] discovered 2 review units" in message for message in progress))
        self.assertTrue(any("[review 1/1] scheduled 6 tasks" in message for message in progress))

    async def test_repeat_run_receives_only_prior_results_for_the_same_task(self) -> None:
        scope = DiffScope(
            repo=Path("/tmp/example"),
            base="base",
            head="head",
            files=("app.py",),
            changed_lines={"app.py": frozenset({10})},
        )
        runner = FanoutCodexRunner(return_security_candidate=True)

        result = await ReviewPipeline(runner, repeat_runs=2).run(scope)

        self.assertEqual(result.review_tasks, 6)
        second_security = next(
            prior
            for unit_id, category, repeat_run, prior in runner.review_calls
            if unit_id == "unit-001" and category == "security" and repeat_run == 2
        )
        second_correctness = next(
            prior
            for unit_id, category, repeat_run, prior in runner.review_calls
            if unit_id == "unit-001" and category == "correctness" and repeat_run == 2
        )
        self.assertEqual(len(second_security), 1)
        self.assertEqual(second_correctness, ())

    async def test_inventory_omission_creates_fallback_unit_for_every_uncovered_file(self) -> None:
        scope = DiffScope(
            repo=Path("/tmp/example"),
            base="base",
            head="head",
            files=("api.py", "db.py"),
            changed_lines={"api.py": frozenset({10}), "db.py": frozenset({20})},
        )
        runner = OmittingInventoryRunner()

        result = await ReviewPipeline(runner).run(scope)

        self.assertEqual(result.review_tasks, 6)
        self.assertEqual(result.review_units[-1].label, "Unclassified changed files")
        self.assertEqual(result.review_units[-1].changed_files, ("db.py",))
        self.assertEqual(result.failed_tasks, ())
        self.assertTrue(any("inventory did not classify changed files: db.py" in gap for gap in result.coverage_gaps))

    async def test_all_review_failures_include_root_causes(self) -> None:
        scope = DiffScope(
            repo=Path("/tmp/example"),
            base="base",
            head="head",
            files=("app.py",),
            changed_lines={"app.py": frozenset({10})},
        )

        with self.assertRaisesRegex(PipelineError, "security unavailable"):
            await ReviewPipeline(FailingCodexRunner()).run(scope)

    async def test_empty_diff_does_not_require_codex_executable(self) -> None:
        scope = DiffScope(
            repo=Path("/tmp/example"),
            base="base",
            head="head",
            files=(),
            changed_lines={},
        )
        runner = CodexRunner(scope.repo, executable="codex-executable-that-does-not-exist")

        result = await ReviewPipeline(runner).run(scope)

        self.assertEqual(result.review_tasks, 0)
        self.assertEqual(result.findings, ())

    async def test_partial_review_preserves_successes_and_records_incomplete_coverage(self) -> None:
        scope = DiffScope(
            repo=Path("/tmp/example"),
            base="base",
            head="head",
            files=("app.py",),
            changed_lines={"app.py": frozenset({10})},
        )

        result = await ReviewPipeline(PartialCodexRunner()).run(scope)

        self.assertEqual(result.review_tasks, 3)
        self.assertEqual(result.findings, ())
        self.assertEqual(len(result.failed_tasks), 1)
        self.assertIn("review:unit-001-flow-001:security:repeat-1: security timed out", result.failed_tasks[0])

    async def test_pipeline_filters_non_diff_findings_and_deduplicates(self) -> None:
        scope = DiffScope(
            repo=Path("/tmp/example"),
            base="base",
            head="head",
            files=("app.py",),
            changed_lines={"app.py": frozenset({10, 11})},
        )
        runner = FakeCodexRunner()
        pipeline = ReviewPipeline(runner, concurrency=3)

        result = await pipeline.run(scope)

        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.findings[0].severity, "high")
        self.assertEqual(result.findings[0].line, 10)
        self.assertEqual(result.review_tasks, 3)
        self.assertEqual(result.verification_tasks, 1)
        self.assertEqual(len(runner.verification_calls), 1)
        self.assertEqual(result.failed_tasks, ())

    async def test_candidate_limit_marks_coverage_incomplete(self) -> None:
        scope = DiffScope(
            repo=Path("/tmp/example"),
            base="base",
            head="head",
            files=("app.py",),
            changed_lines={"app.py": frozenset(range(1, 52))},
        )

        result = await ReviewPipeline(ManyCandidatesRunner()).run(scope)

        self.assertEqual(result.candidate_count, 51)
        self.assertEqual(result.verification_tasks, 50)
        self.assertEqual(len(result.failed_tasks), 1)
        self.assertIn("omitted 1 eligible candidates", result.failed_tasks[0])


class FanoutCodexRunner:
    def __init__(self, *, return_security_candidate: bool = False) -> None:
        self.return_security_candidate = return_security_candidate
        self.review_calls: list[tuple[str, str, int, tuple[Candidate, ...]]] = []

    async def inventory(self, scope: DiffScope) -> list[ReviewUnit]:
        return [
            ReviewUnit(f"unit-{index:03d}", path, (path,), f"Review changes in {path}.")
            for index, path in enumerate(scope.files, start=1)
        ]

    async def map_flows(self, scope: DiffScope, unit: ReviewUnit) -> list[ReviewFlow]:
        return [_mapped_flow(scope, unit)]

    async def review(
        self,
        scope: DiffScope,
        flow: ReviewFlow,
        mission: ReviewMission,
        repeat_run: int,
        prior_candidates: tuple[Candidate, ...],
    ) -> list[Candidate]:
        category = mission.name
        self.review_calls.append((flow.unit_id, category, repeat_run, prior_candidates))
        if not self.return_security_candidate or category != "security" or repeat_run != 1:
            return []
        return [
            Candidate(
                category="security",
                severity="medium",
                confidence=0.9,
                file_path=flow.changed_locations[0].file_path,
                line=10,
                summary="Missing authorization",
                explanation="The changed handler skips authorization.",
                evidence=(f"{flow.changed_locations[0].file_path}:10",),
                suggested_fix="Add authorization.",
            )
        ]

    async def verify(self, scope: DiffScope, candidate: Candidate) -> Finding:
        return Finding(
            **candidate.to_dict(),
            verification="Not confirmed.",
            confirmed=False,
            introduced_by_diff=True,
            actionable=True,
        )


class OmittingInventoryRunner(FanoutCodexRunner):
    async def inventory(self, scope: DiffScope) -> list[ReviewUnit]:
        return [ReviewUnit("unit-001", "API flow", ("api.py",), "Review the API flow.")]


class DeduplicateAndRankTest(unittest.TestCase):
    def test_verification_prompt_escapes_non_utf8_path(self) -> None:
        file_path = b"odd-\xff.py".decode("utf-8", errors="surrogateescape")
        candidate = Candidate(
            category="correctness",
            severity="medium",
            confidence=0.9,
            file_path=file_path,
            line=1,
            summary="Broken path",
            explanation="The path is mishandled.",
            evidence=("The path came from Git.",),
            suggested_fix="Preserve the path.",
        )
        scope = DiffScope(Path("/tmp/example"), "base", "head", (file_path,), {file_path: frozenset({1})})

        prompt = verification_prompt(scope, candidate)

        self.assertIn("\\udcff", prompt)
        prompt.encode("utf-8")

    def test_candidate_preserves_posix_backslash_path(self) -> None:
        candidate = Candidate.from_dict(
            {
                "category": "correctness",
                "severity": "medium",
                "confidence": 0.9,
                "file_path": "odd\\name.py",
                "line": 1,
                "summary": "Broken path",
                "explanation": "The path must stay exact.",
                "evidence": ["odd\\name.py:1"],
                "suggested_fix": "Preserve the path.",
            },
            expected_category="correctness",
        )

        self.assertEqual(candidate.file_path, "odd\\name.py")

    def test_candidate_preserves_significant_path_whitespace(self) -> None:
        candidate = Candidate.from_dict(
            {
                "category": "correctness",
                "severity": "medium",
                "confidence": 0.9,
                "file_path": " leading and trailing.py ",
                "line": 1,
                "summary": "Broken path",
                "explanation": "The path must stay exact.",
                "evidence": [" leading and trailing.py :1"],
                "suggested_fix": "Preserve the path.",
            },
            expected_category="correctness",
        )

        self.assertEqual(candidate.file_path, " leading and trailing.py ")

    def test_keeps_distinct_findings_and_prefers_higher_severity_duplicate(self) -> None:
        summary = "Project deletion skips the ownership check"
        lower = self._finding("security", "medium", 0.85, 10, summary)
        higher = self._finding("correctness", "high", 0.90, 10, summary)
        distinct = self._finding("performance", "medium", 0.80, 18, "List endpoint performs one query per project")

        findings = deduplicate_and_rank([lower, distinct, higher])

        self.assertEqual(findings, (higher, distinct))

    def test_keeps_independent_findings_on_the_same_line(self) -> None:
        authorization = self._finding("security", "high", 0.90, 10, "Missing authorization before deletion")
        blocking_io = self._finding("performance", "medium", 0.85, 10, "Synchronous filesystem scan blocks requests")

        findings = deduplicate_and_rank([authorization, blocking_io])

        self.assertEqual(findings, (authorization, blocking_io))

    def test_keeps_findings_with_opposite_operators_on_the_same_line(self) -> None:
        less_than = self._finding("correctness", "high", 0.90, 10, "Reject when count < limit")
        greater_than = self._finding("correctness", "medium", 0.85, 10, "Reject when count > limit")

        findings = deduplicate_and_rank([less_than, greater_than])

        self.assertEqual(findings, (less_than, greater_than))

    def test_keeps_distinct_chinese_findings_on_the_same_line(self) -> None:
        authorization = self._finding("security", "high", 0.90, 10, "删除前缺少权限校验")
        blocking_io = self._finding("performance", "medium", 0.85, 10, "同步文件扫描阻塞请求")

        findings = deduplicate_and_rank([authorization, blocking_io])

        self.assertEqual(findings, (authorization, blocking_io))

    def test_keeps_similarly_worded_findings_on_adjacent_lines(self) -> None:
        authorization = self._finding("security", "high", 0.90, 10, "Missing authorization check for deletion")
        ownership = self._finding("correctness", "medium", 0.85, 11, "Missing ownership check for deletion")

        findings = deduplicate_and_rank([authorization, ownership])

        self.assertEqual(findings, (authorization, ownership))

    def test_scope_prefers_exact_backslash_path_before_compatibility_form(self) -> None:
        scope = DiffScope(
            repo=Path("/tmp/example"),
            base="base",
            head="head",
            files=("src/app.py", "src\\app.py"),
            changed_lines={"src/app.py": frozenset({1}), "src\\app.py": frozenset({2})},
        )

        self.assertTrue(scope.contains_changed_line("src\\app.py", 2))
        self.assertFalse(scope.contains_changed_line("src\\app.py", 1))

    @staticmethod
    def _finding(category: str, severity: str, confidence: float, line: int, summary: str) -> Finding:
        return Finding(
            category=category,
            severity=severity,
            confidence=confidence,
            file_path="app.py",
            line=line,
            summary=summary,
            explanation=summary,
            evidence=(f"app.py:{line}",),
            suggested_fix="Fix it.",
            verification="Confirmed.",
            confirmed=True,
            introduced_by_diff=True,
            actionable=True,
        )


if __name__ == "__main__":
    unittest.main()
