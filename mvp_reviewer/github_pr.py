"""Resolve a GitHub pull request URL into a temporary review repository."""

from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, request

_PR_URL_PATTERN = re.compile(
    r"^https://github\.com/"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/"
    r"(?P<repo>[A-Za-z0-9._-]+)/pull/(?P<number>[1-9][0-9]*)/?$"
)
_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
_MAX_API_RESPONSE_BYTES = 2 * 1024 * 1024
_SYSTEM_CA_FILES = (
    Path("/etc/ssl/cert.pem"),
    Path("/etc/ssl/certs/ca-certificates.crt"),
    Path("/etc/pki/tls/certs/ca-bundle.crt"),
)


class PullRequestError(ValueError):
    """Raised when a GitHub pull request cannot be prepared safely."""


@dataclass(frozen=True)
class GitHubPullRequest:
    """The identity and canonical URL of a GitHub pull request."""

    owner: str
    repo: str
    number: int
    url: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def clone_url(self) -> str:
        return f"https://github.com/{self.full_name}.git"

    @property
    def api_url(self) -> str:
        return f"https://api.github.com/repos/{self.full_name}/pulls/{self.number}"


@dataclass(frozen=True)
class PullRequestMetadata:
    """Immutable base and head metadata returned by GitHub."""

    pull_request: GitHubPullRequest
    base_sha: str
    base_ref: str
    head_sha: str

    @classmethod
    def from_payload(
        cls,
        pull_request: GitHubPullRequest,
        payload: Mapping[str, Any],
    ) -> PullRequestMetadata:
        try:
            base = payload["base"]
            head = payload["head"]
            if not isinstance(base, Mapping) or not isinstance(head, Mapping):
                raise TypeError
            base_repo = base["repo"]
            if not isinstance(base_repo, Mapping):
                raise TypeError
            repository = base_repo["full_name"]
            base_sha = base["sha"]
            base_ref = base["ref"]
            head_sha = head["sha"]
        except (KeyError, TypeError) as exc:
            raise PullRequestError("GitHub returned incomplete pull request metadata") from exc

        if not isinstance(repository, str) or repository.casefold() != pull_request.full_name.casefold():
            raise PullRequestError("GitHub pull request repository does not match the requested URL")
        validated_base_sha = _validated_sha(base_sha, "base.sha")
        validated_head_sha = _validated_sha(head_sha, "head.sha")
        if not isinstance(base_ref, str) or not base_ref.strip():
            raise PullRequestError("GitHub returned an invalid base.ref")
        return cls(pull_request, validated_base_sha, base_ref, validated_head_sha)


@dataclass(frozen=True)
class PreparedPullRequest:
    """A temporary Git repository ready for the normal local review pipeline."""

    repo: Path
    base: str
    head: str
    source: GitHubPullRequest


def parse_github_pr_url(url: str) -> GitHubPullRequest:
    """Parse a canonical github.com pull request URL."""
    match = _PR_URL_PATTERN.fullmatch(url)
    if match is None:
        raise PullRequestError("--pr must be a canonical https://github.com/<owner>/<repo>/pull/<number> URL")
    return GitHubPullRequest(
        owner=match.group("owner"),
        repo=match.group("repo"),
        number=int(match.group("number")),
        url=url,
    )


def fetch_pull_request_metadata(pull_request: GitHubPullRequest) -> PullRequestMetadata:
    """Fetch immutable review coordinates from the GitHub REST API."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "mvp-reviewer",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    api_request = request.Request(pull_request.api_url, headers=headers)

    try:
        with request.urlopen(api_request, timeout=30, context=_ssl_context()) as response:
            body = response.read(_MAX_API_RESPONSE_BYTES + 1)
    except error.HTTPError as exc:
        if exc.code == 404:
            message = "pull request was not found or is not accessible"
        elif exc.code in {401, 403}:
            message = "GitHub API authentication or rate limit rejected the request"
        else:
            message = f"GitHub API returned HTTP {exc.code}"
        raise PullRequestError(message) from exc
    except (error.URLError, TimeoutError, OSError) as exc:
        raise PullRequestError("could not reach the GitHub API") from exc

    if len(body) > _MAX_API_RESPONSE_BYTES:
        raise PullRequestError("GitHub API response was unexpectedly large")
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PullRequestError("GitHub API returned invalid JSON") from exc
    if not isinstance(payload, Mapping):
        raise PullRequestError("GitHub API returned an invalid pull request payload")
    return PullRequestMetadata.from_payload(pull_request, payload)


def run_git(cwd: Path | None, *args: str) -> str:
    """Run a non-interactive Git command used while preparing a PR."""
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=1800,
            env=environment,
        )
    except FileNotFoundError as exc:
        raise PullRequestError("git executable was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise PullRequestError(f"git {args[0]} timed out") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip()[-2000:] or "unknown Git error"
        raise PullRequestError(f"git {args[0]} failed: {detail}")
    return completed.stdout.strip()


@contextmanager
def prepare_github_pr(
    pr_url: str,
    *,
    fetch_metadata: Callable[[GitHubPullRequest], PullRequestMetadata] = fetch_pull_request_metadata,
    run_git: Callable[..., str] = run_git,
    on_progress: Callable[[str], None] = lambda _message: None,
) -> Iterator[PreparedPullRequest]:
    """Prepare a PR without checking out its untrusted working tree."""
    pull_request = parse_github_pr_url(pr_url)
    on_progress(f"[prepare] resolving {pull_request.full_name}#{pull_request.number}")
    metadata = fetch_metadata(pull_request)
    on_progress(f"[prepare] resolved base={metadata.base_ref} ({metadata.base_sha[:12]}) head={metadata.head_sha[:12]}")

    with tempfile.TemporaryDirectory(prefix="mvp-review-pr-") as directory:
        repo = Path(directory) / "repo"
        on_progress(f"[prepare] cloning {pull_request.full_name}")
        run_git(
            None,
            "clone",
            "--no-checkout",
            "--origin",
            "origin",
            "--",
            pull_request.clone_url,
            str(repo),
        )
        pull_ref = f"refs/remotes/origin/pr-{pull_request.number}"
        on_progress(f"[prepare] fetching pull request #{pull_request.number}")
        run_git(
            repo,
            "fetch",
            "--no-tags",
            "origin",
            f"+refs/pull/{pull_request.number}/head:{pull_ref}",
        )
        fetched_head = _validated_sha(run_git(repo, "rev-parse", pull_ref), "fetched head")
        if fetched_head != metadata.head_sha:
            raise PullRequestError("pull request head changed while it was being prepared; retry the review")
        run_git(repo, "cat-file", "-e", f"{metadata.base_sha}^{{commit}}")
        run_git(repo, "update-ref", "--no-deref", "HEAD", metadata.head_sha)
        on_progress(f"[prepare] repository ready at head={metadata.head_sha[:12]}")
        yield PreparedPullRequest(repo, metadata.base_sha, metadata.head_sha, pull_request)


def _validated_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA_PATTERN.fullmatch(value) is None:
        raise PullRequestError(f"GitHub returned an invalid {field}")
    return value.lower()


def _ssl_context() -> ssl.SSLContext:
    configured_ca_file = os.environ.get("SSL_CERT_FILE")
    if configured_ca_file:
        return ssl.create_default_context(cafile=configured_ca_file)
    for ca_file in _SYSTEM_CA_FILES:
        if ca_file.is_file():
            return ssl.create_default_context(cafile=ca_file)
    return ssl.create_default_context()
