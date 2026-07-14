# Copyright The Marin Authors
# SPDX-License-Identifier: Apache-2.0

"""Portable pre-commit linter for Marin-style repositories.

Runs pinned ruff/black/pyrefly checks and an optional license-header check over a
repository's files. The repository root is discovered from the current working
directory, and per-repo behaviour is read from a `[tool.marin-style]` table in the
root `pyproject.toml`. The `--review` path hands off to the agentic lint-review
runner in `marin_style.lint_review`.
"""

import fnmatch
import io
import pathlib
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass

import click

from marin_style.lint_review import LINT_REVIEW_AGENT_DEFAULT, run_lint_review

DEFAULT_RUFF_VERSION = "0.14.3"
DEFAULT_BLACK_VERSION = "25.9.0"
PYREFLY_SPEC = "pyrefly>=1.0.0,<1.1.0"

# Built-in exclude globs, matched against repo-relative paths. A repo's own
# `exclude` list (from `[tool.marin-style]`) is appended to these.
EXCLUDE_PATTERNS = [
    ".git/**",
    ".github/**",
    "tests/snapshots/**",
    # grpc generated files
    "**/*_connect.py",
    "**/*_pb2.py",
    "**/*.gz",
    "**/*.pb",
    "**/*.index",
    "**/*.ico",
    "**/*.npy",
    "**/*.lock",
    "**/*.png",
    "**/*.jpg",
    "**/*.html",
    "**/*.jpeg",
    "**/*.gif",
    "**/*.mov",
    "**/*.mp4",
    "**/*.data-*",
    "**/package-lock.json",
    "**/__pycache__/**",
    "**/*.pyc",
    "**/*-template.yaml",
]

ALL_CHECKS = ("ruff-check", "ruff-format", "black", "typecheck", "license-header")
DEFAULT_CHECKS = ["ruff-check", "ruff-format"]
KNOWN_CONFIG_KEYS = {"checks", "include", "exclude", "license_header", "main_branch", "ruff_version", "black_version"}


@dataclass(frozen=True)
class StyleConfig:
    """Resolved `[tool.marin-style]` configuration for a repository."""

    root: pathlib.Path
    checks: list[str]
    include: list[str]
    exclude: list[str]
    license_header: pathlib.Path | None
    main_branch: str
    ruff_version: str
    black_version: str


def _repo_root() -> pathlib.Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return pathlib.Path(result.stdout.strip())


def load_config(root: pathlib.Path) -> StyleConfig:
    """Read and validate the `[tool.marin-style]` table from the repo's pyproject.toml."""
    pyproject = root / "pyproject.toml"
    table: dict = {}
    if pyproject.exists():
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
        table = data.get("tool", {}).get("marin-style", {})

    unknown = set(table) - KNOWN_CONFIG_KEYS
    if unknown:
        raise ValueError(
            f"Unknown [tool.marin-style] key(s): {', '.join(sorted(unknown))}. "
            f"Valid keys: {', '.join(sorted(KNOWN_CONFIG_KEYS))}"
        )

    checks = table.get("checks", list(DEFAULT_CHECKS))
    invalid = [c for c in checks if c not in ALL_CHECKS]
    if invalid:
        raise ValueError(f"Unknown check(s): {', '.join(invalid)}. Valid checks: {', '.join(ALL_CHECKS)}")

    license_header = None
    if "license-header" in checks:
        header = table.get("license_header")
        if not header:
            raise ValueError("[tool.marin-style] requires `license_header` when the 'license-header' check is enabled")
        license_header = root / header

    return StyleConfig(
        root=root,
        checks=list(checks),
        include=list(table.get("include", [])),
        exclude=list(table.get("exclude", [])),
        license_header=license_header,
        main_branch=table.get("main_branch", "main"),
        ruff_version=table.get("ruff_version", DEFAULT_RUFF_VERSION),
        black_version=table.get("black_version", DEFAULT_BLACK_VERSION),
    )


