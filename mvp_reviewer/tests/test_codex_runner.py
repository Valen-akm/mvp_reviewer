import contextlib
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from mvp_reviewer.codex_runner import CodexExecutionError, CodexRunner, _shell_environment_config
from mvp_reviewer.git_diff import DiffScope
from mvp_reviewer.models import ChangedLocation, ReviewFlow, ReviewMission, ReviewUnit

TEST_REPO = Path("/tmp/example")
TEST_UNIT = ReviewUnit("unit-001", "Application", ("app.py",), "Review changed application behavior.")
TEST_MISSION = ReviewMission("unit-001-flow-001-mission-001", "contract-correctness", "Check the public contract.")
TEST_FLOW = ReviewFlow(
    id="unit-001-flow-001",
    unit_id=TEST_UNIT.id,
    label="Application request flow",
    entrypoint="changed handler",
    actor="application caller",
    controlled_inputs=("request",),
    preconditions=("handler is reached",),
    trace=("app.py:10 handler - processes request",),
    terminal_effect="returns a response",
    invariants=("the public contract remains valid",),
    changed_locations=(ChangedLocation("app.py", 10, "handler"),),
    missions=(TEST_MISSION,),
)


class RecordingCodexRunner(CodexRunner):
    def __init__(self) -> None:
        super().__init__(Path("/tmp/example"), executable="codex-test")
        self.command: list[str] = []
        self.prompt = ""

    async def _invoke(self, command: list[str], prompt: str) -> dict[str, Any]:
        self.command = command
        self.prompt = prompt
        if any(item.endswith("review_units.json") for item in command):
            return {
                "review_units": [
                    {
                        "label": "Application",
                        "changed_files": ["app.py"],
                        "review_focus": "Review changed application behavior.",
                    }
                ]
            }
        if any(item.endswith("review_flows.json") for item in command):
            return {
                "review_flows": [
                    {
                        "label": "Application request flow",
                        "entrypoint": "changed handler",
                        "actor": "application caller",
                        "controlled_inputs": ["request"],
                        "preconditions": ["handler is reached"],
                        "trace": ["app.py:10 handler - processes request"],
                        "terminal_effect": "returns a response",
                        "invariants": ["the public contract remains valid"],
                        "changed_locations": [{"file_path": "app.py", "line": 10, "symbol": "handler"}],
                        "missions": [{"name": "contract-correctness", "objective": "Check the public contract."}],
                    }
                ]
            }
        return {"findings": []}


