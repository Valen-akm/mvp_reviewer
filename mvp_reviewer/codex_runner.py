import asyncio
import json
import os
import shutil
import signal
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from mvp_reviewer.git_diff import DiffScope
from mvp_reviewer.models import Candidate, Finding, ModelOutputError, ReviewFlow, ReviewMission, ReviewUnit, RootCause
from mvp_reviewer.prompts import (
    aggregation_prompt,
    flow_mapping_prompt,
    inventory_prompt,
    review_prompt,
    verification_prompt,
)

SCHEMA_DIR = Path(__file__).with_name("schemas")
PROCESS_CLEANUP_TIMEOUT_SECONDS = 2


class CodexExecutionError(RuntimeError):
    """Raised when a Codex subprocess fails or returns invalid output."""


class CodexRunner:
    """Thin asynchronous adapter around read-only `codex exec` calls."""

    def __init__(self, repo: Path, *, timeout_seconds: int = 900, executable: str = "codex") -> None:
        self.repo = repo
        self.timeout_seconds = timeout_seconds
        self.executable = executable

    async def inventory(self, scope: DiffScope) -> list[ReviewUnit]:
        """Map a diff into logical review units before focused analysis."""
        payload = await self._invoke(
            self._command(SCHEMA_DIR / "review_units.json"),
            inventory_prompt(scope),
        )
        raw_units = payload.get("review_units")
        if not isinstance(raw_units, list):
            raise CodexExecutionError("Codex inventory output is missing the review_units array")
        try:
            scope_files = frozenset(scope.files)
            return [
                ReviewUnit.from_dict(item, index=index, scope_files=scope_files)
                for index, item in enumerate(raw_units, 1)
            ]
        except ModelOutputError as exc:
            raise CodexExecutionError(f"invalid Codex inventory output: {exc}") from exc

    async def review(
        self,
        scope: DiffScope,
        flow: ReviewFlow,
        mission: ReviewMission,
        repeat_run: int,
        prior_candidates: tuple[Candidate, ...],
    ) -> list[Candidate]:
        """Run one focused Codex pass for a mapped flow and mission."""
        payload = await self._invoke(
            self._command(SCHEMA_DIR / "candidates.json"),
            review_prompt(scope, flow, mission, repeat_run=repeat_run, prior_candidates=prior_candidates),
        )
        raw_findings = payload.get("findings")
        if not isinstance(raw_findings, list):
            raise CodexExecutionError("Codex review output is missing the findings array")
        try:
            return [Candidate.from_dict(item) for item in raw_findings]
        except ModelOutputError as exc:
            raise CodexExecutionError(f"invalid Codex review output: {exc}") from exc

    async def map_flows(self, scope: DiffScope, unit: ReviewUnit) -> list[ReviewFlow]:
        """Map one logical unit into concrete flows and dynamic review missions."""
        payload = await self._invoke(
            self._command(SCHEMA_DIR / "review_flows.json"),
            flow_mapping_prompt(scope, unit),
        )
        raw_flows = payload.get("review_flows")
        if not isinstance(raw_flows, list):
            raise CodexExecutionError("Codex flow mapping output is missing the review_flows array")
        try:
            return [
                ReviewFlow.from_dict(
                    item,
                    index=index,
                    unit=unit,
                    contains_changed_line=scope.contains_changed_line,
                )
                for index, item in enumerate(raw_flows, 1)
            ]
        except ModelOutputError as exc:
            raise CodexExecutionError(f"invalid Codex flow mapping output: {exc}") from exc

    async def verify(self, scope: DiffScope, candidate: Candidate) -> Finding:
        """Run an independent read-only verification pass for one candidate."""
        payload = await self._invoke(
            self._command(SCHEMA_DIR / "verification.json"),
            verification_prompt(scope, candidate),
        )
        try:
            return Finding.from_dict(payload, expected_category=candidate.category)
        except ModelOutputError as exc:
            raise CodexExecutionError(f"invalid Codex verification output: {exc}") from exc

    async def aggregate(self, scope: DiffScope, findings: tuple[Finding, ...]) -> list[RootCause]:
        """Consume every verified finding and cluster them into semantic root causes."""
        payload = await self._invoke(
            self._command(SCHEMA_DIR / "root_causes.json"),
            aggregation_prompt(scope, findings),
        )
        raw_root_causes = payload.get("root_causes")
        if not isinstance(raw_root_causes, list):
            raise CodexExecutionError("Codex aggregation output is missing the root_causes array")
        expected_ids = frozenset(f"finding-{index:03d}" for index in range(1, len(findings) + 1))
        try:
            root_causes = [
                RootCause.from_dict(item, index=index, allowed_finding_ids=expected_ids)
                for index, item in enumerate(raw_root_causes, 1)
            ]
        except ModelOutputError as exc:
            raise CodexExecutionError(f"invalid Codex aggregation output: {exc}") from exc
        seen_ids = [finding_id for root_cause in root_causes for finding_id in root_cause.finding_ids]
        missing = sorted(expected_ids - set(seen_ids))
        duplicates = sorted({finding_id for finding_id in seen_ids if seen_ids.count(finding_id) > 1})
        if missing or duplicates:
            raise CodexExecutionError(
                f"Codex aggregation must partition every finding exactly once; missing={missing}, duplicates={duplicates}"
            )
        invalid_anchor = next(
            (
                f"{root_cause.file_path}:{root_cause.line}"
                for root_cause in root_causes
                if not scope.contains_changed_line(root_cause.file_path, root_cause.line)
            ),
            None,
        )
        if invalid_anchor:
            raise CodexExecutionError(f"Codex root cause is not anchored to the diff: {invalid_anchor}")
        return root_causes

    def _command(self, output_schema: Path) -> list[str]:
        """Build a deterministic command that ignores instructions from the review target."""
        return [
            self.executable,
            "exec",
            "--ephemeral",
            "--ignore-rules",
            "--disable",
            "hooks",
            "--disable",
            "plugins",
            "--disable",
            "skill_search",
            "--config",
            "project_doc_max_bytes=0",
            "--config",
            "mcp_servers={}",
            "--config",
            _filesystem_permission_config(),
            "--config",
            "permissions.mvp-review.network={enabled = false}",
            "--config",
            'default_permissions="mvp-review"',
            "--config",
            'approval_policy="never"',
            "--config",
            'shell_environment_policy.inherit="none"',
            "--config",
            "allow_login_shell=false",
            "--output-schema",
            str(output_schema),
        ]

    async def _invoke(self, command: list[str], prompt: str) -> dict[str, Any]:
        if os.name != "posix":
            raise CodexExecutionError("Codex review requires a POSIX platform for process-tree isolation")
        if shutil.which(self.executable) is None:
            raise CodexExecutionError(f"{self.executable!r} executable was not found")
        output_descriptor, output_name = tempfile.mkstemp(prefix="codex-review-", suffix=".json")
        os.close(output_descriptor)
        output_path = Path(output_name)
        runtime_directory = tempfile.TemporaryDirectory(prefix="codex-review-runtime-")
        runtime_path = Path(runtime_directory.name)
        environment = os.environ.copy()
        environment["GIT_CONFIG_GLOBAL"] = os.devnull
        environment["TMPDIR"] = str(runtime_path)
        developer_dir = Path("/Library/Developer/CommandLineTools")
        if developer_dir.is_dir():
            environment["DEVELOPER_DIR"] = str(developer_dir)
        full_command = [
            *command,
            "--config",
            _filesystem_permission_config(runtime_path),
            "--config",
            _shell_environment_config(runtime_path, environment),
            "--output-last-message",
            str(output_path),
            "-",
        ]
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *full_command,
                cwd=self.repo,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                env=environment,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(prompt.encode("utf-8")),
                    timeout=self.timeout_seconds,
                )
            except TimeoutError as exc:
                raise CodexExecutionError(f"Codex timed out after {self.timeout_seconds} seconds") from exc
            if process.returncode != 0:
                detail = stderr.decode("utf-8", errors="replace").strip()
                raise CodexExecutionError(detail[-3000:] or f"Codex exited with status {process.returncode}")
            output = output_path.read_text(encoding="utf-8").strip()
            if not output:
                output = stdout.decode("utf-8", errors="replace").strip()
            try:
                payload = json.loads(output)
            except json.JSONDecodeError as exc:
                raise CodexExecutionError("Codex did not return valid JSON") from exc
            if not isinstance(payload, dict):
                raise CodexExecutionError("Codex output must be a JSON object")
            return payload
        finally:
            if process is not None:
                await _terminate_process(process)
            runtime_directory.cleanup()
            output_path.unlink(missing_ok=True)