@dataclass
class CheckResult:
    name: str
    exit_code: int
    output: str


_check_results: list[CheckResult] = []


def _record(name: str, exit_code: int, output: str = "") -> int:
    """Print a one-line status and stash failure details for the summary."""
    status = "ok" if exit_code == 0 else "FAIL"
    click.echo(f"  {name:.<40s} {status}")
    _check_results.append(CheckResult(name=name, exit_code=exit_code, output=output.rstrip()))
    return exit_code


def run_cmd(cmd: list[str], root: pathlib.Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=root, capture_output=True, text=True)


def _base_ref(root: pathlib.Path, main_branch: str) -> str | None:
    """Resolve the branch base ref: prefer `origin/<main_branch>`, fall back to the local branch."""
    for ref in (f"origin/{main_branch}", main_branch):
        r = subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref], cwd=root, capture_output=True, text=True)
        if r.returncode == 0:
            return ref
    return None


def _git_files(root: pathlib.Path, args: list[str]) -> list[pathlib.Path]:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return [root / f for f in result.stdout.strip().split("\n") if f]


def get_staged_files(root: pathlib.Path) -> list[pathlib.Path]:
    return _git_files(root, ["diff", "--cached", "--name-only", "--diff-filter=ACM"])


def get_unstaged_files(root: pathlib.Path) -> list[pathlib.Path]:
    return _git_files(root, ["diff", "--name-only", "--diff-filter=ACM"])


def get_branch_files(root: pathlib.Path, main_branch: str) -> list[pathlib.Path]:
    """Files changed on this branch versus the merge-base with the main branch."""
    base_ref = _base_ref(root, main_branch)
    if base_ref is None:
        return []
    merge_base = subprocess.run(
        ["git", "merge-base", base_ref, "HEAD"], cwd=root, capture_output=True, text=True
    ).stdout.strip()
    if not merge_base:
        return []
    return _git_files(root, ["diff", f"{merge_base}...HEAD", "--name-only", "--diff-filter=ACM"])


def get_changed_files(root: pathlib.Path, main_branch: str) -> list[pathlib.Path]:
    files: set[pathlib.Path] = set()
    files.update(get_staged_files(root))
    files.update(get_unstaged_files(root))
    files.update(get_branch_files(root, main_branch))
    return [f for f in files if f.exists()]


def get_all_files(root: pathlib.Path) -> list[pathlib.Path]:
    files = _git_files(root, ["ls-files"])
    return [f for f in files if f.exists()]


def _matches(file_path: pathlib.Path, root: pathlib.Path, patterns: list[str]) -> bool:
    relative_path = str(file_path.relative_to(root))
    return any(fnmatch.fnmatch(relative_path, pattern) for pattern in patterns)


def select_files(files: list[pathlib.Path], config: StyleConfig) -> list[pathlib.Path]:
    """Apply the built-in and configured excludes, then the optional include allowlist."""
    excludes = EXCLUDE_PATTERNS + config.exclude
    selected = [f for f in files if not _matches(f, config.root, excludes)]
    if config.include:
        selected = [f for f in selected if _matches(f, config.root, config.include)]
    return selected


def _python_files(files: list[pathlib.Path]) -> list[pathlib.Path]:
    return [f for f in files if f.suffix == ".py"]


def check_ruff_check(files: list[pathlib.Path], fix: bool, config: StyleConfig) -> int:
    py_files = _python_files(files)
    if not py_files:
        return 0
    args = ["uvx", f"ruff@{config.ruff_version}", "check"]
    if fix:
        args.extend(["--fix", "--exit-non-zero-on-fix"])
    args.extend(str(f.relative_to(config.root)) for f in py_files)
    result = run_cmd(args, config.root)
    return _record("Ruff check", result.returncode, (result.stdout + result.stderr).strip())


