import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from mvp_reviewer.__main__ import (
    EXIT_FINDINGS,
    EXIT_OPERATIONAL_ERROR,
    EXIT_SUCCESS,
    gate_exit_code,
    main,
)
from mvp_reviewer.models import Finding, ReviewResult, RootCause


class DeliveryGateTest(unittest.TestCase):
    def test_gate_blocks_findings_at_or_above_threshold(self) -> None:
        result = self._result(self._finding("high"))

        self.assertEqual(gate_exit_code(result, fail_on="critical", require_complete=False), EXIT_SUCCESS)
        self.assertEqual(gate_exit_code(result, fail_on="high", require_complete=False), EXIT_FINDINGS)
        self.assertEqual(gate_exit_code(result, fail_on="medium", require_complete=False), EXIT_FINDINGS)
        self.assertEqual(gate_exit_code(result, fail_on="none", require_complete=False), EXIT_SUCCESS)

    def test_gate_uses_root_cause_severity(self) -> None:
        root_cause = RootCause(
            id="root-001",
            summary="Combined impact is critical",
            explanation="The verified symptoms share one critical root cause.",
            severity="critical",
            confidence=0.9,
            file_path="app.py",
            line=10,
            finding_ids=("finding-001",),
            evidence=("app.py:10",),
            suggested_fix="Fix the root cause.",
        )
        result = self._result(root_causes=(root_cause,))

        self.assertEqual(gate_exit_code(result, fail_on="critical", require_complete=False), EXIT_FINDINGS)

    def test_gate_treats_incomplete_coverage_as_operational_failure(self) -> None:
        result = self._result(self._finding("critical"), failures=("review:security: timed out",))
        semantic_gap = self._result(self._finding("critical"), coverage_gaps=("flow-map omitted helper.py",))

        self.assertEqual(
            gate_exit_code(result, fail_on="critical", require_complete=True),
            EXIT_OPERATIONAL_ERROR,
        )
        self.assertEqual(
            gate_exit_code(semantic_gap, fail_on="none", require_complete=True),
            EXIT_OPERATIONAL_ERROR,
        )
        self.assertEqual(gate_exit_code(result, fail_on="none", require_complete=False), EXIT_SUCCESS)

    @staticmethod
    def _result(
        *findings: Finding,
        failures: tuple[str, ...] = (),
        coverage_gaps: tuple[str, ...] = (),
        root_causes: tuple[RootCause, ...] = (),
    ) -> ReviewResult:
        return ReviewResult(
            base="base",
            head="head",
            files=("app.py",),
            findings=findings,
            review_tasks=3,
            candidate_count=len(findings),
            verification_tasks=len(findings),
            failed_tasks=failures,
            coverage_gaps=coverage_gaps,
            root_causes=root_causes,
        )

    @staticmethod
    def _finding(severity: str) -> Finding:
        return Finding(
            category="correctness",
            severity=severity,
            confidence=0.9,
            file_path="app.py",
            line=10,
            summary="Broken invariant",
            explanation="The changed line breaks an invariant.",
            evidence=("app.py:10",),
            suggested_fix="Preserve the invariant.",
            verification="Confirmed from the caller.",
            confirmed=True,
            introduced_by_diff=True,
            actionable=True,
        )


class CliIntegrationTest(unittest.TestCase):
    def test_empty_diff_completes_without_invoking_codex(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            output = Path(directory) / "output"
            repo.mkdir()
            self._git(repo, "init", "--quiet")
            self._git(repo, "config", "user.email", "reviewer@example.com")
            self._git(repo, "config", "user.name", "Reviewer Test")
            (repo / "README.md").write_text("fixture\n", encoding="utf-8")
            self._git(repo, "add", "README.md")
            self._git(repo, "commit", "--quiet", "-m", "base")

            exit_code = main(
                [
                    "--repo",
                    str(repo),
                    "--base",
                    "HEAD",
                    "--output",
                    str(output),
                    "--require-complete",
                    "--trusted-target",
                ]
            )
            payload = json.loads((output / "findings.json").read_text(encoding="utf-8"))

            self.assertEqual(exit_code, EXIT_SUCCESS)
            self.assertEqual(payload["status"], "complete")
            self.assertEqual(payload["files"], [])

    def test_operational_error_returns_two_and_writes_failure_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing_repo = Path(directory) / "missing"
            output = Path(directory) / "output"

            exit_code = main(
                [
                    "--repo",
                    str(missing_repo),
                    "--base",
                    "HEAD",
                    "--output",
                    str(output),
                    "--trusted-target",
                ]
            )
            payload = json.loads((output / "findings.json").read_text(encoding="utf-8"))

            self.assertEqual(exit_code, EXIT_OPERATIONAL_ERROR)
            self.assertEqual(payload["status"], "failed")
            self.assertIn("repository directory does not exist", payload["error"])

    def test_local_untrusted_target_fails_before_git_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"

            exit_code = main(["--repo", str(Path(directory) / "missing"), "--base", "HEAD", "--output", str(output)])
            payload = json.loads((output / "findings.json").read_text(encoding="utf-8"))

            self.assertEqual(exit_code, EXIT_OPERATIONAL_ERROR)
            self.assertIn("pass --trusted-target only for repositories you trust", payload["error"])

    @staticmethod
    def _git(repo: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
