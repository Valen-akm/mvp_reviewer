import json
import tempfile
import unittest
from pathlib import Path

from mvp_reviewer.models import Finding, ReviewResult, RootCause
from mvp_reviewer.report import write_error_report, write_report


class ReportTest(unittest.TestCase):
    def test_complete_report_has_versioned_machine_contract(self) -> None:
        result = ReviewResult("base", "head", (), (), 0, 0, 0, ())

        with tempfile.TemporaryDirectory() as directory:
            json_path, markdown_path = write_report(result, Path(directory))
            payload = json.loads(json_path.read_text(encoding="utf-8"))

            self.assertEqual(payload["schema_version"], 3)
            self.assertEqual(payload["status"], "complete")
            self.assertTrue(payload["summary"]["complete"])
            self.assertIn("Confirmed findings: 0", markdown_path.read_text(encoding="utf-8"))

    def test_operational_failure_still_writes_delivery_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            json_path, markdown_path = write_error_report("Codex timed out", Path(directory))
            payload = json.loads(json_path.read_text(encoding="utf-8"))

            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["error"], "Codex timed out")
            self.assertIn("Review failed", markdown_path.read_text(encoding="utf-8"))

    def test_markdown_leads_with_semantic_root_causes(self) -> None:
        root_cause = RootCause(
            id="root-001",
            summary="Shared query omits tenant filtering",
            explanation="Two endpoint symptoms share the same query construction defect.",
            severity="high",
            confidence=0.92,
            file_path="app.py",
            line=10,
            finding_ids=("finding-001", "finding-002"),
            evidence=("app.py:10 constructs the shared query",),
            suggested_fix="Add the tenant predicate to the shared query.",
        )
        result = ReviewResult("base", "head", ("app.py",), (), 2, 2, 2, (), root_causes=(root_cause,))

        with tempfile.TemporaryDirectory() as directory:
            _json_path, markdown_path = write_report(result, Path(directory))
            markdown = markdown_path.read_text(encoding="utf-8")

            self.assertIn("Root causes: 1", markdown)
            self.assertIn("Shared query omits tenant filtering", markdown)
            self.assertIn("`finding-001`, `finding-002`", markdown)

    def test_reports_escape_non_utf8_git_paths(self) -> None:
        path = "bad\udcff.py"
        finding = Finding(
            category="correctness",
            severity="medium",
            confidence=0.9,
            file_path=path,
            line=1,
            summary="Invalid path handling",
            explanation="The path is reported safely.",
            evidence=(f"{path}:1",),
            suggested_fix="Escape the path.",
            verification="Confirmed.",
            confirmed=True,
            introduced_by_diff=True,
            actionable=True,
        )
        result = ReviewResult("base", "head", (path,), (finding,), 3, 1, 1, ())

        with tempfile.TemporaryDirectory() as directory:
            json_path, markdown_path = write_report(result, Path(directory))
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            markdown = markdown_path.read_text(encoding="utf-8")

            self.assertEqual(payload["files"], [path])
            self.assertEqual(payload["findings"][0]["finding_id"], "finding-001")
            self.assertEqual(payload["findings"][0]["file_path"], path)
            self.assertIn(r"bad\udcff.py", markdown)


if __name__ == "__main__":
    unittest.main()
