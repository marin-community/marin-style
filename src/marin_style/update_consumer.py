"""Create and merge a validated marin-style update in one consumer repository."""

import argparse
import json
import re
import subprocess
import tempfile
import time
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

MARIN_STYLE_REPOSITORY = "https://github.com/marin-community/marin-style"
MARIN_STYLE_PACKAGE = "marin-style"
MARIN_STYLE_MANIFEST = ".agents/marin-style/manifest.json"
MARIN_STYLE_BRANCH = "automation/marin-style"
MARIN_STYLE_TITLE = "[dependencies] Advance marin-style"
CANONICAL_PIN_FILE = "infra/pre-commit.py"
UV_LOCK_FILE = "uv.lock"
MANAGED_PREFIXES = (".agents/marin-style/", ".agents/skills/")
DEPENDENCY_UPDATE_LABELS = ("agent-generated", "dependencies")
PASS_CHECK_BUCKETS = frozenset({"pass", "skipping"})
PENDING_CHECK_BUCKET = "pending"
REVISION = re.compile(r"[0-9a-f]{40}")
MARIN_STYLE_PIN = re.compile(rf"{re.escape(MARIN_STYLE_REPOSITORY)}(?:\.git)?@([0-9a-f]{{40}})")
CONTENT_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
PR_BODY = (
    "Advance marin-style to `{revision}` and regenerate its shared agent guidance.\n\n"
    "Changed paths are restricted to discovered marin-style pins, the generated lockfile when present, "
    "and files owned by the old and new manifests.\n"
)


@dataclass(frozen=True)
class GeneratedManifest:
    """Exact paths and hashes owned by one marin-style revision."""

    revision: str
    files: tuple[tuple[str, str], ...]

    @classmethod
    def from_text(cls, text: str, *, expected_revision: str) -> "GeneratedManifest":
        payload = json.loads(text)
        if not isinstance(payload, dict) or set(payload) != {"files", "format", "revision"}:
            raise ValueError("invalid marin-style manifest shape")
        if payload["format"] != 1 or payload["revision"] != expected_revision:
            raise ValueError(f"manifest does not describe marin-style revision {expected_revision}")
        files = payload["files"]
        if not isinstance(files, dict) or not files:
            raise ValueError("marin-style manifest has no files")
        for path, digest in files.items():
            if not isinstance(path, str) or not isinstance(digest, str):
                raise ValueError("marin-style manifest files must map paths to digests")
            relative = PurePosixPath(path)
            if (
                relative.is_absolute()
                or relative.as_posix() != path
                or ".." in relative.parts
                or not path.startswith(MANAGED_PREFIXES)
                or path == MARIN_STYLE_MANIFEST
            ):
                raise ValueError(f"invalid marin-style managed path: {path!r}")
            if CONTENT_DIGEST.fullmatch(digest) is None:
                raise ValueError(f"invalid digest for marin-style managed path: {path!r}")
        return cls(revision=expected_revision, files=tuple(sorted(files.items())))


@dataclass(frozen=True)
class PullRequestPolicy:
    base_branch: str
    head_branch: str
    title: str
    allowed_files: frozenset[str]


@dataclass(frozen=True)
class GeneratedUpdate:
    old_revision: str
    new_revision: str
    changed_files: tuple[str, ...]
    policy: PullRequestPolicy


@dataclass(frozen=True)
class LockedMarinStyle:
    version: str
    source: str


@dataclass(frozen=True)
class PullRequestSnapshot:
    author: str
    base_branch: str
    files: tuple[str, ...]
    head_branch: str
    head_sha: str
    state: str
    title: str
    url: str


@dataclass(frozen=True)
class CheckRow:
    name: str
    bucket: str

    @classmethod
    def from_json(cls, payload: object) -> "CheckRow":
        if not isinstance(payload, dict):
            raise ValueError(f"GitHub returned a non-object check row: {payload!r}")
        name = payload.get("name")
        bucket = payload.get("bucket")
        if not isinstance(name, str) or not isinstance(bucket, str):
            raise ValueError(f"GitHub returned an invalid check row: {payload!r}")
        return cls(name=name, bucket=bucket)


