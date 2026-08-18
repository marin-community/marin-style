import hashlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from marin_style.cli import main
from marin_style.vendor import MANIFEST_PATH, _render, check_sync, sync


def test_python_asset_keeps_shebang_and_pins_source_revision() -> None:
    source = Path("src/marin_style/assets/skills/consult-echo/scripts/echo.py")

    rendered = _render(source, "0.4.0", "abc123")

    assert rendered.startswith("#!/usr/bin/env -S uv run --script\n# Vendored from")
    assert "marin-style@abc123" in rendered
    assert "@MARIN_STYLE_REV@" not in rendered
    compile(rendered, str(source), "exec")


def test_sync_writes_manifest_and_check_accepts_generated_tree(tmp_path: Path) -> None:
    synced = sync(repo_root=tmp_path)

    manifest = json.loads((tmp_path / MANIFEST_PATH).read_text())
    assert manifest["format"] == 1
    assert not synced.manifest_drifted
    assert set(manifest["files"]) == {
        path.relative_to(tmp_path).as_posix()
        for path in (tmp_path / ".agents").rglob("*")
        if path.is_file() and path != tmp_path / MANIFEST_PATH
    }

    result = check_sync(repo_root=tmp_path)
    assert not result.missing
    assert not result.drifted
    assert not result.stale
    assert not result.manifest_drifted


def test_managed_files_command_matches_written_manifest(tmp_path: Path) -> None:
    sync(repo_root=tmp_path)

    result = CliRunner().invoke(main, ["managed-files"])

    assert result.exit_code == 0
    assert json.loads(result.output) == json.loads((tmp_path / MANIFEST_PATH).read_text())


def _add_stale_manifest_file(tmp_path: Path, content: bytes) -> Path:
    stale_path = tmp_path / ".agents/skills/removed/SKILL.md"
    stale_path.parent.mkdir(parents=True)
    stale_path.write_bytes(content)
    manifest_path = tmp_path / MANIFEST_PATH
    manifest = json.loads(manifest_path.read_text())
    manifest["files"][stale_path.relative_to(tmp_path).as_posix()] = f"sha256:{hashlib.sha256(content).hexdigest()}"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return stale_path


def test_sync_removes_unchanged_file_from_old_manifest(tmp_path: Path) -> None:
    sync(repo_root=tmp_path)
    stale_path = _add_stale_manifest_file(tmp_path, b"generated\n")

    result = sync(repo_root=tmp_path)

    assert stale_path in result.stale
    assert not stale_path.exists()
    manifest = json.loads((tmp_path / MANIFEST_PATH).read_text())
    assert stale_path.relative_to(tmp_path).as_posix() not in manifest["files"]


def test_sync_refuses_to_remove_modified_file_from_old_manifest(tmp_path: Path) -> None:
    sync(repo_root=tmp_path)
    stale_path = _add_stale_manifest_file(tmp_path, b"generated\n")
    stale_path.write_text("consumer edit\n")

    with pytest.raises(ValueError):
        sync(repo_root=tmp_path)

    assert stale_path.read_text() == "consumer edit\n"


def test_sync_refuses_to_overwrite_generated_symlink(tmp_path: Path) -> None:
    sync(repo_root=tmp_path)
    target = tmp_path / "consumer-owned.md"
    target.write_text("consumer\n")
    generated = tmp_path / ".agents/marin-style/AGENTS-core.md"
    generated.unlink()
    generated.symlink_to(target)

    with pytest.raises(ValueError):
        sync(repo_root=tmp_path)

    assert target.read_text() == "consumer\n"
