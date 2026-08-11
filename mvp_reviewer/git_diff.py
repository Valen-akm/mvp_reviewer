import os
import re
import selectors
import shutil
import signal
import subprocess
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
PATCH_HEADER_PREFIXES = (b"diff --git ", b"--- ", b"+++ ", b"@@ ")
MAX_PATCH_BYTES = 25 * 1024 * 1024
MAX_CHANGED_LINES = 100_000
MAX_DELETION_CONTEXT_BYTES = 25 * 1024 * 1024
MAX_DELETION_CONTEXT_FILES = 100
MAX_HEADER_BYTES = 64 * 1024
MAX_FILE_LIST_BYTES = 25 * 1024 * 1024
MAX_TREE_LIST_BYTES = 25 * 1024 * 1024
MAX_GIT_STDERR_BYTES = 1024 * 1024
GIT_DIFF_TIMEOUT_SECONDS = 120
GIT_COMMAND_TIMEOUT_SECONDS = 300
MAX_SNAPSHOT_COPY_BYTES = 2 * 1024 * 1024 * 1024
MAX_SNAPSHOT_WORKTREE_BYTES = 4 * 1024 * 1024 * 1024
MAX_SNAPSHOT_FILES = 200_000
GIT_PATH_ESCAPES = {
    ord('"'): ord('"'),
    ord("\\"): ord("\\"),
    ord("a"): 0x07,
    ord("b"): 0x08,
    ord("t"): 0x09,
    ord("n"): 0x0A,
    ord("v"): 0x0B,
    ord("f"): 0x0C,
    ord("r"): 0x0D,
}


class GitError(RuntimeError):
    """Raised when the requested Git review scope cannot be resolved."""


@dataclass(frozen=True, slots=True)
class DiffScope:
    """The deterministic base-to-HEAD scope reviewed by Codex."""

    repo: Path
    base: str
    head: str
    files: tuple[str, ...]
    changed_lines: dict[str, frozenset[int]]

    def contains_changed_line(self, file_path: str, line: int) -> bool:
        """Return whether a finding points at a changed line or deletion anchor."""
        normalized = file_path.removeprefix("./")
        if normalized in self.changed_lines:
            return line in self.changed_lines[normalized]
        compatibility_path = normalized.replace("\\", "/")
        return line in self.changed_lines.get(compatibility_path, frozenset())


def collect_diff_scope(repo: Path, base: str) -> DiffScope:
    """Resolve a merge-base diff and record every changed line in the HEAD version."""
    requested_repo = repo.expanduser().resolve()
    if not requested_repo.is_dir():
        raise GitError(f"repository directory does not exist: {requested_repo}")
    root = Path(_git(requested_repo, "rev-parse", "--show-toplevel")).resolve()
    base_commit = _git(root, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}")
    head = _git(root, "rev-parse", "HEAD")
    merge_base = _git(root, "merge-base", base_commit, head)
    diff_range = f"{merge_base}..{head}"
    raw_files = _git_diff_output(
        root,
        "-c",
        "core.quotepath=false",
        "diff",
        "--name-only",
        "-z",
        "--find-renames",
        "--ignore-submodules=none",
        diff_range,
        "--",
    )
    files = tuple(part.decode("utf-8", errors="surrogateescape") for part in raw_files.split(b"\0") if part)
    _reject_changed_gitlinks(root, diff_range)
    changed_lines = _changed_lines(root, diff_range, files, head)
    return DiffScope(repo=root, base=merge_base, head=head, files=files, changed_lines=changed_lines)