def check_ruff_format(files: list[pathlib.Path], fix: bool, config: StyleConfig) -> int:
    py_files = _python_files(files)
    if not py_files:
        return 0
    file_args = [str(f.relative_to(config.root)) for f in py_files]
    args = ["uvx", f"ruff@{config.ruff_version}", "format"]
    if not fix:
        args.append("--check")
    args.extend(file_args)
    result = run_cmd(args, config.root)
    return _record("Ruff format", result.returncode, (result.stdout + result.stderr).strip())


def check_black(files: list[pathlib.Path], fix: bool, config: StyleConfig) -> int:
    py_files = _python_files(files)
    if not py_files:
        return 0
    file_args = [str(f.relative_to(config.root)) for f in py_files]
    args = ["uvx", f"black@{config.black_version}", "--check"]
    if fix:
        args.append("--diff")
    args.extend(file_args)
    result = run_cmd(args, config.root)
    output = (result.stdout + result.stderr).strip()

    if result.returncode != 0 and fix:
        run_cmd(["uvx", f"black@{config.black_version}", *file_args], config.root)

    return _record("Black formatter", result.returncode, output)


def check_typecheck(files: list[pathlib.Path], fix: bool, config: StyleConfig) -> int:
    """Run pyrefly over the repo. Pyrefly reads the repo's own pyproject config for scope."""
    if not _python_files(files):
        return 0
    args = ["uvx", "--from", PYREFLY_SPEC, "pyrefly", "check"]
    baseline = config.root / ".pyrefly-baseline.json"
    if baseline.exists():
        args.extend(["--baseline", str(baseline.relative_to(config.root))])
    result = run_cmd(args, config.root)
    return _record("Pyrefly type checker", result.returncode, (result.stdout + result.stderr).strip())


def check_license_header(files: list[pathlib.Path], fix: bool, config: StyleConfig) -> int:
    license_file = config.license_header
    assert license_file is not None, "license_header must be configured for the license-header check"

    py_files = _python_files(files)
    if not py_files:
        return 0

    label = f"License headers ({license_file.relative_to(config.root)})"
    if not license_file.exists():
        return _record(label, 1, f"License header file not found: {license_file}")

    license_template = license_file.read_text().strip()
    license_lines = [f"# {line}" if line else "#" for line in license_template.split("\n")]
    expected_header = "\n".join(license_lines) + "\n"

    files_without_header = []
    buf = io.StringIO()

    for file_path in py_files:
        content = file_path.read_text()
        lines = content.split("\n")

        comment_lines = []
        start_idx = 1 if content.startswith("#!") else 0
        for line in lines[start_idx:]:
            stripped = line.lstrip()
            if stripped.startswith("#"):
                comment_lines.append(stripped[2:] if stripped[1:2] == " " else stripped[1:])
            elif stripped:
                break

        if license_template in "\n".join(comment_lines):
            continue

        files_without_header.append(file_path)
        if not fix:
            continue

        has_shebang = content.startswith("#!")
        shebang_line = lines[0] if has_shebang else ""
        rest_content = "\n".join(lines[1:]) if has_shebang else content

        if not rest_content.startswith(expected_header):
            rest_content = f"{expected_header}\n{rest_content}" if rest_content else expected_header

        new_content = f"{shebang_line}\n{rest_content}" if has_shebang else rest_content
        file_path.write_text(new_content)

    if files_without_header:
        buf.write(f"{len(files_without_header)} files missing license headers\n")
        for f in files_without_header:
            buf.write(f"  - {f.relative_to(config.root)}\n")
        return _record(label, 1, buf.getvalue())

    return _record(label, 0)


CheckFn = Callable[[list[pathlib.Path], bool, StyleConfig], int]

CHECK_FUNCTIONS: dict[str, CheckFn] = {
    "ruff-check": check_ruff_check,
    "ruff-format": check_ruff_format,
    "black": check_black,
    "typecheck": check_typecheck,
    "license-header": check_license_header,
}


