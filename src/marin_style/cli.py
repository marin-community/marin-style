"""Command-line entry point for `marin-style`."""

from pathlib import Path

import click

from marin_style.echo import echo_group
from marin_style.vendor import CORE_AGENTS_REF, MANIFEST_PATH, SyncResult, check_sync, managed_manifest_text, sync


@click.group()
def main() -> None:
    """Marin coding-standards kit."""


main.add_command(echo_group)


@main.command(name="managed-files")
def managed_files_command() -> None:
    """Print the installed package's generated-file manifest as JSON."""
    click.echo(managed_manifest_text(), nl=False)


@main.command(name="sync")
@click.option(
    "--repo-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Target repository root. Defaults to the git toplevel of the current directory.",
)
@click.option(
    "--check",
    is_flag=True,
    help="Report drift without writing. Exits nonzero if vendored files are missing or stale.",
)
def sync_command(repo_root: Path | None, check: bool) -> None:
    """Vendor the packaged agent guidance and skills into a consumer repo."""
    if check:
        result = check_sync(repo_root=repo_root)
        _report_check(result)
        return

    result = sync(repo_root=repo_root)
    _report_sync(result)


def _report_check(result: SyncResult) -> None:
    if not result.missing and not result.drifted and not result.stale and not result.manifest_drifted:
        click.echo("marin-style: vendored files are up to date.")
        return

    for path in result.missing:
        click.echo(f"missing: {path}", err=True)
    for path in result.drifted:
        click.echo(f"stale:   {path}", err=True)
    for path in result.stale:
        click.echo(f"obsolete: {path}", err=True)
    if result.manifest_drifted:
        click.echo(f"stale:   {MANIFEST_PATH}", err=True)
    click.echo(
        "marin-style: "
        f"{len(result.missing)} missing, {len(result.drifted)} stale, {len(result.stale)} obsolete. "
        "Run `marin-style sync`.",
        err=True,
    )
    raise SystemExit(1)


def _report_sync(result: SyncResult) -> None:
    click.echo(f"marin-style: vendored {len(result.written)} files.")
    if result.symlink_created:
        click.echo("marin-style: created .claude/skills -> ../.agents/skills symlink.")
    if not result.agents_reference_present:
        click.echo(
            f"marin-style: add a reference to {CORE_AGENTS_REF} in your AGENTS.md "
            "so agents pick up the vendored standards, e.g.:\n"
            f"    @{CORE_AGENTS_REF}"
        )
