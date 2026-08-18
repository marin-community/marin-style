"""Vendor the packaged Marin-style agent guidance and skills into a consumer repo.

`marin-style sync` copies the packaged assets into a target repository:

- `assets/agents/*.md` -> `<root>/.agents/marin-style/`
- `assets/skills/<name>/*` -> `<root>/.agents/skills/<name>/`

Every vendored file carries a header noting it is generated, so a re-run
overwrites it in place. Skills the repo authored itself are never touched.
"""

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from importlib import metadata, resources
from pathlib import Path, PurePosixPath

PACKAGE = "marin_style"
REVISION_PLACEHOLDER = "@MARIN_STYLE_REV@"
DEFAULT_REVISION = "main"
AGENTS_VENDOR_DIR = ".agents/marin-style"
SKILLS_VENDOR_DIR = ".agents/skills"
CLAUDE_SKILLS_LINK = ".claude/skills"
CORE_AGENTS_REF = ".agents/marin-style/AGENTS-core.md"
MANIFEST_PATH = ".agents/marin-style/manifest.json"
MANIFEST_FORMAT = 1
MANAGED_PREFIXES = (f"{AGENTS_VENDOR_DIR}/", f"{SKILLS_VENDOR_DIR}/")
CONTENT_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def _version() -> str:
    try:
        return metadata.version("marin-style")
    except metadata.PackageNotFoundError:
        return "0.0.0+source"


def _revision() -> str:
    """Return the installed git commit or the unpinned source fallback."""
    try:
        direct_url = metadata.distribution("marin-style").read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return DEFAULT_REVISION
    if direct_url is None:
        return DEFAULT_REVISION
    vcs = json.loads(direct_url).get("vcs_info", {})
    return vcs.get("commit_id", DEFAULT_REVISION)


def _note(version: str) -> str:
    return f"Vendored from marin-community/marin-style v{version} — do not edit; re-run `marin-style sync`."


def _assets_dir() -> Path:
    return Path(str(resources.files(PACKAGE))) / "assets"


@dataclass(frozen=True)
class VendoredFile:
    """A single packaged asset and the repo-relative path it vendors to."""

    source: Path
    relative_dest: Path


@dataclass(frozen=True)
class ManagedManifest:
    """The exact rendered files owned by one marin-style revision."""

    revision: str
    files: tuple[tuple[str, str], ...]


class SyncMode(StrEnum):
    WRITE = "write"
    CHECK = "check"


def _iter_assets() -> list[VendoredFile]:
    assets = _assets_dir()
    files: list[VendoredFile] = []

    agents_src = assets / "agents"
    for path in sorted(agents_src.glob("*.md")):
        files.append(VendoredFile(path, Path(AGENTS_VENDOR_DIR) / path.name))

    skills_src = assets / "skills"
    for skill_dir in sorted(p for p in skills_src.iterdir() if p.is_dir()):
        for path in sorted(p for p in skill_dir.rglob("*") if p.is_file()):
            rel_in_skill = path.relative_to(skills_src)
            files.append(VendoredFile(path, Path(SKILLS_VENDOR_DIR) / rel_in_skill))

    return files


def _render(source: Path, version: str, revision: str) -> str:
    """Return the source content with the vendor note inserted as a header.

    For files that open with YAML frontmatter (skill `SKILL.md`), the note goes
    in the body just below the closing `---` so it never disturbs the metadata
    block. Python files receive a Python comment; other files receive HTML.
    """
    text = source.read_text().replace(REVISION_PLACEHOLDER, revision)
    note = _note(version)
    comment = f"<!-- {note} -->"

    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            split = end + len("\n---\n")
            frontmatter, body = text[:split], text[split:]
            return f"{frontmatter}\n{comment}\n\n{body.lstrip()}"

    if source.suffix == ".py":
        header = f"# {note}"
        if text.startswith("#!"):
            shebang, body = text.split("\n", 1)
            return f"{shebang}\n{header}\n{body}"
        return f"{header}\n\n{text}"

    return f"{comment}\n\n{text}"


def _digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _managed_path(path: str) -> PurePosixPath:
    relative = PurePosixPath(path)
    if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != path or path == MANIFEST_PATH:
        raise ValueError(f"invalid managed path: {path!r}")
    if not path.startswith(MANAGED_PREFIXES):
        raise ValueError(f"managed path is outside marin-style directories: {path!r}")
    return relative


def _rendered_assets(version: str, revision: str) -> dict[str, bytes]:
    return {
        asset.relative_dest.as_posix(): _render(asset.source, version, revision).encode() for asset in _iter_assets()
    }


def _manifest_from_rendered(revision: str, rendered: dict[str, bytes]) -> ManagedManifest:
    return ManagedManifest(
        revision=revision,
        files=tuple((path, _digest(content)) for path, content in sorted(rendered.items())),
    )


def managed_manifest() -> ManagedManifest:
    """Return the installed package's rendered file manifest."""
    revision = _revision()
    rendered = _rendered_assets(_version(), revision)
    return _manifest_from_rendered(revision, rendered)


