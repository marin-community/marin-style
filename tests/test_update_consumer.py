import hashlib
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from marin_style.update_consumer import (
    BranchPushMode,
    CheckRow,
    GeneratedManifest,
    MergeDecision,
    PullRequestPolicy,
    PullRequestSnapshot,
    UpdateBranch,
    evaluate_merge,
    evaluate_protected_checks,
    generate_update,
    publish_update,
    validated_pull_request,
)

OLD_REVISION = "a" * 40
NEW_REVISION = "b" * 40
APP_SLUG = "marin-external-runtime-updater"


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _manifest(revision: str, files: dict[str, str]) -> dict[str, object]:
    return {
        "files": {
            path: f"sha256:{hashlib.sha256(content.encode()).hexdigest()}" for path, content in sorted(files.items())
        },
        "format": 1,
        "revision": revision,
    }


def _consumer_repository(tmp_path: Path, *, with_lock: bool = False) -> tuple[Path, dict[str, object]]:
    repository = tmp_path / "consumer"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Test User")
    _git(repository, "config", "user.email", "test@example.com")

    old_files = {".agents/skills/consult-echo/scripts/echo.py": f"# marin-style@{OLD_REVISION}\n"}
    old_manifest = _manifest(OLD_REVISION, old_files)
    for relative, content in old_files.items():
        path = repository / relative
        path.parent.mkdir(parents=True)
        path.write_text(content)
    manifest_path = repository / ".agents/marin-style/manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps(old_manifest, indent=2, sort_keys=True) + "\n")

    pin = f"git+https://github.com/marin-community/marin-style@{OLD_REVISION}\n"
    precommit = repository / "infra/pre-commit.py"
    precommit.parent.mkdir()
    precommit.write_text(pin)
    ci = repository / ".github/workflows/marin-ci.yaml"
    ci.parent.mkdir(parents=True)
    ci.write_text(f"MARIN_STYLE_REV: {OLD_REVISION}\n")
    update_workflow = repository / ".github/workflows/marin-style-update.yaml"
    update_workflow.write_text(
        f"uses: marin-community/marin-style/actions/update-consumer@{OLD_REVISION}\n"
    )
    (repository / "README.md").write_text("consumer\n")

    if with_lock:
        (repository / "pyproject.toml").write_text(
            f'marin-style = "marin-style @ git+https://github.com/marin-community/marin-style@{OLD_REVISION}"\n'
        )
        (repository / "uv.lock").write_text(
            "version = 1\n\n"
            "[[package]]\n"
            'name = "marin-style"\n'
            'version = "0.4.0"\n'
            f'source = {{ git = "https://github.com/marin-community/marin-style?rev={OLD_REVISION}#{OLD_REVISION}" }}\n'
        )

    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "initial")
    return repository, old_manifest


def _install_fake_style_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    old_manifest: dict[str, object],
    new_files: dict[str, str] | None = None,
) -> None:
    if new_files is None:
        new_files = {".agents/skills/consult-echo/scripts/echo.py": f"# marin-style@{NEW_REVISION}\n"}
    data = {
        OLD_REVISION: {"files": {}, "manifest": old_manifest},
        NEW_REVISION: {"files": new_files, "manifest": _manifest(NEW_REVISION, new_files)},
    }
    data_file = tmp_path / "style-data.json"
    data_file.write_text(json.dumps(data))
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    uvx = binary_dir / "uvx"
    uvx.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "data = json.loads(pathlib.Path(os.environ['FAKE_STYLE_DATA']).read_text())\n"
        "revision = sys.argv[2].rsplit('@', 1)[1]\n"
        "entry = data[revision]\n"
        "if sys.argv[-1] == 'managed-files':\n"
        "    print(json.dumps(entry['manifest'], indent=2, sort_keys=True))\n"
        "else:\n"
        "    root = pathlib.Path(sys.argv[sys.argv.index('--repo-root') + 1])\n"
        "    for relative, content in entry['files'].items():\n"
        "        path = root / relative\n"
        "        path.parent.mkdir(parents=True, exist_ok=True)\n"
        "        path.write_text(content)\n"
        "    manifest = root / '.agents/marin-style/manifest.json'\n"
        "    manifest.parent.mkdir(parents=True, exist_ok=True)\n"
        "    manifest.write_text(json.dumps(entry['manifest'], indent=2, sort_keys=True) + '\\n')\n"
    )
    uvx.chmod(0o755)
    uv = binary_dir / "uv"
    uv.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib\n"
        "path = pathlib.Path('uv.lock')\n"
        "path.write_text(path.read_text().replace(os.environ['OLD_REVISION'], os.environ['NEW_REVISION']))\n"
    )
    uv.chmod(0o755)
    monkeypatch.setenv("FAKE_STYLE_DATA", str(data_file))
    monkeypatch.setenv("OLD_REVISION", OLD_REVISION)
    monkeypatch.setenv("NEW_REVISION", NEW_REVISION)
    monkeypatch.setenv("PATH", f"{binary_dir}:{os.environ['PATH']}")