def _collect_files(
    config: StyleConfig,
    all_files: bool,
    pre_commit: bool,
    changed_files: bool,
    input_files: tuple[str, ...],
) -> list[pathlib.Path]:
    files: set[pathlib.Path] = set()
    if all_files:
        files.update(get_all_files(config.root))
    elif pre_commit:
        files.update(get_staged_files(config.root))
    elif changed_files or not input_files:
        files.update(get_changed_files(config.root, config.main_branch))

    for f in input_files:
        path = config.root / f
        if path.exists():
            files.add(path)
        else:
            click.echo(f"Warning: skipping non-existent file: {f}")

    return sorted(f for f in files if f.exists())


@click.command()
@click.option("--fix", is_flag=True, help="Automatically fix issues where possible")
@click.option("--all-files", is_flag=True, help="Run checks on all tracked files, not just changed")
@click.option(
    "--changed-files",
    "changed_files",
    is_flag=True,
    help="Run checks on uncommitted and branch-specific changes",
)
@click.option(
    "--pre-commit",
    is_flag=True,
    help="Run checks on staged changes only (for git pre-commit hook)",
)
@click.option(
    "--review",
    is_flag=True,
    help="Run the advisory lint-review rule catalog over the branch diff via per-lane agents",
)
@click.option(
    "--agent-command",
    "agent_command",
    default=LINT_REVIEW_AGENT_DEFAULT,
    show_default=True,
    help="Headless agent invocation for --review (e.g. 'claude -p', 'codex exec').",
)
@click.option(
    "--lint-lane",
    "lint_lanes",
    multiple=True,
    help="With --review, run only these lane(s): complexity, interfaces, robustness, cruft, prose, meta. Repeatable.",
)
@click.option(
    "--lint-compose/--no-lint-compose",
    "lint_compose",
    default=True,
    show_default=True,
    help="With --review, merge lanes via the composer agent (default) or a deterministic concat.",
)
@click.option("--skip", multiple=True, help="Skip specific checks by name (e.g. ruff-check, black)")
@click.option("--only", multiple=True, help="Run only specific checks by name (e.g. ruff-check, black)")
@click.argument("files", nargs=-1)
def main(
    fix: bool,
    all_files: bool,
    changed_files: bool,
    pre_commit: bool,
    review: bool,
    agent_command: str,
    lint_lanes: tuple[str, ...],
    lint_compose: bool,
    skip: tuple[str, ...],
    only: tuple[str, ...],
    files: tuple[str, ...],
):
    config = load_config(_repo_root())

    if review:
        sys.exit(
            run_lint_review(
                agent_command,
                config.root,
                main_branch=config.main_branch,
                lane_names=list(lint_lanes) or None,
                compose=lint_compose,
            )
        )

    if skip and only:
        click.echo("Error: --only and --skip are mutually exclusive.", err=True)
        sys.exit(1)

    for name in (*skip, *only):
        if name not in ALL_CHECKS:
            click.echo(f"Error: unknown check '{name}'. Valid checks: {', '.join(ALL_CHECKS)}", err=True)
            sys.exit(1)

    active_checks = [c for c in config.checks if c not in set(skip)]
    if only:
        active_checks = [c for c in active_checks if c in set(only)]

    candidate_files = _collect_files(config, all_files, pre_commit, changed_files, files)
    selected_files = select_files(candidate_files, config)

    exit_codes = []
    for check_name in active_checks:
        check_fn = CHECK_FUNCTIONS[check_name]
        exit_codes.append(check_fn(selected_files, fix, config))

    failures = [r for r in _check_results if r.exit_code != 0 and r.output]
    if failures:
        click.echo(f"\n{'=' * 60}")
        click.echo("Failure details:\n")
        for r in failures:
            click.echo(f"--- {r.name} ---")
            click.echo(r.output)
            click.echo()

    click.echo("=" * 60)
    if any(exit_codes):
        click.echo("FAILED")
        click.echo("=" * 60)
        sys.exit(1)
    click.echo("OK")
    click.echo("=" * 60)
    sys.exit(0)


if __name__ == "__main__":
    main()