def managed_manifest_text() -> str:
    """Return the installed package's deterministic JSON manifest."""
    return _manifest_text(managed_manifest())


def _manifest_text(manifest: ManagedManifest) -> str:
    return (
        json.dumps(
            {
                "format": MANIFEST_FORMAT,
                "revision": manifest.revision,
                "files": dict(manifest.files),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _read_manifest(root: Path) -> ManagedManifest | None:
    path = root / MANIFEST_PATH
    if path.is_symlink():
        raise ValueError(f"marin-style manifest must be a regular file: {path}")
    if not path.exists():
        return None
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict) or set(payload) != {"format", "revision", "files"}:
        raise ValueError(f"invalid marin-style manifest: {path}")
    if payload["format"] != MANIFEST_FORMAT or not isinstance(payload["revision"], str):
        raise ValueError(f"unsupported marin-style manifest: {path}")
    files = payload["files"]
    if not isinstance(files, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in files.items()
    ):
        raise ValueError(f"invalid marin-style manifest files: {path}")
    for relative, digest in files.items():
        _managed_path(relative)
        if CONTENT_DIGEST.fullmatch(digest) is None:
            raise ValueError(f"invalid digest for managed path {relative!r}")
    return ManagedManifest(
        revision=payload["revision"],
        files=tuple(sorted(files.items())),
    )


def resolve_repo_root(repo_root: Path | None) -> Path:
    if repo_root is not None:
        return repo_root.resolve()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(result.stdout.strip()).resolve()


@dataclass
class SyncResult:
    written: list[Path]
    drifted: list[Path]
    missing: list[Path]
    stale: list[Path]
    manifest_drifted: bool
    symlink_created: bool
    agents_reference_present: bool


def _sync(repo_root: Path | None, mode: SyncMode) -> SyncResult:
    """Vendor the packaged assets into ``repo_root``.

    Check mode reports drift without modifying the repository.
    """
    root = resolve_repo_root(repo_root)
    version = _version()
    revision = _revision()
    rendered = _rendered_assets(version, revision)
    manifest = _manifest_from_rendered(revision, rendered)
    manifest_text = _manifest_text(manifest)
    old_manifest = _read_manifest(root)

    written: list[Path] = []
    drifted: list[Path] = []
    missing: list[Path] = []
    stale: list[Path] = []

    current_paths = frozenset(rendered)
    current_destinations = {relative: root / _managed_path(relative) for relative in rendered}
    linked_destinations = [path for path in current_destinations.values() if path.is_symlink()]
    if linked_destinations and mode is SyncMode.WRITE:
        raise ValueError(f"refusing to overwrite generated symlink: {linked_destinations[0]}")
    stale_files: list[tuple[Path, str]] = []
    if old_manifest is not None:
        for relative, expected_digest in old_manifest.files:
            if relative in current_paths:
                continue
            stale_path = root / _managed_path(relative)
            if not stale_path.exists():
                continue
            stale.append(stale_path)
            stale_files.append((stale_path, expected_digest))
            if mode is SyncMode.WRITE and (
                stale_path.is_symlink()
                or not stale_path.is_file()
                or _digest(stale_path.read_bytes()) != expected_digest
            ):
                raise ValueError(f"refusing to delete modified stale generated file: {stale_path}")

    for relative, content in rendered.items():
        dest = current_destinations[relative]

        if mode is SyncMode.CHECK:
            if not dest.exists():
                missing.append(dest)
            elif dest.is_symlink() or dest.read_bytes() != content:
                drifted.append(dest)
            continue

        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        written.append(dest)

    if mode is SyncMode.WRITE:
        for stale_path, _ in stale_files:
            stale_path.unlink()

    manifest_path = root / MANIFEST_PATH
    manifest_drifted = not manifest_path.exists() or manifest_path.read_text() != manifest_text
    if mode is SyncMode.WRITE:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(manifest_text)
        written.append(manifest_path)
        manifest_drifted = False

    symlink_created = False if mode is SyncMode.CHECK else _ensure_claude_symlink(root)
    agents_reference_present = _agents_reference_present(root)

    return SyncResult(
        written=written,
        drifted=drifted,
        missing=missing,
        stale=stale,
        manifest_drifted=manifest_drifted,
        symlink_created=symlink_created,
        agents_reference_present=agents_reference_present,
    )


def sync(repo_root: Path | None = None) -> SyncResult:
    """Vendor the packaged assets into a consumer repository."""
    return _sync(repo_root, SyncMode.WRITE)


def check_sync(repo_root: Path | None = None) -> SyncResult:
    """Report vendored asset and manifest drift without writing files."""
    return _sync(repo_root, SyncMode.CHECK)


def _ensure_claude_symlink(root: Path) -> bool:
    link = root / CLAUDE_SKILLS_LINK
    if link.exists() or link.is_symlink():
        return False
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(Path("../.agents/skills"))
    return True


def _agents_reference_present(root: Path) -> bool:
    agents_md = root / "AGENTS.md"
    if not agents_md.exists():
        return False
    return CORE_AGENTS_REF in agents_md.read_text()