def test_generate_update_self_bumps_workflow_and_owned_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, old_manifest = _consumer_repository(tmp_path)
    _install_fake_style_tools(tmp_path, monkeypatch, old_manifest=old_manifest)

    update = generate_update(repo_root=repository, base_branch="main", target_revision=NEW_REVISION)

    assert update.changed_files == (
        ".agents/marin-style/manifest.json",
        ".agents/skills/consult-echo/scripts/echo.py",
        ".github/workflows/marin-ci.yaml",
        ".github/workflows/marin-style-update.yaml",
        "infra/pre-commit.py",
    )
    assert NEW_REVISION in (repository / ".github/workflows/marin-style-update.yaml").read_text()
    assert NEW_REVISION in (repository / "infra/pre-commit.py").read_text()
    assert (repository / "README.md").read_text() == "consumer\n"


def test_generate_update_infers_and_regenerates_root_lockfile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, old_manifest = _consumer_repository(tmp_path, with_lock=True)
    _install_fake_style_tools(tmp_path, monkeypatch, old_manifest=old_manifest)

    update = generate_update(repo_root=repository, base_branch="main", target_revision=NEW_REVISION)

    assert {"pyproject.toml", "uv.lock"} <= set(update.changed_files)
    assert OLD_REVISION not in (repository / "uv.lock").read_text()
    assert NEW_REVISION in (repository / "uv.lock").read_text()


def test_generate_update_rejects_consumer_owned_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, old_manifest = _consumer_repository(tmp_path)
    _install_fake_style_tools(tmp_path, monkeypatch, old_manifest=old_manifest)
    (repository / "README.md").write_text("unrelated\n")

    with pytest.raises(ValueError, match="unexpected files"):
        generate_update(repo_root=repository, base_branch="main", target_revision=NEW_REVISION)


def test_generate_update_rejects_unrecognized_revision_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, old_manifest = _consumer_repository(tmp_path)
    _install_fake_style_tools(tmp_path, monkeypatch, old_manifest=old_manifest)
    (repository / "README.md").write_text(f"old style: {OLD_REVISION}\n")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "add hidden reference")

    with pytest.raises(ValueError, match="unexpected files reference"):
        generate_update(repo_root=repository, base_branch="main", target_revision=NEW_REVISION)


def test_manifest_rejects_consumer_owned_agent_path() -> None:
    text = json.dumps(
        {
            "files": {".agents/ops/runbook.md": f"sha256:{'0' * 64}"},
            "format": 1,
            "revision": NEW_REVISION,
        }
    )

    with pytest.raises(ValueError):
        GeneratedManifest.from_text(text, expected_revision=NEW_REVISION)


