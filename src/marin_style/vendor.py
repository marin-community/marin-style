"""Vendor the packaged Marin-style agent guidance and skills into a consumer repo.

`marin-style sync` copies the packaged assets into a target repository:

- `assets/agents/*.md` -> `<root>/.agents/marin-style/`
- `assets/skills/<name>/*` -> `<root>/.agents/skills/<name>/`

Every vendored file carries a header noting it is generated, so a re-run
overwrites it in place. Skills the repo authored itself are never touched.
"""

import json
import subprocess
from dataclasses import dataclass
from importlib import metadata, resources
from pathlib import Path

PACKAGE = "marin_style"
REVISION_PLACEHOLDER = "@MARIN_STYLE_REV@"
DEFAULT_REVISION = "main"
AGENTS_VENDOR_DIR = ".agents/marin-style"
SKILLS_VENDOR_DIR = ".agents/skills"
CLAUDE_SKILLS_LINK = ".claude/skills"
CORE_AGENTS_REF = ".agents/marin-style/AGENTS-core.md"


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
    symlink_created: bool
    agents_reference_present: bool


def sync(repo_root: Path | None = None, check: bool = False) -> SyncResult:
    """Vendor the packaged assets into ``repo_root``.

    With ``check=True`` nothing is written; the result reports vendored files
    that are missing or differ from the packaged assets (for CI drift gating).
    """
    root = resolve_repo_root(repo_root)
    version = _version()
    revision = _revision()

    written: list[Path] = []
    drifted: list[Path] = []
    missing: list[Path] = []

    for asset in _iter_assets():
        dest = root / asset.relative_dest
        content = _render(asset.source, version, revision)

        if check:
            if not dest.exists():
                missing.append(dest)
            elif dest.read_text() != content:
                drifted.append(dest)
            continue

        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
        written.append(dest)

    symlink_created = False if check else _ensure_claude_symlink(root)
    agents_reference_present = _agents_reference_present(root)

    return SyncResult(
        written=written,
        drifted=drifted,
        missing=missing,
        symlink_created=symlink_created,
        agents_reference_present=agents_reference_present,
    )


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