@contextmanager
def review_snapshot(scope: DiffScope) -> Iterator[Path]:
    """Yield a detached clone fixed at the resolved HEAD without reading live worktree changes."""
    alternates = Path(_git(scope.repo, "rev-parse", "--git-path", "objects/info/alternates"))
    if not alternates.is_absolute():
        alternates = scope.repo / alternates
    if alternates.is_file() and alternates.stat().st_size:
        raise GitError("alternate-backed repositories are not supported; repack the repository before review")
    _entry_count, tree_bytes = _snapshot_tree_usage(scope.repo, scope.head)
    object_directory = _git_path(scope.repo, "objects")
    with tempfile.TemporaryDirectory(prefix=".codex-review-snapshot-") as directory:
        snapshot = Path(directory) / "repo"
        clone_options = ["--local"]
        required_bytes = tree_bytes
        if not _same_device(object_directory, Path(directory)):
            object_bytes = _git_object_store_bytes(scope.repo)
            if object_bytes > MAX_SNAPSHOT_COPY_BYTES:
                raise GitError(
                    f"cross-filesystem snapshot requires {object_bytes} bytes, "
                    f"exceeding the {MAX_SNAPSHOT_COPY_BYTES}-byte copy limit; "
                    "set TMPDIR to a writable directory on the repository filesystem"
                )
            clone_options.append("--no-hardlinks")
            required_bytes += object_bytes
        free_bytes = shutil.disk_usage(directory).free
        if required_bytes > free_bytes:
            raise GitError(
                f"snapshot requires at least {required_bytes} bytes, but the target filesystem has "
                f"only {free_bytes} bytes free"
            )
        _run_git(
            scope.repo,
            "clone",
            "--quiet",
            "--no-checkout",
            *clone_options,
            "--",
            str(scope.repo),
            str(snapshot),
            text=True,
        )
        _git(snapshot, "update-ref", "--no-deref", "HEAD", scope.head)
        _git(snapshot, "read-tree", scope.head)
        _materialize_snapshot(snapshot, scope.head)
        try:
            _run_git(
                snapshot,
                "update-index",
                "--refresh",
                "--ignore-submodules",
                text=True,
            )
            _run_git(
                snapshot,
                "diff-files",
                "--quiet",
                "--no-ext-diff",
                "--no-textconv",
                "--",
                text=True,
            )
        except GitError as exc:
            raise GitError("materialized snapshot does not match the resolved HEAD") from exc
        yield snapshot


def _same_device(source: Path, destination: Path) -> bool:
    return source.stat().st_dev == destination.stat().st_dev


def _git_path(repo: Path, name: str) -> Path:
    path = Path(_git(repo, "rev-parse", "--git-path", name))
    return path.resolve() if path.is_absolute() else (repo / path).resolve()


def _git_object_store_bytes(repo: Path) -> int:
    sizes: dict[str, int] = {}
    for line in _git(repo, "count-objects", "-v").splitlines():
        key, separator, value = line.partition(": ")
        if separator and value.isdigit():
            sizes[key] = int(value)
    return 1024 * sum(sizes.get(key, 0) for key in ("size", "size-pack", "size-garbage"))


def _snapshot_tree_usage(repo: Path, head: str) -> tuple[int, int]:
    pending = bytearray()
    entry_count = 0
    total_bytes = 0

    def consume_stdout(chunk: bytes) -> None:
        nonlocal entry_count, total_bytes
        pending.extend(chunk)
        while True:
            terminator = pending.find(b"\0")
            if terminator < 0:
                if len(pending) > MAX_HEADER_BYTES:
                    raise GitError(f"git ls-tree entry exceeds the {MAX_HEADER_BYTES}-byte review limit")
                return
            record = bytes(pending[:terminator])
            del pending[: terminator + 1]
            metadata, separator, _path = record.partition(b"\t")
            fields = metadata.split()
            if not separator or len(fields) != 4:
                raise GitError("git ls-tree returned an invalid entry")
            entry_count += 1
            if entry_count > MAX_SNAPSHOT_FILES:
                raise GitError(f"HEAD tree exceeds the {MAX_SNAPSHOT_FILES}-file snapshot limit")
            if fields[1] == b"blob" and fields[3].isdigit():
                total_bytes += int(fields[3])
                if total_bytes > MAX_SNAPSHOT_WORKTREE_BYTES:
                    raise GitError(f"HEAD tree exceeds the {MAX_SNAPSHOT_WORKTREE_BYTES}-byte expanded snapshot limit")

    _stream_git_output(
        repo,
        "ls-tree",
        "-r",
        "-l",
        "-z",
        head,
        "--",
        operation="git ls-tree",
        max_stdout_bytes=MAX_TREE_LIST_BYTES,
        stdout_limit_message=f"tree listing exceeds the {MAX_TREE_LIST_BYTES}-byte review limit",
        consume_stdout=consume_stdout,
    )
    if pending:
        raise GitError("git ls-tree returned an unterminated entry")
    return entry_count, total_bytes


def _reject_changed_gitlinks(repo: Path, diff_range: str) -> None:
    raw_diff = _git_diff_output(
        repo,
        "diff",
        "--raw",
        "--no-abbrev",
        "-z",
        "--ignore-submodules=none",
        diff_range,
        "--",
    )
    records = raw_diff.split(b"\0")
    cursor = 0
    while cursor < len(records) and records[cursor]:
        metadata = records[cursor]
        fields = metadata[1:].split() if metadata.startswith(b":") else []
        if len(fields) != 5:
            raise GitError("git diff --raw returned an invalid entry")
        if b"160000" in fields[:2]:
            raise GitError("changed submodules are not supported by the MVP reviewer")
        cursor += 3 if fields[4].startswith((b"R", b"C")) else 2