def test_protected_checks_gate_merge_without_a_local_check_catalog() -> None:
    pending = evaluate_protected_checks(
        [CheckRow(name="lint", bucket="pass"), CheckRow(name="tests", bucket="pending")]
    )
    passing = evaluate_protected_checks(
        [CheckRow(name="lint", bucket="pass"), CheckRow(name="docs", bucket="skipping")]
    )
    failing = evaluate_protected_checks([CheckRow(name="tests", bucket="fail")])

    assert evaluate_merge("OPEN", pending) is MergeDecision.WAIT
    assert evaluate_merge("OPEN", passing) is MergeDecision.MERGE
    assert evaluate_merge("OPEN", failing) is MergeDecision.FAIL
    assert evaluate_merge("OPEN", evaluate_protected_checks([])) is MergeDecision.WAIT


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("author", "octocat"),
        ("head_sha", "c" * 40),
        ("files", ("src/backdoor.py",)),
        ("files", ()),
    ],
    ids=["author", "head", "files", "empty"],
)
def test_pull_request_validation_rejects_changes_outside_generated_boundary(field: str, value: object) -> None:
    policy = PullRequestPolicy(
        base_branch="main",
        head_branch="automation/marin-style",
        title="[dependencies] Advance marin-style",
        allowed_files=frozenset({"infra/pre-commit.py"}),
    )
    pull_request = PullRequestSnapshot(
        author=f"app/{APP_SLUG}",
        base_branch="main",
        files=("infra/pre-commit.py",),
        head_branch="automation/marin-style",
        head_sha=NEW_REVISION,
        state="OPEN",
        title="[dependencies] Advance marin-style",
        url="https://github.com/marin-community/harbor/pull/123",
    )

    with pytest.raises(ValueError):
        validated_pull_request(
            replace(pull_request, **{field: value}),
            policy=policy,
            expected_app_slug=APP_SLUG,
            expected_head_sha=NEW_REVISION,
        )


def test_publish_update_pushes_only_the_validated_diff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, old_manifest = _consumer_repository(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(repository, "remote", "add", "origin", str(remote))
    _git(repository, "push", "-u", "origin", "main")
    _git(repository, "switch", "-c", "automation/marin-style")
    _install_fake_style_tools(tmp_path, monkeypatch, old_manifest=old_manifest)
    update = generate_update(repo_root=repository, base_branch="main", target_revision=NEW_REVISION)

    fake_gh = tmp_path / "bin/gh"
    fake_gh.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "if sys.argv[1:3] == ['pr', 'view']:\n"
        "    fields = sys.argv[sys.argv.index('--json') + 1]\n"
        "    if fields == 'url':\n"
        "        print(json.dumps({'url': 'https://github.com/marin-community/harbor/pull/123'}))\n"
        "    else:\n"
        "        sha = __import__('subprocess').check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()\n"
        "        print(json.dumps({\n"
        "            'author': {'login': 'app/marin-external-runtime-updater'},\n"
        "            'baseRefName': 'main',\n"
        "            'files': [{'path': path} for path in [\n"
        "                '.agents/marin-style/manifest.json',\n"
        "                '.agents/skills/consult-echo/scripts/echo.py',\n"
        "                '.github/workflows/marin-ci.yaml',\n"
        "                '.github/workflows/marin-style-update.yaml',\n"
        "                'infra/pre-commit.py',\n"
        "            ]],\n"
        "            'headRefName': 'automation/marin-style',\n"
        "            'headRefOid': sha,\n"
        "            'state': 'OPEN',\n"
        "            'title': '[dependencies] Advance marin-style',\n"
        "            'url': 'https://github.com/marin-community/harbor/pull/123',\n"
        "        }))\n"
    )
    fake_gh.chmod(0o755)
    monkeypatch.chdir(repository)
    published = publish_update(
        repo_root=repository,
        repository="marin-community/harbor",
        branch=UpdateBranch(expected_remote_sha="", pull_request_url="", push_mode=BranchPushMode.CREATE),
        update=update,
        app_slug=APP_SLUG,
    )

    remote_sha = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "automation/marin-style"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert published.head_sha == remote_sha
    assert published.url == "https://github.com/marin-community/harbor/pull/123"
    assert _git(repository, "show", f"{remote_sha}:infra/pre-commit.py").endswith(NEW_REVISION)