@dataclass(frozen=True)
class RequiredCheckGate:
    failing: tuple[str, ...]
    missing: tuple[str, ...]
    pending: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failing and not self.missing and not self.pending


class MergeDecision(StrEnum):
    WAIT = "wait"
    FAIL = "fail"
    MERGE = "merge"
    DONE = "done"


class BranchPushMode(StrEnum):
    CREATE = "create"
    FORCE_WITH_LEASE = "force-with-lease"


@dataclass(frozen=True)
class UpdateBranch:
    expected_remote_sha: str
    pull_request_url: str
    push_mode: BranchPushMode


@dataclass(frozen=True)
class PublishedPullRequest:
    head_sha: str
    url: str


class ConsumerUpdateStatus(StrEnum):
    CURRENT = "current"
    PUBLISHED = "published"
    MERGED = "merged"


@dataclass(frozen=True)
class ConsumerUpdateResult:
    status: ConsumerUpdateStatus
    pull_request_url: str


def _run(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(list(args), cwd=cwd, check=True, capture_output=True, text=True).stdout


def _gh_json(*args: str) -> object:
    return json.loads(_run("gh", *args))


def resolve_target_revision() -> str:
    """Return the exact commit currently at marin-style's default branch."""
    output = _run("git", "ls-remote", MARIN_STYLE_REPOSITORY, "refs/heads/main")
    fields = output.split()
    if len(fields) != 2 or fields[1] != "refs/heads/main" or REVISION.fullmatch(fields[0]) is None:
        raise ValueError(f"git returned an invalid marin-style main reference: {output!r}")
    return fields[0]


def _manifest_for_revision(revision: str) -> GeneratedManifest:
    output = _run(
        "uvx",
        "--from",
        f"git+{MARIN_STYLE_REPOSITORY}@{revision}",
        "marin-style",
        "managed-files",
    )
    return GeneratedManifest.from_text(output, expected_revision=revision)


def _checked_in_manifest(repo_root: Path, *, expected_revision: str) -> GeneratedManifest:
    path = repo_root / MARIN_STYLE_MANIFEST
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"consumer must contain a regular marin-style manifest: {path}")
    return GeneratedManifest.from_text(path.read_text(), expected_revision=expected_revision)


def _old_revision(repo_root: Path) -> str:
    canonical = repo_root / CANONICAL_PIN_FILE
    matches = MARIN_STYLE_PIN.findall(canonical.read_text())
    if len(matches) != 1:
        raise ValueError(f"expected one marin-style revision in {canonical}")
    return matches[0]


