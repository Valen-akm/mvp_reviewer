import unittest
from pathlib import Path

from mvp_reviewer.github_pr import (
    GitHubPullRequest,
    PullRequestError,
    PullRequestMetadata,
    parse_github_pr_url,
    prepare_github_pr,
)

PR_URL = "https://github.com/EvoseAI/api.tiwork.ai/pull/125"
BASE_SHA = "1" * 40
HEAD_SHA = "2" * 40


class GitHubPullRequestTest(unittest.TestCase):
    def test_parse_accepts_only_canonical_github_pull_request_urls(self) -> None:
        parsed = parse_github_pr_url(PR_URL)

        self.assertEqual(parsed, GitHubPullRequest("EvoseAI", "api.tiwork.ai", 125, PR_URL))

        invalid_urls = (
            "http://github.com/EvoseAI/api.tiwork.ai/pull/125",
            "https://evil.example/EvoseAI/api.tiwork.ai/pull/125",
            "https://github.com/EvoseAI/api.tiwork.ai/pull/0",
            "https://github.com/EvoseAI/api.tiwork.ai/issues/125",
            "https://github.com/EvoseAI/api.tiwork.ai/pull/125/files",
        )
        for invalid_url in invalid_urls:
            with self.subTest(invalid_url=invalid_url), self.assertRaises(PullRequestError):
                parse_github_pr_url(invalid_url)

    def test_metadata_rejects_mismatched_repository_and_invalid_sha(self) -> None:
        pull_request = parse_github_pr_url(PR_URL)
        valid_payload = {
            "base": {"sha": BASE_SHA, "ref": "main", "repo": {"full_name": "EvoseAI/api.tiwork.ai"}},
            "head": {"sha": HEAD_SHA},
        }

        metadata = PullRequestMetadata.from_payload(pull_request, valid_payload)

        self.assertEqual(metadata.base_sha, BASE_SHA)
        self.assertEqual(metadata.head_sha, HEAD_SHA)
        self.assertEqual(metadata.base_ref, "main")

        mismatched = dict(valid_payload, base={**valid_payload["base"], "repo": {"full_name": "other/repo"}})
        with self.assertRaisesRegex(PullRequestError, "repository does not match"):
            PullRequestMetadata.from_payload(pull_request, mismatched)

        invalid_sha = dict(valid_payload, head={"sha": "not-a-sha"})
        with self.assertRaisesRegex(PullRequestError, "head.sha"):
            PullRequestMetadata.from_payload(pull_request, invalid_sha)

    def test_prepare_fetches_github_pr_ref_without_checking_out_untrusted_files(self) -> None:
        pull_request = parse_github_pr_url(PR_URL)
        metadata = PullRequestMetadata(pull_request, BASE_SHA, "main", HEAD_SHA)
        calls: list[tuple[Path | None, tuple[str, ...]]] = []

        def fake_fetch(_pull_request: GitHubPullRequest) -> PullRequestMetadata:
            return metadata

        def fake_git(cwd: Path | None, *args: str) -> str:
            calls.append((cwd, args))
            if args[0] == "clone":
                Path(args[-1]).mkdir(parents=True)
            if args[:2] == ("rev-parse", "refs/remotes/origin/pr-125"):
                return HEAD_SHA
            return ""

        progress: list[str] = []
        with prepare_github_pr(
            PR_URL, fetch_metadata=fake_fetch, run_git=fake_git, on_progress=progress.append
        ) as target:
            prepared_path = target.repo
            self.assertTrue(prepared_path.is_dir())
            self.assertEqual(target.base, BASE_SHA)
            self.assertEqual(target.head, HEAD_SHA)

        self.assertFalse(prepared_path.exists())
        commands = [args for _cwd, args in calls]
        self.assertIn(
            (
                "fetch",
                "--no-tags",
                "origin",
                "+refs/pull/125/head:refs/remotes/origin/pr-125",
            ),
            commands,
        )
        self.assertIn(("update-ref", "--no-deref", "HEAD", HEAD_SHA), commands)
        self.assertFalse(any(args[0] in {"checkout", "switch"} for args in commands))
        self.assertTrue(any("resolved base=main" in message for message in progress))


if __name__ == "__main__":
    unittest.main()