def _filesystem_permission_config(scratch: Path | None = None) -> str:
    entries = [
        '":minimal" = "read"',
        '"/opt/homebrew" = "read"',
        '"/usr/local" = "read"',
        '"/Library/Developer/CommandLineTools" = "read"',
    ]
    if scratch is not None:
        entries.append(f'{json.dumps(str(scratch))} = "write"')
    entries.append('":workspace_roots" = { "." = "read" }')
    return f"permissions.mvp-review.filesystem={{{', '.join(entries)}}}"


def _shell_environment_config(scratch: Path, environment: dict[str, str]) -> str:
    values = {
        "PATH": environment.get("PATH", os.defpath),
        "HOME": str(scratch),
        "TMPDIR": str(scratch),
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    developer_dir = Path("/Library/Developer/CommandLineTools")
    if developer_dir.is_dir():
        values["DEVELOPER_DIR"] = str(developer_dir)
    if "LANG" in environment:
        values["LANG"] = environment["LANG"]
    assignments = ", ".join(f"{key} = {json.dumps(value)}" for key, value in values.items())
    return f'shell_environment_policy={{inherit = "none", set = {{{assignments}}}}}'


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """Terminate the original process group without waiting on detached descendants."""
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    try:
        await asyncio.wait_for(process.wait(), timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS)
    except TimeoutError:
        pass
    finally:
        if process.stdin is not None:
            process.stdin.close()
        for reader in (process.stdout, process.stderr):
            transport = getattr(reader, "_transport", None)
            if transport is not None:
                transport.close()