class CodexRunnerTest(unittest.IsolatedAsyncioTestCase):
    @patch("mvp_reviewer.codex_runner.os.name", "nt")
    async def test_non_posix_platform_fails_before_starting_codex(self) -> None:
        scope = DiffScope(TEST_REPO, "base", "head", ("app.py",), {"app.py": frozenset({1})})
        runner = CodexRunner(scope.repo, executable="codex-test")

        with self.assertRaisesRegex(CodexExecutionError, "requires a POSIX platform"):
            await runner.review(scope, TEST_FLOW, TEST_MISSION, 1, ())

    async def test_inventory_uses_structured_review_unit_schema(self) -> None:
        scope = DiffScope(TEST_REPO, "base", "head", ("app.py",), {"app.py": frozenset({1})})
        runner = RecordingCodexRunner()

        units = await runner.inventory(scope)

        self.assertEqual(units, [TEST_UNIT])
        self.assertTrue(any(item.endswith("review_units.json") for item in runner.command))
        self.assertIn("every changed file must appear in at least one unit", runner.prompt)

    async def test_focused_review_uses_read_only_exec_with_custom_prompt(self) -> None:
        scope = DiffScope(
            repo=Path("/tmp/example"),
            base="base",
            head="head",
            files=("app.py",),
            changed_lines={"app.py": frozenset({10})},
        )
        runner = RecordingCodexRunner()

        findings = await runner.review(scope, TEST_FLOW, TEST_MISSION, 1, ())

        self.assertEqual(findings, [])
        self.assertEqual(runner.command[:3], ["codex-test", "exec", "--ephemeral"])
        self.assertNotIn("review", runner.command)
        self.assertNotIn("--sandbox", runner.command)
        self.assertIn("project_doc_max_bytes=0", runner.command)
        self.assertIn("mcp_servers={}", runner.command)
        self.assertIn('default_permissions="mvp-review"', runner.command)
        self.assertIn('approval_policy="never"', runner.command)
        self.assertIn("permissions.mvp-review.network={enabled = false}", runner.command)
        self.assertIn('shell_environment_policy.inherit="none"', runner.command)
        self.assertIn("allow_login_shell=false", runner.command)
        self.assertIn("--ignore-rules", runner.command)
        self.assertEqual(runner.command.count("--disable"), 3)

    async def test_flow_mapping_uses_structured_dynamic_mission_schema(self) -> None:
        scope = DiffScope(TEST_REPO, "base", "head", ("app.py",), {"app.py": frozenset({10})})
        runner = RecordingCodexRunner()

        flows = await runner.map_flows(scope, TEST_UNIT)

        self.assertEqual(flows, [TEST_FLOW])
        self.assertTrue(any(item.endswith("review_flows.json") for item in runner.command))
        self.assertIn("Choose missions from the actual flow", runner.prompt)

    def test_shell_environment_policy_does_not_copy_caller_secrets(self) -> None:
        config = _shell_environment_config(
            TEST_REPO,
            {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "OPEN_KRITT_SENTINEL_SECRET": "do-not-copy"},
        )

        self.assertIn('inherit = "none"', config)
        self.assertIn('PATH = "/usr/bin:/bin"', config)
        self.assertNotIn("OPEN_KRITT_SENTINEL_SECRET", config)
        self.assertNotIn("do-not-copy", config)

    @unittest.skipUnless(os.name == "posix", "process-group termination requires POSIX")
    async def test_timeout_terminates_wrapper_and_child_processes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            executable = repo / "slow-codex"
            executable.write_text("#!/bin/sh\nsleep 60 &\nwait\n", encoding="utf-8")
            executable.chmod(0o755)
            scope = DiffScope(repo, "base", "head", ("app.py",), {"app.py": frozenset({1})})
            runner = CodexRunner(repo, timeout_seconds=1, executable=str(executable))

            started = time.monotonic()
            with self.assertRaisesRegex(CodexExecutionError, "timed out after 1 seconds"):
                await runner.review(scope, TEST_FLOW, TEST_MISSION, 1, ())

            self.assertLess(time.monotonic() - started, 5)

    @unittest.skipUnless(os.name == "posix", "process-group termination requires POSIX")
    async def test_timeout_cleans_children_after_wrapper_exits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            child_file = repo / "child.pid"
            executable = repo / "detaching-codex"
            executable.write_text(
                f"#!/bin/sh\nsleep 60 &\necho $! > {child_file}\nexit 7\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            scope = DiffScope(repo, "base", "head", ("app.py",), {"app.py": frozenset({1})})
            runner = CodexRunner(repo, timeout_seconds=1, executable=str(executable))

            with self.assertRaisesRegex(CodexExecutionError, "timed out after 1 seconds"):
                await runner.review(scope, TEST_FLOW, TEST_MISSION, 1, ())

            child_pid = child_file.read_text(encoding="utf-8").strip()
            state = subprocess.run(
                ["ps", "-o", "stat=", "-p", child_pid],
                check=False,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertTrue(not state or state.startswith("Z"), f"child process remained active with state {state}")

    @unittest.skipUnless(os.name == "posix", "detached process test requires POSIX")
    async def test_timeout_does_not_wait_for_detached_pipe_holder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            child_file = repo / "child.pid"
            executable = repo / "detached-codex"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import os\n"
                "import time\n"
                f"child_file = {str(child_file)!r}\n"
                "child_pid = os.fork()\n"
                "if child_pid == 0:\n"
                "    os.setsid()\n"
                "    time.sleep(60)\n"
                "else:\n"
                "    with open(child_file, 'w', encoding='utf-8') as output:\n"
                "        output.write(str(child_pid))\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            scope = DiffScope(repo, "base", "head", ("app.py",), {"app.py": frozenset({1})})
            runner = CodexRunner(repo, timeout_seconds=1, executable=str(executable))
            child_pid: int | None = None
            try:
                started = time.monotonic()
                with self.assertRaisesRegex(CodexExecutionError, "timed out after 1 seconds"):
                    await runner.review(scope, TEST_FLOW, TEST_MISSION, 1, ())
                self.assertLess(time.monotonic() - started, 5)
                child_pid = int(child_file.read_text(encoding="utf-8"))
            finally:
                if child_pid is not None:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(child_pid, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