def _materialize_snapshot(repo: Path, head: str) -> None:
    try:
        process = subprocess.Popen(
            ["git", "archive", "--format=tar", head],
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=_git_environment(),
        )
    except FileNotFoundError as exc:
        raise GitError("git executable was not found") from exc
    if process.stdout is None or process.stderr is None:
        _kill_process_group(process)
        process.wait()
        raise GitError("git archive did not expose output pipes")

    stderr = bytearray()
    stderr_exceeded = threading.Event()

    def consume_stderr() -> None:
        while chunk := process.stderr.read(64 * 1024):
            stderr.extend(chunk)
            if len(stderr) > MAX_GIT_STDERR_BYTES:
                stderr_exceeded.set()
                _kill_process_group(process)
                return

    stderr_thread = threading.Thread(target=consume_stderr, daemon=True)
    stderr_thread.start()
    timed_out = threading.Event()

    def terminate_on_timeout() -> None:
        if process.poll() is None:
            timed_out.set()
            _kill_process_group(process)

    timer = threading.Timer(GIT_COMMAND_TIMEOUT_SECONDS, terminate_on_timeout)
    timer.daemon = True
    timer.start()
    try:
        _extract_snapshot_archive(process.stdout, repo)
        return_code = process.wait()
    except BaseException as exc:
        _kill_process_group(process)
        process.wait()
        stderr_thread.join()
        process.stderr.close()
        if stderr_exceeded.is_set():
            raise GitError(f"git archive stderr exceeds the {MAX_GIT_STDERR_BYTES}-byte review limit") from exc
        if timed_out.is_set():
            raise GitError(f"git archive timed out after {GIT_COMMAND_TIMEOUT_SECONDS} seconds") from exc
        raise
    finally:
        timer.cancel()
        process.stdout.close()
    stderr_thread.join()
    process.stderr.close()
    if stderr_exceeded.is_set():
        raise GitError(f"git archive stderr exceeds the {MAX_GIT_STDERR_BYTES}-byte review limit")
    if timed_out.is_set():
        raise GitError(f"git archive timed out after {GIT_COMMAND_TIMEOUT_SECONDS} seconds")
    if return_code != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise GitError(detail or "git archive failed")