def _tracked_revision_paths(repo_root: Path, revision: str) -> frozenset[str]:
    result = subprocess.run(
        ["git", "grep", "--fixed-strings", "--name-only", revision, "--"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode not in {0, 1}:
        result.check_returncode()
    return frozenset(result.stdout.splitlines())


def _is_direct_pin_path(path: str) -> bool:
    if path in {CANONICAL_PIN_FILE, "pyproject.toml"}:
        return True
    relative = PurePosixPath(path)
    return relative.suffix in {".yaml", ".yml"} and relative.parts[:2] == (".github", "workflows")


def _direct_pin_files(
    repo_root: Path,
    *,
    revision: str,
    managed_paths: frozenset[str],
    uses_uv_lock: bool,
) -> tuple[str, ...]:
    ignored = {*managed_paths, MARIN_STYLE_MANIFEST}
    if uses_uv_lock:
        ignored.add(UV_LOCK_FILE)
    candidates = _tracked_revision_paths(repo_root, revision) - ignored
    direct: list[str] = []
    unexpected: list[str] = []
    for relative in sorted(candidates):
        if not _is_direct_pin_path(relative):
            unexpected.append(relative)
            continue
        matching_lines = [line for line in (repo_root / relative).read_text().splitlines() if revision in line]
        if not matching_lines or any(
            "marin-style" not in line.lower() and "marin_style_rev" not in line.lower() for line in matching_lines
        ):
            unexpected.append(relative)
            continue
        direct.append(relative)
    if unexpected:
        raise ValueError(f"unexpected files reference the pinned marin-style revision: {unexpected}")
    if CANONICAL_PIN_FILE not in direct:
        raise ValueError(f"{CANONICAL_PIN_FILE} is not a direct marin-style pin")
    return tuple(direct)


def _locked_marin_style(repo_root: Path) -> LockedMarinStyle | None:
    lock_path = repo_root / UV_LOCK_FILE
    if not lock_path.exists():
        return None
    with lock_path.open("rb") as lock_file:
        payload = tomllib.load(lock_file)
    packages = [package for package in payload.get("package", []) if package.get("name") == MARIN_STYLE_PACKAGE]
    if not packages:
        return None
    if len(packages) != 1:
        raise ValueError("uv.lock must contain exactly one marin-style package")
    package = packages[0]
    source = package.get("source")
    version = package.get("version")
    if not isinstance(source, dict) or not isinstance(source.get("git"), str) or not isinstance(version, str):
        raise ValueError("uv.lock contains an invalid marin-style package record")
    return LockedMarinStyle(version=version, source=source["git"])


def _replace_pin_files(repo_root: Path, pin_files: tuple[str, ...], *, old_revision: str, new_revision: str) -> None:
    for relative in pin_files:
        path = repo_root / relative
        text = path.read_text()
        updated = text.replace(old_revision, new_revision)
        if updated == text or old_revision in updated:
            raise ValueError(f"failed to replace marin-style revision in {path}")
        path.write_text(updated)


def _update_uv_lock(repo_root: Path, *, old_revision: str, new_revision: str) -> None:
    subprocess.run(["uv", "lock", "--upgrade-package", MARIN_STYLE_PACKAGE], cwd=repo_root, check=True)
    package = _locked_marin_style(repo_root)
    if package is None or new_revision not in package.source:
        raise ValueError("uv.lock does not contain the target marin-style revision")
    if old_revision in (repo_root / UV_LOCK_FILE).read_text():
        raise ValueError("uv.lock still contains the previous marin-style revision")


def changed_worktree_files(repo_root: Path) -> tuple[str, ...]:
    """Return tracked and untracked changes relative to the checked-out base."""
    tracked = _run("git", "diff", "--name-only", cwd=repo_root)
    untracked = _run("git", "ls-files", "--others", "--exclude-standard", cwd=repo_root)
    return tuple(sorted({*tracked.splitlines(), *untracked.splitlines()}))


def validate_changed_files(files: Iterable[str], *, policy: PullRequestPolicy) -> tuple[str, ...]:
    """Return sorted changed files after enforcing the generated update boundary."""
    changed = tuple(sorted(set(files)))
    unexpected = tuple(path for path in changed if path not in policy.allowed_files)
    if unexpected:
        raise ValueError(f"marin-style update changed unexpected files: {list(unexpected)}")
    return changed


def generate_update(*, repo_root: Path, base_branch: str, target_revision: str) -> GeneratedUpdate:
    """Update a consumer checkout and validate its complete generated diff."""
    if REVISION.fullmatch(target_revision) is None:
        raise ValueError("target revision must be a full lowercase commit SHA")
    old_revision = _old_revision(repo_root)
    if old_revision == target_revision:
        raise ValueError("consumer already pins the target marin-style revision")
    old_manifest = _checked_in_manifest(repo_root, expected_revision=old_revision)
    if _manifest_for_revision(old_revision) != old_manifest:
        raise ValueError("checked-in manifest does not match the pinned marin-style revision")
    new_manifest = _manifest_for_revision(target_revision)
    old_paths = frozenset(path for path, _ in old_manifest.files)
    new_paths = frozenset(path for path, _ in new_manifest.files)

    locked_marin_style = _locked_marin_style(repo_root)
    if locked_marin_style is not None and old_revision not in locked_marin_style.source:
        raise ValueError("uv.lock does not contain the pinned marin-style revision")
    uses_uv_lock = locked_marin_style is not None
    pin_files = _direct_pin_files(
        repo_root,
        revision=old_revision,
        managed_paths=old_paths,
        uses_uv_lock=uses_uv_lock,
    )
    _replace_pin_files(repo_root, pin_files, old_revision=old_revision, new_revision=target_revision)
    subprocess.run(
        [
            "uvx",
            "--from",
            f"git+{MARIN_STYLE_REPOSITORY}@{target_revision}",
            "marin-style",
            "sync",
            "--repo-root",
            str(repo_root),
        ],
        check=True,
    )
    if _checked_in_manifest(repo_root, expected_revision=target_revision) != new_manifest:
        raise ValueError("marin-style sync wrote a manifest that differs from the installed package")
    if uses_uv_lock:
        _update_uv_lock(repo_root, old_revision=old_revision, new_revision=target_revision)

    lock_files = {UV_LOCK_FILE} if uses_uv_lock else set()
    policy = PullRequestPolicy(
        base_branch=base_branch,
        head_branch=MARIN_STYLE_BRANCH,
        title=MARIN_STYLE_TITLE,
        allowed_files=frozenset({*pin_files, *lock_files, *old_paths, *new_paths, MARIN_STYLE_MANIFEST}),
    )
    changed_files = validate_changed_files(changed_worktree_files(repo_root), policy=policy)
    if not changed_files:
        raise ValueError("marin-style update produced no changed files")
    return GeneratedUpdate(
        old_revision=old_revision,
        new_revision=target_revision,
        changed_files=changed_files,
        policy=policy,
    )


def _open_pull_request(repository: str, policy: PullRequestPolicy) -> str:
    payload = _gh_json(
        "pr",
        "list",
        "--repo",
        repository,
        "--head",
        policy.head_branch,
        "--base",
        policy.base_branch,
        "--state",
        "open",
        "--json",
        "url",
    )
    if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
        raise ValueError(f"GitHub returned an invalid pull request list: {payload!r}")
    if len(payload) > 1:
        raise ValueError(f"found multiple open pull requests for {policy.head_branch!r}")
    if not payload:
        return ""
    url = payload[0].get("url")
    if not isinstance(url, str):
        raise ValueError(f"GitHub returned an invalid pull request URL: {url!r}")
    return url


def prepare_update_branch(*, repo_root: Path, repository: str, policy: PullRequestPolicy) -> UpdateBranch:
    """Reset the automation branch to the consumer's current default branch."""
    if changed_worktree_files(repo_root):
        raise ValueError("consumer checkout is not clean")
    subprocess.run(["git", "fetch", "origin", policy.base_branch], cwd=repo_root, check=True)
    pull_request_url = _open_pull_request(repository, policy)
    remote_branch = subprocess.run(
        ["git", "ls-remote", "--exit-code", "--heads", "origin", policy.head_branch],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if remote_branch.returncode == 0:
        fields = remote_branch.stdout.split()
        if len(fields) != 2 or fields[1] != f"refs/heads/{policy.head_branch}":
            raise ValueError(f"git returned an invalid remote branch: {remote_branch.stdout!r}")
        expected_remote_sha = fields[0]
        push_mode = BranchPushMode.FORCE_WITH_LEASE
    elif remote_branch.returncode == 2:
        expected_remote_sha = ""
        push_mode = BranchPushMode.CREATE
    else:
        remote_branch.check_returncode()
        raise AssertionError("unreachable")
    if pull_request_url and push_mode is not BranchPushMode.FORCE_WITH_LEASE:
        raise ValueError("open automation pull request has no remote branch")
    if not pull_request_url and push_mode is BranchPushMode.FORCE_WITH_LEASE:
        raise ValueError("automation branch exists without an open pull request")
    subprocess.run(
        ["git", "switch", "-C", policy.head_branch, f"origin/{policy.base_branch}"],
        cwd=repo_root,
        check=True,
    )
    return UpdateBranch(
        expected_remote_sha=expected_remote_sha,
        pull_request_url=pull_request_url,
        push_mode=push_mode,
    )


def _commit_update(repo_root: Path, update: GeneratedUpdate) -> str:
    changed_files = validate_changed_files(changed_worktree_files(repo_root), policy=update.policy)
    if changed_files != update.changed_files:
        raise ValueError("consumer changed after marin-style update validation")
    subprocess.run(["git", "config", "user.name", "github-actions[bot]"], cwd=repo_root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"],
        cwd=repo_root,
        check=True,
    )
    subprocess.run(["git", "add", "--", *changed_files], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-m", update.policy.title], cwd=repo_root, check=True)
    return _run("git", "rev-parse", "HEAD", cwd=repo_root).strip()


def _push_update_branch(repo_root: Path, branch: UpdateBranch, policy: PullRequestPolicy) -> None:
    push_args = ["git", "push"]
    if branch.push_mode is BranchPushMode.FORCE_WITH_LEASE:
        push_args.append(f"--force-with-lease=refs/heads/{policy.head_branch}:{branch.expected_remote_sha}")
    push_args.extend(["origin", f"HEAD:{policy.head_branch}"])
    subprocess.run(push_args, cwd=repo_root, check=True)


def _upsert_pull_request(
    *,
    repository: str,
    policy: PullRequestPolicy,
    pull_request_url: str,
    body_file: Path,
) -> str:
    if pull_request_url:
        label_args = [argument for label in DEPENDENCY_UPDATE_LABELS for argument in ("--add-label", label)]
        subprocess.run(
            [
                "gh",
                "pr",
                "edit",
                pull_request_url,
                "--repo",
                repository,
                "--title",
                policy.title,
                "--body-file",
                str(body_file),
                *label_args,
            ],
            check=True,
        )
        return pull_request_url
    label_args = [argument for label in DEPENDENCY_UPDATE_LABELS for argument in ("--label", label)]
    subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            repository,
            "--base",
            policy.base_branch,
            "--head",
            policy.head_branch,
            "--title",
            policy.title,
            "--body-file",
            str(body_file),
            *label_args,
        ],
        check=True,
    )
    payload = _gh_json("pr", "view", policy.head_branch, "--repo", repository, "--json", "url")
    if not isinstance(payload, dict) or not isinstance(payload.get("url"), str):
        raise ValueError(f"GitHub returned an invalid created pull request: {payload!r}")
    return payload["url"]


def publish_update(
    *,
    repo_root: Path,
    repository: str,
    branch: UpdateBranch,
    update: GeneratedUpdate,
    app_slug: str,
) -> PublishedPullRequest:
    """Commit, push, and upsert one validated consumer update."""
    if branch.pull_request_url:
        validated_pull_request(
            pull_request_snapshot(branch.pull_request_url, repository),
            policy=update.policy,
            expected_app_slug=app_slug,
            expected_head_sha=branch.expected_remote_sha,
        )
    head_sha = _commit_update(repo_root, update)
    _push_update_branch(repo_root, branch, update.policy)
    with tempfile.TemporaryDirectory() as temp_dir:
        body_file = Path(temp_dir) / "body.md"
        body_file.write_text(PR_BODY.format(revision=update.new_revision))
        pull_request_url = _upsert_pull_request(
            repository=repository,
            policy=update.policy,
            pull_request_url=branch.pull_request_url,
            body_file=body_file,
        )
    validated_pull_request(
        pull_request_snapshot(pull_request_url, repository),
        policy=update.policy,
        expected_app_slug=app_slug,
        expected_head_sha=head_sha,
    )
    return PublishedPullRequest(head_sha=head_sha, url=pull_request_url)


def pull_request_snapshot(pr: str, repository: str) -> PullRequestSnapshot:
    """Read the identity and merge boundary of one pull request."""
    payload = _gh_json(
        "pr",
        "view",
        pr,
        "--repo",
        repository,
        "--json",
        "author,baseRefName,files,headRefName,headRefOid,state,title,url",
    )
    if not isinstance(payload, dict):
        raise ValueError(f"GitHub returned an invalid pull request: {payload!r}")
    return PullRequestSnapshot(
        author=payload["author"]["login"],
        base_branch=payload["baseRefName"],
        files=tuple(sorted(file["path"] for file in payload["files"])),
        head_branch=payload["headRefName"],
        head_sha=payload["headRefOid"],
        state=payload["state"],
        title=payload["title"],
        url=payload["url"],
    )


def validated_pull_request(
    pull_request: PullRequestSnapshot,
    *,
    policy: PullRequestPolicy,
    expected_app_slug: str,
    expected_head_sha: str,
) -> PullRequestSnapshot:
    """Return a pull request after verifying its generated update boundary."""
    expected_author = f"app/{expected_app_slug}"
    if pull_request.author != expected_author:
        raise ValueError(f"unexpected pull request author {pull_request.author!r}; expected {expected_author!r}")
    if pull_request.base_branch != policy.base_branch or pull_request.head_branch != policy.head_branch:
        raise ValueError("unexpected pull request branch")
    if pull_request.head_sha != expected_head_sha:
        raise ValueError(f"unexpected head SHA {pull_request.head_sha!r}; expected {expected_head_sha!r}")
    if pull_request.title != policy.title:
        raise ValueError(f"unexpected pull request title {pull_request.title!r}")
    if not pull_request.files:
        raise ValueError("pull request has no changed files")
    unexpected_files = sorted(set(pull_request.files) - policy.allowed_files)
    if unexpected_files:
        raise ValueError(f"pull request contains unexpected files: {unexpected_files}")
    return pull_request


def protected_check_rows(pr: str, repository: str) -> tuple[CheckRow, ...]:
    """Read checks GitHub marks as required for the pull request."""
    result = subprocess.run(
        ["gh", "pr", "checks", pr, "--repo", repository, "--required", "--json", "name,bucket"],
        capture_output=True,
        text=True,
    )
    if not result.stdout.strip():
        return ()
    payload = json.loads(result.stdout)
    if not isinstance(payload, list):
        raise ValueError(f"GitHub returned invalid required checks: {payload!r}")
    return tuple(CheckRow.from_json(row) for row in payload)


def evaluate_protected_checks(rows: Iterable[CheckRow]) -> RequiredCheckGate:
    """Classify GitHub's required checks without maintaining a local check list."""
    buckets: dict[str, str] = {}
    for row in rows:
        if row.name in buckets:
            raise ValueError(f"GitHub returned duplicate required check rows for {row.name!r}")
        buckets[row.name] = row.bucket
    if not buckets:
        return RequiredCheckGate(failing=(), missing=("protected status checks",), pending=())
    pending = tuple(name for name, bucket in buckets.items() if bucket == PENDING_CHECK_BUCKET)
    failing = tuple(
        name for name, bucket in buckets.items() if bucket not in PASS_CHECK_BUCKETS | {PENDING_CHECK_BUCKET}
    )
    return RequiredCheckGate(failing=tuple(sorted(failing)), missing=(), pending=tuple(sorted(pending)))


def evaluate_merge(state: str, checks: RequiredCheckGate) -> MergeDecision:
    """Choose the next merge action from pull-request state and protected checks."""
    if state == "MERGED":
        return MergeDecision.DONE
    if state != "OPEN" or checks.failing:
        return MergeDecision.FAIL
    if checks.passed:
        return MergeDecision.MERGE
    return MergeDecision.WAIT


def merge_when_protected_checks_green(
    *,
    pr: str,
    repository: str,
    app_slug: str,
    policy: PullRequestPolicy,
    expected_head_sha: str,
    timeout: float,
    poll_interval: float,
) -> None:
    """Poll protected checks, then perform a synchronous head-bound merge."""
    deadline = time.monotonic() + timeout
    while True:
        snapshot = validated_pull_request(
            pull_request_snapshot(pr, repository),
            policy=policy,
            expected_app_slug=app_slug,
            expected_head_sha=expected_head_sha,
        )
        checks = evaluate_protected_checks(protected_check_rows(pr, repository))
        decision = evaluate_merge(snapshot.state, checks)
        if decision is MergeDecision.DONE:
            return
        if decision is MergeDecision.FAIL:
            raise RuntimeError(f"marin-style update is blocked: state={snapshot.state}, failing={list(checks.failing)}")
        if decision is MergeDecision.MERGE:
            subprocess.run(
                [
                    "gh",
                    "pr",
                    "merge",
                    pr,
                    "--repo",
                    repository,
                    "--squash",
                    "--admin",
                    "--match-head-commit",
                    expected_head_sha,
                ],
                check=True,
            )
            if pull_request_snapshot(pr, repository).state != "MERGED":
                raise RuntimeError("merge command completed without merging the pull request")
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "required checks did not finish before the merge deadline: "
                f"missing={list(checks.missing)}, pending={list(checks.pending)}"
            )
        time.sleep(min(poll_interval, max(0, deadline - time.monotonic())))


def _preflight(repository: str, base_branch: str) -> None:
    owner, separator, name = repository.partition("/")
    if owner != "marin-community" or separator != "/" or not name or "/" in name:
        raise ValueError(f"invalid consumer repository: {repository!r}")
    payload = _gh_json("repo", "view", repository, "--json", "defaultBranchRef")
    if not isinstance(payload, dict) or payload.get("defaultBranchRef", {}).get("name") != base_branch:
        raise ValueError(f"{base_branch!r} is not the default branch for {repository}")
    for label in DEPENDENCY_UPDATE_LABELS:
        _run("gh", "api", f"repos/{repository}/labels/{label}")


def update_consumer(
    *,
    repo_root: Path,
    repository: str,
    base_branch: str,
    app_slug: str,
    merge: bool,
    timeout: float = 2400,
    poll_interval: float = 30,
) -> ConsumerUpdateResult:
    """Publish the latest marin-style update and optionally merge it after protected checks."""
    _preflight(repository, base_branch)
    target_revision = resolve_target_revision()
    initial_policy = PullRequestPolicy(
        base_branch=base_branch,
        head_branch=MARIN_STYLE_BRANCH,
        title=MARIN_STYLE_TITLE,
        allowed_files=frozenset(),
    )
    branch = prepare_update_branch(repo_root=repo_root, repository=repository, policy=initial_policy)
    old_revision = _old_revision(repo_root)
    if old_revision == target_revision:
        old_manifest = _checked_in_manifest(repo_root, expected_revision=old_revision)
        if _manifest_for_revision(old_revision) != old_manifest:
            raise ValueError("checked-in manifest does not match the pinned marin-style revision")
        if branch.pull_request_url:
            raise ValueError(f"consumer is current but has an open automation PR: {branch.pull_request_url}")
        return ConsumerUpdateResult(status=ConsumerUpdateStatus.CURRENT, pull_request_url="")

    update = generate_update(repo_root=repo_root, base_branch=base_branch, target_revision=target_revision)
    published = publish_update(
        repo_root=repo_root,
        repository=repository,
        branch=branch,
        update=update,
        app_slug=app_slug,
    )
    if merge:
        merge_when_protected_checks_green(
            pr=published.url,
            repository=repository,
            app_slug=app_slug,
            policy=update.policy,
            expected_head_sha=published.head_sha,
            timeout=timeout,
            poll_interval=poll_interval,
        )
    status = ConsumerUpdateStatus.MERGED if merge else ConsumerUpdateStatus.PUBLISHED
    return ConsumerUpdateResult(status=status, pull_request_url=published.url)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--base-branch", required=True)
    parser.add_argument("--app-slug", required=True)
    parser.add_argument("--merge", action="store_true")
    parser.add_argument("--timeout", type=float, default=2400)
    parser.add_argument("--poll-interval", type=float, default=30)
    return parser


def main() -> None:
    args = _parser().parse_args()
    result = update_consumer(
        repo_root=args.repo_root.resolve(),
        repository=args.repository,
        base_branch=args.base_branch,
        app_slug=args.app_slug,
        merge=args.merge,
        timeout=args.timeout,
        poll_interval=args.poll_interval,
    )
    if result.status is ConsumerUpdateStatus.CURRENT:
        print(f"{args.repository} already pins the latest marin-style revision")
    else:
        print(f"{result.status}: {result.pull_request_url}")


if __name__ == "__main__":
    main()
