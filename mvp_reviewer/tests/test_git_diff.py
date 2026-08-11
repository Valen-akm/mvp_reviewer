import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mvp_reviewer.git_diff as git_diff
from mvp_reviewer.git_diff import GitError, collect_diff_scope, review_snapshot


class GitDiffScopeTest(unittest.TestCase):
    def test_collect_diff_scope_preserves_trailing_space_in_repo_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo "
            repo.mkdir()
            self._git(repo, "init", "--quiet")
            self._git(repo, "config", "user.email", "reviewer@example.com")
            self._git(repo, "config", "user.name", "Reviewer Test")
            source = repo / "app.py"
            source.write_text("value = 1\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "base")
            base = self._git(repo, "rev-parse", "HEAD")
            source.write_text("value = 2\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "head")

            scope = collect_diff_scope(repo, base)

            self.assertEqual(scope.repo, repo.resolve())
            self.assertEqual(scope.files, ("app.py",))

    def test_collect_diff_scope_tracks_files_and_new_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._git(repo, "init", "--quiet")
            self._git(repo, "config", "user.email", "reviewer@example.com")
            self._git(repo, "config", "user.name", "Reviewer Test")

            source = repo / "sample.py"
            source.write_text("def total(values):\n    return sum(values)\n", encoding="utf-8")
            self._git(repo, "add", "sample.py")
            self._git(repo, "commit", "--quiet", "-m", "base")
            base = self._git(repo, "rev-parse", "HEAD")

            source.write_text(
                "def total(values):\n    if values is None:\n        return 0\n    return sum(values)\n",
                encoding="utf-8",
            )
            self._git(repo, "add", "sample.py")
            self._git(repo, "commit", "--quiet", "-m", "head")

            scope = collect_diff_scope(repo, f"{base}~0")

            self.assertEqual(scope.files, ("sample.py",))
            self.assertEqual(scope.base, base)
            self.assertEqual(scope.head, self._git(repo, "rev-parse", "HEAD"))
            self.assertTrue(scope.contains_changed_line("sample.py", 2))
            self.assertTrue(scope.contains_changed_line("sample.py", 3))
            self.assertFalse(scope.contains_changed_line("sample.py", 4))

    def test_collect_diff_scope_keeps_anchor_for_deletion_only_hunk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._git(repo, "init", "--quiet")
            self._git(repo, "config", "user.email", "reviewer@example.com")
            self._git(repo, "config", "user.name", "Reviewer Test")

            source = repo / "b" / "project.py"
            source.parent.mkdir()
            source.write_text(
                "def delete_project(user, project):\n"
                "    if user.id != project.owner_id:\n"
                "        raise PermissionError\n"
                "    project.delete()\n",
                encoding="utf-8",
            )
            self._git(repo, "add", "b/project.py")
            self._git(repo, "commit", "--quiet", "-m", "base")
            base = self._git(repo, "rev-parse", "HEAD")

            source.write_text(
                "def delete_project(user, project):\n    project.delete()\n",
                encoding="utf-8",
            )
            self._git(repo, "add", "b/project.py")
            self._git(repo, "commit", "--quiet", "-m", "remove authorization")

            scope = collect_diff_scope(repo, base)

            self.assertEqual(scope.changed_lines["b/project.py"], frozenset({1, 2}))

    def test_collect_diff_scope_keeps_file_anchor_without_text_hunks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._git(repo, "init", "--quiet")
            self._git(repo, "config", "user.email", "reviewer@example.com")
            self._git(repo, "config", "user.name", "Reviewer Test")

            (repo / "original.py").write_text("value = 1\n", encoding="utf-8")
            tool = repo / "tool.sh"
            tool.write_text("#!/bin/sh\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "base")
            base = self._git(repo, "rev-parse", "HEAD")

            self._git(repo, "mv", "original.py", "renamed.py")
            tool.chmod(0o755)
            self._git(repo, "add", "tool.sh")
            self._git(repo, "commit", "--quiet", "-m", "rename and change mode")

            scope = collect_diff_scope(repo, base)

            self.assertEqual(scope.changed_lines["renamed.py"], frozenset({1}))
            self.assertEqual(scope.changed_lines["tool.sh"], frozenset({1}))

    def test_multi_file_scope_streams_bounded_diffs_and_preserves_unusual_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._git(repo, "init", "--quiet")
            self._git(repo, "config", "user.email", "reviewer@example.com")
            self._git(repo, "config", "user.name", "Reviewer Test")
            paths = (repo / "normal.py", repo / "odd\nname.py")
            for source in paths:
                source.write_text("value = 1\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "base")
            base = self._git(repo, "rev-parse", "HEAD")

            for source in paths:
                source.write_text("value = 2\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "change both files")

            original_stream_git_output = git_diff._stream_git_output
            original_patch_headers = git_diff._git_patch_headers
            with (
                patch(
                    "mvp_reviewer.git_diff._stream_git_output", wraps=original_stream_git_output
                ) as stream_git_output,
                patch("mvp_reviewer.git_diff._git_patch_headers", wraps=original_patch_headers) as patch_headers,
            ):
                scope = collect_diff_scope(repo, base)

            self.assertEqual(stream_git_output.call_count, 3)
            self.assertEqual(patch_headers.call_count, 1)
            self.assertEqual(scope.changed_lines["normal.py"], frozenset({1}))
            self.assertEqual(scope.changed_lines["odd\nname.py"], frozenset({1}))

    def test_collect_diff_scope_reports_actual_merge_base(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._git(repo, "init", "--quiet")
            self._git(repo, "config", "user.email", "reviewer@example.com")
            self._git(repo, "config", "user.name", "Reviewer Test")
            base_branch = self._git(repo, "symbolic-ref", "--short", "HEAD")
            (repo / "shared.txt").write_text("base\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "common base")
            common_base = self._git(repo, "rev-parse", "HEAD")
            self._git(repo, "branch", "feature")

            (repo / "base-only.txt").write_text("base branch\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "advance base")
            self._git(repo, "checkout", "--quiet", "feature")
            (repo / "feature-only.txt").write_text("feature branch\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "feature")

            scope = collect_diff_scope(repo, base_branch)

            self.assertEqual(scope.base, common_base)
            self.assertEqual(scope.files, ("feature-only.txt",))

    def test_review_snapshot_ignores_live_worktree_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._git(repo, "init", "--quiet")
            self._git(repo, "config", "user.email", "reviewer@example.com")
            self._git(repo, "config", "user.name", "Reviewer Test")
            source = repo / "app.py"
            source.write_text("value = 1\n", encoding="utf-8")
            script = repo / "tool.sh"
            script.write_text("#!/bin/sh\n", encoding="utf-8")
            script.chmod(0o755)
            (repo / "app-link.py").symlink_to("app.py")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "base")
            base = self._git(repo, "rev-parse", "HEAD")
            source.write_text("value = 2\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "head")
            source.write_text("value = 999\n", encoding="utf-8")
            (repo / "untracked.txt").write_text("not committed\n", encoding="utf-8")
            scope = collect_diff_scope(repo, base)

            with review_snapshot(scope) as snapshot:
                self.assertEqual((snapshot / "app.py").read_text(encoding="utf-8"), "value = 2\n")
                self.assertFalse((snapshot / "untracked.txt").exists())
                self.assertTrue((snapshot / "tool.sh").stat().st_mode & 0o111)
                self.assertEqual((snapshot / "app-link.py").readlink(), Path("app.py"))
                self.assertFalse((snapshot / ".git" / "objects" / "info" / "alternates").exists())
                self.assertEqual(self._git(snapshot, "diff", "--name-only", scope.base, scope.head), "app.py")
                self.assertEqual(self._git(snapshot, "status", "--porcelain"), "")
                object_suffix = Path(scope.head[:2]) / scope.head[2:]
                self.assertTrue(
                    (repo / ".git" / "objects" / object_suffix).samefile(snapshot / ".git" / "objects" / object_suffix)
                )

    def test_review_snapshot_does_not_require_writable_repo_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "read-only"
            repo = parent / "repo"
            repo.mkdir(parents=True)
            self._git(repo, "init", "--quiet")
            self._git(repo, "config", "user.email", "reviewer@example.com")
            self._git(repo, "config", "user.name", "Reviewer Test")
            source = repo / "app.py"
            source.write_text("value = 1\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "base")
            base = self._git(repo, "rev-parse", "HEAD")
            source.write_text("value = 2\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "head")
            scope = collect_diff_scope(repo, base)
            parent.chmod(0o555)
            try:
                with review_snapshot(scope) as snapshot:
                    self.assertEqual((snapshot / "app.py").read_text(encoding="utf-8"), "value = 2\n")
            finally:
                parent.chmod(0o755)

    def test_review_snapshot_copies_objects_across_filesystems(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._git(repo, "init", "--quiet")
            self._git(repo, "config", "user.email", "reviewer@example.com")
            self._git(repo, "config", "user.name", "Reviewer Test")
            source = repo / "app.py"
            source.write_text("value = 1\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "base")
            base = self._git(repo, "rev-parse", "HEAD")
            source.write_text("value = 2\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "head")
            scope = collect_diff_scope(repo, base)

            with (
                patch("mvp_reviewer.git_diff._same_device", return_value=False),
                review_snapshot(scope) as snapshot,
            ):
                object_suffix = Path(scope.head[:2]) / scope.head[2:]
                self.assertFalse(
                    (repo / ".git" / "objects" / object_suffix).samefile(snapshot / ".git" / "objects" / object_suffix)
                )

    def test_review_snapshot_rejects_oversized_cross_filesystem_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._git(repo, "init", "--quiet")
            self._git(repo, "config", "user.email", "reviewer@example.com")
            self._git(repo, "config", "user.name", "Reviewer Test")
            source = repo / "app.py"
            source.write_text("value = 1\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "base")
            base = self._git(repo, "rev-parse", "HEAD")
            source.write_text("value = 2\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "head")
            scope = collect_diff_scope(repo, base)

            with (
                patch("mvp_reviewer.git_diff._same_device", return_value=False),
                patch("mvp_reviewer.git_diff.MAX_SNAPSHOT_COPY_BYTES", 128),
                patch("mvp_reviewer.git_diff._git_object_store_bytes", return_value=129),
                self.assertRaisesRegex(GitError, "exceeding the 128-byte copy limit"),
                review_snapshot(scope),
            ):
                self.fail("oversized snapshots must not be created")

    def test_review_snapshot_rejects_oversized_expanded_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._git(repo, "init", "--quiet")
            self._git(repo, "config", "user.email", "reviewer@example.com")
            self._git(repo, "config", "user.name", "Reviewer Test")
            source = repo / "app.py"
            source.write_text("value = 1\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "base")
            base = self._git(repo, "rev-parse", "HEAD")
            source.write_text("value = 2\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "head")
            scope = collect_diff_scope(repo, base)

            with (
                patch("mvp_reviewer.git_diff.MAX_SNAPSHOT_WORKTREE_BYTES", 1),
                self.assertRaisesRegex(GitError, "HEAD tree exceeds the 1-byte expanded snapshot limit"),
                review_snapshot(scope),
            ):
                self.fail("oversized worktrees must not be checked out")

    def test_review_snapshot_rejects_too_many_tracked_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._git(repo, "init", "--quiet")
            self._git(repo, "config", "user.email", "reviewer@example.com")
            self._git(repo, "config", "user.name", "Reviewer Test")
            source = repo / "app.py"
            source.write_text("value = 1\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "base")
            base = self._git(repo, "rev-parse", "HEAD")
            source.write_text("value = 2\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "head")
            scope = collect_diff_scope(repo, base)

            with (
                patch("mvp_reviewer.git_diff.MAX_SNAPSHOT_FILES", 0),
                self.assertRaisesRegex(GitError, "HEAD tree exceeds the 0-file snapshot limit"),
                review_snapshot(scope),
            ):
                self.fail("oversized worktrees must not be checked out")

    def test_review_snapshot_ignores_global_smudge_filters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            self._git(repo, "init", "--quiet")
            self._git(repo, "config", "user.email", "reviewer@example.com")
            self._git(repo, "config", "user.name", "Reviewer Test")
            (repo / ".gitattributes").write_text("asset.txt filter=inflate\n", encoding="utf-8")
            (repo / "asset.txt").write_text("small\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "head")
            head = self._git(repo, "rev-parse", "HEAD")
            scope = git_diff.DiffScope(repo, head, head, ("asset.txt",), {"asset.txt": frozenset({1})})
            filter_script = root / "inflate.sh"
            filter_script.write_text("#!/bin/sh\nprintf expanded\n", encoding="utf-8")
            filter_script.chmod(0o755)
            global_config = root / "global.gitconfig"
            subprocess.run(
                ["git", "config", "--file", str(global_config), "filter.inflate.smudge", str(filter_script)],
                check=True,
            )
            subprocess.run(
                ["git", "config", "--file", str(global_config), "filter.inflate.required", "true"],
                check=True,
            )

            with (
                patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(global_config)}),
                review_snapshot(scope) as snapshot,
            ):
                self.assertEqual((snapshot / "asset.txt").read_text(encoding="utf-8"), "small\n")

    def test_review_snapshot_limits_ident_expansion_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._git(repo, "init", "--quiet")
            self._git(repo, "config", "user.email", "reviewer@example.com")
            self._git(repo, "config", "user.name", "Reviewer Test")
            attributes = "asset.txt ident\n"
            (repo / ".gitattributes").write_text(attributes, encoding="utf-8")
            raw_content = "$Id$\n" * 100
            (repo / "asset.txt").write_text(raw_content, encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "head")
            head = self._git(repo, "rev-parse", "HEAD")
            scope = git_diff.DiffScope(repo, head, head, ("asset.txt",), {"asset.txt": frozenset({1})})

            raw_tree_bytes = len(attributes.encode()) + len(raw_content.encode())
            with (
                patch("mvp_reviewer.git_diff.MAX_SNAPSHOT_WORKTREE_BYTES", raw_tree_bytes),
                self.assertRaisesRegex(GitError, "archive exceeds .* expanded snapshot limit"),
                review_snapshot(scope),
            ):
                self.fail("attribute expansion must not exceed the materialization limit")

    def test_review_snapshot_rejects_archive_only_substitution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._git(repo, "init", "--quiet")
            self._git(repo, "config", "user.email", "reviewer@example.com")
            self._git(repo, "config", "user.name", "Reviewer Test")
            (repo / ".gitattributes").write_text("asset.txt export-subst\n", encoding="utf-8")
            (repo / "asset.txt").write_text("$Format:%H$\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "head")
            head = self._git(repo, "rev-parse", "HEAD")
            scope = git_diff.DiffScope(repo, head, head, ("asset.txt",), {"asset.txt": frozenset({1})})

            with (
                self.assertRaisesRegex(GitError, "materialized snapshot does not match the resolved HEAD"),
                review_snapshot(scope),
            ):
                self.fail("archive-only substitutions must not be reviewed as HEAD content")

    def test_review_snapshot_bounds_archive_stderr_while_streaming(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._git(repo, "init", "--quiet")
            self._git(repo, "config", "user.email", "reviewer@example.com")
            self._git(repo, "config", "user.name", "Reviewer Test")
            (repo / ".gitattributes").write_text("!invalid\n" * 20, encoding="utf-8")
            (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "head")
            head = self._git(repo, "rev-parse", "HEAD")
            scope = git_diff.DiffScope(repo, head, head, ("app.py",), {"app.py": frozenset({1})})

            with (
                patch("mvp_reviewer.git_diff.MAX_GIT_STDERR_BYTES", 64),
                self.assertRaisesRegex(GitError, "git archive stderr exceeds the 64-byte review limit"),
                review_snapshot(scope),
            ):
                self.fail("archive stderr unexpectedly exceeded its streaming limit")

    def test_collect_diff_scope_rejects_changed_submodule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._git(repo, "init", "--quiet")
            self._git(repo, "config", "user.email", "reviewer@example.com")
            self._git(repo, "config", "user.name", "Reviewer Test")
            (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "base")
            base = self._git(repo, "rev-parse", "HEAD")
            self._git(repo, "update-index", "--add", "--cacheinfo", f"160000,{base},vendor")
            self._git(repo, "commit", "--quiet", "-m", "add submodule")

            with self.assertRaisesRegex(GitError, "changed submodules are not supported"):
                collect_diff_scope(repo, base)

    def test_collect_diff_scope_rejects_ignored_changed_submodule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._git(repo, "init", "--quiet")
            self._git(repo, "config", "user.email", "reviewer@example.com")
            self._git(repo, "config", "user.name", "Reviewer Test")
            (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "initial target")
            initial_target = self._git(repo, "rev-parse", "HEAD")
            (repo / ".gitmodules").write_text(
                '[submodule "vendor"]\n\tpath = vendor\n\turl = ../vendor\n\tignore = all\n',
                encoding="utf-8",
            )
            self._git(repo, "add", ".gitmodules")
            self._git(repo, "update-index", "--add", "--cacheinfo", f"160000,{initial_target},vendor")
            self._git(repo, "commit", "--quiet", "-m", "add ignored submodule")
            base = self._git(repo, "rev-parse", "HEAD")
            self._git(repo, "update-index", "--cacheinfo", f"160000,{base},vendor")
            self._git(repo, "commit", "--quiet", "-m", "move ignored submodule")

            with self.assertRaisesRegex(GitError, "changed submodules are not supported"):
                collect_diff_scope(repo, base)

    def test_collect_diff_scope_ignores_repo_noprefix_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._git(repo, "init", "--quiet")
            self._git(repo, "config", "user.email", "reviewer@example.com")
            self._git(repo, "config", "user.name", "Reviewer Test")
            self._git(repo, "config", "diff.noprefix", "true")
            for directory_name in ("a", "b"):
                source = repo / directory_name / "sample.py"
                source.parent.mkdir()
                source.write_text("first = 1\nsecond = 1\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "base")
            base = self._git(repo, "rev-parse", "HEAD")
            for directory_name in ("a", "b"):
                source = repo / directory_name / "sample.py"
                source.write_text("first = 1\nsecond = 2\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "head")

            scope = collect_diff_scope(repo, base)

            self.assertEqual(scope.changed_lines["a/sample.py"], frozenset({2}))
            self.assertEqual(scope.changed_lines["b/sample.py"], frozenset({2}))

    def test_review_snapshot_uses_object_directory_device(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._git(repo, "init", "--quiet")
            self._git(repo, "config", "user.email", "reviewer@example.com")
            self._git(repo, "config", "user.name", "Reviewer Test")
            (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "head")
            head = self._git(repo, "rev-parse", "HEAD")
            scope = git_diff.DiffScope(repo, head, head, ("app.py",), {"app.py": frozenset({1})})
            object_directory = (repo / ".git" / "objects").resolve()
            original_same_device = git_diff._same_device

            def same_device(source: Path, destination: Path) -> bool:
                self.assertEqual(source, object_directory)
                return original_same_device(source, destination)

            with (
                patch("mvp_reviewer.git_diff._same_device", side_effect=same_device),
                review_snapshot(scope) as snapshot,
            ):
                self.assertTrue((snapshot / "app.py").is_file())

    def test_eof_deletion_uses_existing_line_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._git(repo, "init", "--quiet")
            self._git(repo, "config", "user.email", "reviewer@example.com")
            self._git(repo, "config", "user.name", "Reviewer Test")
            source = repo / "sample.txt"
            source.write_text("one\ntwo\nthree\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "base")
            base = self._git(repo, "rev-parse", "HEAD")
            source.write_text("one\ntwo\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "delete EOF")

            scope = collect_diff_scope(repo, base)

            self.assertEqual(scope.changed_lines["sample.txt"], frozenset({2}))
            self.assertFalse(scope.contains_changed_line("sample.txt", 3))

    def test_collect_diff_scope_rejects_oversized_patch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._git(repo, "init", "--quiet")
            self._git(repo, "config", "user.email", "reviewer@example.com")
            self._git(repo, "config", "user.name", "Reviewer Test")
            source = repo / "large.txt"
            source.write_text("small\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "base")
            base = self._git(repo, "rev-parse", "HEAD")
            source.write_text("x" * 1024 + "\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "large line")

            with (
                patch("mvp_reviewer.git_diff.MAX_PATCH_BYTES", 128),
                self.assertRaisesRegex(GitError, "128-byte review limit"),
            ):
                collect_diff_scope(repo, base)

    def test_collect_diff_scope_rejects_oversized_file_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._git(repo, "init", "--quiet")
            self._git(repo, "config", "user.email", "reviewer@example.com")
            self._git(repo, "config", "user.name", "Reviewer Test")
            (repo / "base.txt").write_text("base\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "base")
            base = self._git(repo, "rev-parse", "HEAD")
            (repo / ("a" * 100)).write_text("a\n", encoding="utf-8")
            (repo / ("b" * 100)).write_text("b\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "long file list")

            with (
                patch("mvp_reviewer.git_diff.MAX_FILE_LIST_BYTES", 128),
                self.assertRaisesRegex(GitError, "file list exceeds the 128-byte review limit"),
            ):
                collect_diff_scope(repo, base)

    def test_collect_diff_scope_times_out_file_list_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._git(repo, "init", "--quiet")
            self._git(repo, "config", "user.email", "reviewer@example.com")
            self._git(repo, "config", "user.name", "Reviewer Test")
            (repo / "base.txt").write_text("base\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "base")
            base = self._git(repo, "rev-parse", "HEAD")
            (repo / "head.txt").write_text("head\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "head")

            with (
                patch("mvp_reviewer.git_diff.GIT_DIFF_TIMEOUT_SECONDS", 0),
                self.assertRaisesRegex(GitError, "git diff timed out after 0 seconds"),
            ):
                collect_diff_scope(repo, base)

    def test_changed_line_limit_counts_deleted_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._git(repo, "init", "--quiet")
            self._git(repo, "config", "user.email", "reviewer@example.com")
            self._git(repo, "config", "user.name", "Reviewer Test")
            source = repo / "deleted.txt"
            source.write_text("1\n2\n3\n4\n5\n6\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "base")
            base = self._git(repo, "rev-parse", "HEAD")
            source.write_text("", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "delete lines")

            with (
                patch("mvp_reviewer.git_diff.MAX_CHANGED_LINES", 5),
                self.assertRaisesRegex(GitError, "5-changed-line review limit"),
            ):
                collect_diff_scope(repo, base)

    def test_git_diff_stderr_is_bounded_without_deadlock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._git(repo, "init", "--quiet")
            self._git(repo, "config", "user.email", "reviewer@example.com")
            self._git(repo, "config", "user.name", "Reviewer Test")
            source = repo / "app.py"
            source.write_text("value = 1\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "base")
            base = self._git(repo, "rev-parse", "HEAD")
            source.write_text("value = 2\n", encoding="utf-8")
            (repo / ".gitattributes").write_text("!bad\n" * 100, encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "invalid attributes")

            with (
                patch("mvp_reviewer.git_diff.MAX_GIT_STDERR_BYTES", 128),
                self.assertRaisesRegex(GitError, "git diff stderr exceeds the 128-byte review limit"),
            ):
                collect_diff_scope(repo, base)

    def test_scope_preserves_posix_backslash_filename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._git(repo, "init", "--quiet")
            self._git(repo, "config", "user.email", "reviewer@example.com")
            self._git(repo, "config", "user.name", "Reviewer Test")
            source = repo / "odd\\name.py"
            source.write_text("value = 1\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "base")
            base = self._git(repo, "rev-parse", "HEAD")
            source.write_text("value = 2\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "head")

            scope = collect_diff_scope(repo, base)

            self.assertEqual(scope.files, ("odd\\name.py",))
            self.assertTrue(scope.contains_changed_line("odd\\name.py", 1))

    def test_patch_path_decodes_quoted_non_utf8_bytes(self) -> None:
        file_name = b"odd\n\xff.py".decode("utf-8", errors="surrogateescape")

        self.assertEqual(git_diff._patch_path('"a/odd\\n\\377.py"'), file_name)

    @unittest.skipUnless(sys.platform.startswith("linux"), "raw non-UTF-8 filename test requires Linux")
    def test_scope_preserves_quoted_non_utf8_filename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._git(repo, "init", "--quiet")
            self._git(repo, "config", "user.email", "reviewer@example.com")
            self._git(repo, "config", "user.name", "Reviewer Test")
            file_name = b"odd\n\xff.py".decode("utf-8", errors="surrogateescape")
            source = repo / file_name
            source.write_text("value = 1\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "base")
            base = self._git(repo, "rev-parse", "HEAD")
            source.write_text("value = 2\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "--quiet", "-m", "head")

            scope = collect_diff_scope(repo, base)

            self.assertEqual(scope.files, (file_name,))
            self.assertTrue(scope.contains_changed_line(file_name, 1))
            with review_snapshot(scope) as snapshot:
                self.assertEqual((snapshot / file_name).read_text(encoding="utf-8"), "value = 2\n")

    def test_review_snapshot_rejects_alternate_backed_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin = root / "origin"
            shared = root / "shared"
            origin.mkdir()
            self._git(origin, "init", "--quiet")
            self._git(origin, "config", "user.email", "reviewer@example.com")
            self._git(origin, "config", "user.name", "Reviewer Test")
            (origin / "app.py").write_text("value = 1\n", encoding="utf-8")
            self._git(origin, "add", ".")
            self._git(origin, "commit", "--quiet", "-m", "base")
            subprocess.run(
                ["git", "clone", "--quiet", "--shared", str(origin), str(shared)],
                check=True,
                capture_output=True,
                text=True,
            )
            self._git(shared, "config", "user.email", "reviewer@example.com")
            self._git(shared, "config", "user.name", "Reviewer Test")
            base = self._git(shared, "rev-parse", "HEAD")
            (shared / "app.py").write_text("value = 2\n", encoding="utf-8")
            self._git(shared, "add", ".")
            self._git(shared, "commit", "--quiet", "-m", "head")
            scope = collect_diff_scope(shared, base)

            with (
                self.assertRaisesRegex(GitError, "alternate-backed repositories are not supported"),
                review_snapshot(scope),
            ):
                self.fail("alternate-backed snapshot unexpectedly succeeded")

    @staticmethod
    def _git(repo: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()


if __name__ == "__main__":
    unittest.main()
