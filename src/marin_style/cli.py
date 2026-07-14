"""Command-line entry point for `marin-style`."""

from pathlib import Path

import click

from marin_style.vendor import CORE_AGENTS_REF, SyncResult, sync


@click.group()
def main() -> None:
    """Marin coding-standards kit."""


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
    result = sync(repo_root=repo_root, check=check)

    if check:
        _report_check(result)
        return

    _report_sync(result)


def _report_check(result: SyncResult) -> None:
    if not result.missing and not result.drifted:
        click.echo("marin-style: vendored files are up to date.")
        return

    for path in result.missing:
        click.echo(f"missing: {path}", err=True)
    for path in result.drifted:
        click.echo(f"stale:   {path}", err=True)
    click.echo(
        f"marin-style: {len(result.missing)} missing, {len(result.drifted)} stale. Run `marin-style sync`.",
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