def _extract_snapshot_archive(stream: object, destination: Path) -> None:
    file_count = 0
    total_bytes = 0
    try:
        with tarfile.open(fileobj=stream, mode="r|", encoding="utf-8", errors="surrogateescape") as archive:
            for member in archive:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts:
                    raise GitError("git archive returned an unsafe path")
                target = destination.joinpath(*path.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not (member.isfile() or member.issym()):
                    raise GitError("git archive returned an unsupported entry type")
                file_count += 1
                if file_count > MAX_SNAPSHOT_FILES:
                    raise GitError(f"archive exceeds the {MAX_SNAPSHOT_FILES}-file snapshot limit")
                total_bytes += member.size
                if total_bytes > MAX_SNAPSHOT_WORKTREE_BYTES:
                    raise GitError(f"archive exceeds the {MAX_SNAPSHOT_WORKTREE_BYTES}-byte expanded snapshot limit")
                target.parent.mkdir(parents=True, exist_ok=True)
                if member.issym():
                    target.symlink_to(member.linkname)
                    continue
                source = archive.extractfile(member)
                if source is None:
                    raise GitError("git archive omitted file contents")
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(member.mode & 0o777)
    except tarfile.TarError as exc:
        raise GitError(f"git archive returned invalid tar data: {exc}") from exc


def _kill_process_group(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    if process.poll() is None:
        with suppress(ProcessLookupError):
            process.kill()


def _changed_lines(
    repo: Path,
    diff_range: str,
    files: tuple[str, ...],
    head: str,
) -> dict[str, frozenset[int]]:
    patch_headers = _git_patch_headers(
        repo,
        "-c",
        "core.quotepath=false",
        "diff",
        "--unified=0",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--find-renames",
        "--ignore-submodules=none",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        diff_range,
        "--",
    )
    changed_lines: dict[str, set[int]] = {file_path: set() for file_path in files}
    old_path: str | None = None
    current_path: str | None = None
    waiting_for_old_path = False
    waiting_for_new_path = False
    current_path_exists = False
    changed_line_count = 0
    deletion_after_anchors: dict[str, set[int]] = {}
    for patch_line in patch_headers:
        if patch_line.startswith("diff --git "):
            old_path = None
            current_path = None
            waiting_for_old_path = True
            waiting_for_new_path = False
            current_path_exists = False
            continue
        if waiting_for_old_path and patch_line.startswith("--- "):
            old_path = _patch_path(patch_line[4:])
            waiting_for_old_path = False
            waiting_for_new_path = True
            continue
        if waiting_for_new_path and patch_line.startswith("+++ "):
            new_path = _patch_path(patch_line[4:])
            current_path = new_path or old_path
            current_path_exists = new_path is not None
            waiting_for_new_path = False
            continue
        match = HUNK_HEADER_RE.match(patch_line)
        if not match or current_path not in changed_lines:
            continue
        old_count = int(match.group(1) or "1")
        start = int(match.group(2))
        new_count = int(match.group(3) or "1")
        changed_line_count += old_count + new_count
        if changed_line_count > MAX_CHANGED_LINES:
            raise GitError(f"text diff exceeds the {MAX_CHANGED_LINES}-changed-line review limit")
        if new_count == 0:
            # A deletion-only hunk has no added HEAD line. Keep a stable adjacent
            # anchor so regressions caused by removed checks remain reviewable.
            # start + 1 is not valid when the deletion is at end-of-file.
            changed_lines[current_path].add(max(1, start))
            if current_path_exists:
                deletion_after_anchors.setdefault(current_path, set()).add(max(1, start + 1))
        else:
            changed_lines[current_path].update(range(start, start + new_count))
    if len(deletion_after_anchors) > MAX_DELETION_CONTEXT_FILES:
        raise GitError(f"deletion anchors span more than the {MAX_DELETION_CONTEXT_FILES}-file review limit")
    remaining_bytes = MAX_DELETION_CONTEXT_BYTES
    for file_path, anchors in deletion_after_anchors.items():
        line_count, consumed_bytes = _head_line_count(repo, head, file_path, remaining_bytes)
        remaining_bytes -= consumed_bytes
        changed_lines[file_path].update(anchor for anchor in anchors if anchor <= line_count)
    return {file_path: frozenset(lines or {1}) for file_path, lines in changed_lines.items()}


def _head_line_count(repo: Path, head: str, file_path: str, max_bytes: int) -> tuple[int, int]:
    consumed_bytes = 0
    newline_count = 0
    last_byte: int | None = None

    def consume_stdout(chunk: bytes) -> None:
        nonlocal consumed_bytes, newline_count, last_byte
        consumed_bytes += len(chunk)
        newline_count += chunk.count(b"\n")
        if chunk:
            last_byte = chunk[-1]

    _stream_git_output(
        repo,
        "cat-file",
        "blob",
        f"{head}:{file_path}",
        operation="git cat-file",
        max_stdout_bytes=max_bytes,
        stdout_limit_message=(f"deletion-anchor context exceeds the {MAX_DELETION_CONTEXT_BYTES}-byte review limit"),
        consume_stdout=consume_stdout,
    )
    line_count = newline_count + (1 if consumed_bytes and last_byte != ord("\n") else 0)
    return line_count, consumed_bytes


def _patch_path(raw_path: str) -> str | None:
    if raw_path == "/dev/null":
        return None
    raw_bytes = raw_path.encode("utf-8", errors="surrogateescape")
    path = _unquote_git_path(raw_bytes) if raw_bytes.startswith(b'"') else raw_bytes
    if path.startswith((b"a/", b"b/")):
        path = path[2:]
    return path.decode("utf-8", errors="surrogateescape")


def _unquote_git_path(raw_path: bytes) -> bytes:
    if len(raw_path) < 2 or not raw_path.endswith(b'"'):
        raise GitError("git diff returned an invalid quoted path")
    result = bytearray()
    cursor = 1
    end = len(raw_path) - 1
    while cursor < end:
        byte = raw_path[cursor]
        cursor += 1
        if byte != ord("\\"):
            result.append(byte)
            continue
        if cursor >= end:
            raise GitError("git diff returned an invalid quoted path escape")
        escaped = raw_path[cursor]
        cursor += 1
        if escaped in GIT_PATH_ESCAPES:
            result.append(GIT_PATH_ESCAPES[escaped])
            continue
        if ord("0") <= escaped <= ord("7"):
            digits = bytearray((escaped,))
            while cursor < end and len(digits) < 3 and ord("0") <= raw_path[cursor] <= ord("7"):
                digits.append(raw_path[cursor])
                cursor += 1
            result.append(int(digits, 8))
            continue
        raise GitError("git diff returned an unsupported quoted path escape")
    return bytes(result)


def _git(repo: Path, *args: str) -> str:
    result = _run_git(repo, *args, text=True)
    return result.stdout.removesuffix("\n")


def _git_diff_output(repo: Path, *args: str) -> bytes:
    output = bytearray()
    _stream_git_output(
        repo,
        *args,
        operation="git diff",
        max_stdout_bytes=MAX_FILE_LIST_BYTES,
        stdout_limit_message=f"file list exceeds the {MAX_FILE_LIST_BYTES}-byte review limit",
        consume_stdout=output.extend,
    )
    return bytes(output)


def _git_patch_headers(repo: Path, *args: str) -> tuple[str, ...]:
    """Stream a bounded patch and retain only structural lines needed for anchors."""
    headers: list[str] = []
    pending = bytearray()
    discarding_line = False

    def consume_stdout(chunk: bytes) -> None:
        nonlocal discarding_line
        discarding_line = _consume_patch_chunk(headers, pending, discarding_line, chunk)

    _stream_git_output(
        repo,
        *args,
        operation="git diff",
        max_stdout_bytes=MAX_PATCH_BYTES,
        stdout_limit_message=f"text diff exceeds the {MAX_PATCH_BYTES}-byte review limit",
        consume_stdout=consume_stdout,
    )
    if pending and not discarding_line:
        _append_patch_header(headers, pending)
    return tuple(headers)


def _stream_git_output(
    repo: Path,
    *args: str,
    operation: str,
    max_stdout_bytes: int,
    stdout_limit_message: str,
    consume_stdout: Callable[[bytes], object],
) -> None:
    try:
        process = subprocess.Popen(
            ["git", *args],
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=_git_environment(),
        )
    except FileNotFoundError as exc:
        raise GitError("git executable was not found") from exc

    if process.stdout is None or process.stderr is None:
        process.kill()
        process.communicate()
        raise GitError(f"{operation} did not expose output pipes")

    total_stdout_bytes = 0
    stderr_bytes = bytearray()
    deadline = time.monotonic() + GIT_DIFF_TIMEOUT_SECONDS
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GitError(f"{operation} timed out after {GIT_DIFF_TIMEOUT_SECONDS} seconds")
            events = selector.select(remaining)
            if not events:
                raise GitError(f"{operation} timed out after {GIT_DIFF_TIMEOUT_SECONDS} seconds")
            for key, _mask in events:
                chunk = os.read(key.fd, 64 * 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stderr":
                    stderr_bytes.extend(chunk)
                    if len(stderr_bytes) > MAX_GIT_STDERR_BYTES:
                        raise GitError(f"{operation} stderr exceeds the {MAX_GIT_STDERR_BYTES}-byte review limit")
                    continue
                total_stdout_bytes += len(chunk)
                if total_stdout_bytes > max_stdout_bytes:
                    raise GitError(stdout_limit_message)
                consume_stdout(chunk)
        remaining = max(0, deadline - time.monotonic())
        try:
            return_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"{operation} timed out after {GIT_DIFF_TIMEOUT_SECONDS} seconds") from exc
    except BaseException:
        if process.poll() is None:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            if process.poll() is None:
                with suppress(ProcessLookupError):
                    process.kill()
        process.communicate()
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
    if return_code != 0:
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
        raise GitError(stderr or f"git {' '.join(args)} failed")


def _consume_patch_chunk(headers: list[str], pending: bytearray, discarding_line: bool, chunk: bytes) -> bool:
    cursor = 0
    while cursor < len(chunk):
        newline = chunk.find(b"\n", cursor)
        if discarding_line:
            if newline < 0:
                break
            discarding_line = False
            cursor = newline + 1
            continue
        if newline < 0:
            pending.extend(chunk[cursor:])
            if len(pending) > MAX_HEADER_BYTES:
                pending.clear()
                discarding_line = True
            break
        pending.extend(chunk[cursor:newline])
        _append_patch_header(headers, pending)
        pending.clear()
        cursor = newline + 1
    return discarding_line


def _append_patch_header(headers: list[str], raw_line: bytes | bytearray) -> None:
    line = bytes(raw_line)
    if line.startswith(PATCH_HEADER_PREFIXES):
        headers.append(line.decode("utf-8", errors="surrogateescape"))


def _run_git(repo: Path, *args: str, text: bool) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=text,
            encoding="utf-8" if text else None,
            errors="surrogateescape" if text else None,
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
            env=_git_environment(),
        )
    except FileNotFoundError as exc:
        raise GitError("git executable was not found") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode(errors="replace")
        raise GitError(stderr.strip() or f"git {' '.join(args)} failed") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args)} timed out after {GIT_COMMAND_TIMEOUT_SECONDS} seconds") from exc


def _git_environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ATTR_NOSYSTEM": "1",
        }
    )
    return environment
