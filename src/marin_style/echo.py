"""Portable client for Marin's shared Echo service."""

import json
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import click
import google.auth
import google.auth.exceptions
import google.auth.external_account
import google.auth.impersonated_credentials
import google.auth.jwt
import google.auth.transport.requests
import google.oauth2.credentials
import google.oauth2.id_token
import requests
import yaml

API_URL = os.environ.get("ECHO_API_URL", "https://echo.oa.dev").rstrip("/")
API_BASE = f"{API_URL}/api"
AUDIENCE = "748532799086-qf8m6mvovtdmd71npm07gk1ohijsr3q5.apps.googleusercontent.com"
OAUTH_CLIENT_SECRET = "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"
LOGIN_CLUSTER = os.environ.get("ECHO_LOGIN_CLUSTER", "marin")
LOGIN_SCOPES = ["openid", "email"]
IMPERSONATION_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
REQUEST_TIMEOUT = 30
SEARCH_DOMAINS = ("wiki", "file", "pr", "issue", "discord")
DEFAULT_SEARCH_DOMAINS = ("wiki", "file", "pr", "issue")
MISSING_EMAIL_SCOPE_WARNING = "Not all requested scopes were granted by the authorization server, missing scopes email."


def keep_oauth_log(record: logging.LogRecord) -> bool:
    return record.getMessage() != MISSING_EMAIL_SCOPE_WARNING


logging.getLogger("google.oauth2.credentials").addFilter(keep_oauth_log)


class EchoError(RuntimeError):
    """An actionable Echo authentication or API failure."""


def credentials_directory() -> Path:
    return Path.home() / ".config" / "marin" / "credentials"


def credential_paths(directory: Path) -> list[Path]:
    preferred = directory / f"{LOGIN_CLUSTER}.json"
    others = sorted(path for path in directory.glob("*.json") if path != preferred)
    return ([preferred] if preferred.is_file() else []) + others


def refresh_human_token(refresh_token: str) -> str:
    credentials = google.oauth2.credentials.Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=AUDIENCE,
        client_secret=OAUTH_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=LOGIN_SCOPES,
    )
    try:
        credentials.refresh(google.auth.transport.requests.Request())
    except google.auth.exceptions.RefreshError as error:
        raise EchoError("cached Marin credentials expired; run `iris login` again") from error
    except google.auth.exceptions.TransportError as error:
        raise EchoError(f"could not refresh cached Marin credentials: {error}") from error
    if credentials.id_token is None:
        raise EchoError("cached Marin credentials did not yield an IAP identity token")
    return credentials.id_token


def ambient_token() -> str:
    request = google.auth.transport.requests.Request()
    try:
        return cast(str, google.oauth2.id_token.fetch_id_token(request, AUDIENCE))
    except google.auth.exceptions.DefaultCredentialsError:
        pass
    except google.auth.exceptions.TransportError as error:
        raise EchoError(f"could not mint an ambient Echo identity token: {error}") from error

    try:
        source, _ = google.auth.default(scopes=IMPERSONATION_SCOPES)
    except google.auth.exceptions.DefaultCredentialsError as error:
        raise EchoError("no Marin login or ambient service-account credentials; run `iris login`") from error

    if isinstance(source, google.auth.impersonated_credentials.Credentials):
        signing_credentials = source
    elif isinstance(source, google.auth.external_account.Credentials) and source.service_account_email:
        signing_credentials = source._initialize_impersonated_credentials()
    else:
        raise EchoError("ambient credentials do not represent an impersonated service account; run `iris login`")

    credentials = google.auth.impersonated_credentials.IDTokenCredentials(
        signing_credentials,
        target_audience=AUDIENCE,
        include_email=True,
    )
    try:
        credentials.refresh(request)
    except google.auth.exceptions.GoogleAuthError as error:
        raise EchoError(f"could not mint an impersonated Echo identity token: {error}") from error
    if credentials.token is None:
        raise EchoError("ambient credentials did not yield an IAP identity token")
    return credentials.token


def bearer_token(directory: Path | None = None) -> str:
    for path in credential_paths(directory or credentials_directory()):
        data = json.loads(path.read_text())
        refresh_token = data.get("edge_refresh_token")
        if refresh_token:
            return refresh_human_token(refresh_token)
    return ambient_token()


def request_json(
    method: str,
    path: str,
    *,
    params: Sequence[tuple[str, str | int]] = (),
    body: object = None,
) -> object:
    response = requests.request(
        method,
        f"{API_BASE}{path}",
        params=params,
        json=body,
        headers={"Authorization": f"Bearer {bearer_token()}"},
        timeout=REQUEST_TIMEOUT,
        allow_redirects=False,
    )
    if response.status_code == 401:
        raise EchoError("Echo rejected the IAP token; confirm access and run `iris login` again")
    if response.status_code >= 400:
        try:
            payload = response.json()
            detail = payload.get("detail", response.text) if isinstance(payload, dict) else response.text
        except requests.exceptions.JSONDecodeError:
            detail = response.text
        raise EchoError(f"{method} {path} returned {response.status_code}: {detail}")
    return response.json()


def api_request(
    method: str,
    path: str,
    *,
    params: Sequence[tuple[str, str | int]] = (),
    body: object = None,
) -> object:
    try:
        return request_json(method, path, params=params, body=body)
    except (EchoError, requests.RequestException) as error:
        raise click.ClickException(str(error)) from error


def response_object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise click.ClickException("Echo returned a non-object response")
    return value


def response_objects(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise click.ClickException("Echo returned a non-list response")
    return value


def file_text(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text()


def wiki_document(path: str) -> dict[str, object]:
    text = file_text(path)
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise click.ClickException(f"{path}: expected YAML frontmatter followed by Markdown")
    frontmatter, body = text[4:].split("\n---\n", 1)
    metadata = yaml.safe_load(frontmatter)
    if not isinstance(metadata, dict):
        raise click.ClickException(f"{path}: frontmatter must be a mapping")
    missing = [field for field in ("title", "use_when") if not metadata.get(field)]
    if not body.strip():
        missing.append("body")
    if missing:
        raise click.ClickException(f"{path}: missing {', '.join(missing)}")
    tags = metadata.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise click.ClickException(f"{path}: tags must be a list of strings")
    return {
        "title": str(metadata["title"]),
        "use_when": str(metadata["use_when"]),
        "tags": tags,
        "body": body.strip(),
    }


def print_wiki_document(entry: dict[str, Any]) -> None:
    metadata = {
        "type": "wiki-note",
        "title": entry["title"],
        "use_when": entry["use_when"],
        "tags": entry.get("tags", []),
        "resource": f"{API_URL}/wiki/{entry['id']}",
    }
    click.echo("---")
    click.echo(yaml.safe_dump(metadata, sort_keys=False).rstrip())
    click.echo("---\n")
    click.echo(entry["body"])


@click.group(name="echo")
def echo_group() -> None:
    """Search and write Marin's shared Echo knowledge store."""


@echo_group.command(name="search")
@click.argument("query")
@click.option("--domain", type=click.Choice(SEARCH_DOMAINS), multiple=True)
@click.option("--limit", type=click.IntRange(1, 20), default=10, show_default=True)
def search_command(query: str, domain: tuple[str, ...], limit: int) -> None:
    """Search Echo wiki, repository files, and project activity."""
    domains = domain or DEFAULT_SEARCH_DOMAINS
    params: list[tuple[str, str | int]] = [("q", query), ("limit", limit)]
    params.extend(("domain", value) for value in domains)
    results = response_objects(api_request("GET", "/federated-search", params=params))
    if not results:
        click.echo("No results.")
        return
    for result in results:
        detail = result.get("subtitle") or result.get("snippet") or ""
        click.echo(f"{result['id']}\t{result.get('title', '')}\t{detail}")
        click.echo(f"  {result.get('url', '')}")


@echo_group.command(name="get")
@click.argument("result_id")
def get_command(result_id: str) -> None:
    """Fetch one complete search result by its printed domain:id."""
    domain, separator, value = result_id.partition(":")
    if not separator:
        raise click.UsageError("result id must be domain:id")
    if domain == "wiki":
        entry = response_object(api_request("GET", f"/wiki/{value}"))
        print_wiki_document(entry)
        return
    if domain == "file":
        entry = response_object(api_request("GET", f"/repository-files/{quote(value, safe='/')}"))
    elif domain in {"pr", "issue", "discord"}:
        entry = response_object(api_request("GET", f"/chunks/{value}"))
    else:
        raise click.UsageError(f"unknown result domain: {domain}")
    click.echo(f"[{result_id}] {entry.get('title') or entry.get('url')}")
    click.echo(entry.get("url", ""))
    click.echo()
    click.echo(entry.get("text") or entry.get("snippet") or "")


@echo_group.group(name="work-log")
def work_log_group() -> None:
    """Append distilled milestones to Echo's cross-project work log."""


@work_log_group.command(name="add")
@click.option("--project", required=True, help="Stable repo:issue-or-task slug.")
@click.option("--title", required=True, help="One-line milestone summary.")
@click.option("--body", help="Short Markdown with evidence links.")
@click.option("--body-file", help="Read the Markdown body from a file, or - for stdin.")
def add_work_log_command(project: str, title: str, body: str | None, body_file: str | None) -> None:
    """Append one milestone."""
    if body is not None and body_file is not None:
        raise click.UsageError("use only one of --body and --body-file")
    entry = response_object(
        api_request(
            "POST",
            "/work_log",
            body={"project": project, "title": title, "body": file_text(body_file) if body_file else body},
        )
    )
    click.echo(f"created work-log #{entry['id']}: {entry['title']}")
    click.echo(f"{API_URL}/conversation")


@echo_group.group(name="wiki")
def wiki_group() -> None:
    """Search and publish durable Echo wiki entries."""


@wiki_group.command(name="search")
@click.argument("query", required=False, default="")
@click.option("--tag", multiple=True, help="Require a lowercase kebab-case tag.")
@click.option("--limit", type=click.IntRange(1, 20), default=10, show_default=True)
def wiki_search_command(query: str, tag: tuple[str, ...], limit: int) -> None:
    """Search wiki entries; a blank query returns recent entries."""
    params: list[tuple[str, str | int]] = [("q", query), ("limit", limit)]
    params.extend(("tag", value) for value in tag)
    entries = response_objects(api_request("GET", "/wiki/search", params=params))
    for entry in entries:
        click.echo(f"wiki:{entry['id']}\t{entry['title']}\t{entry['use_when']}")
        click.echo(f"  {API_URL}/wiki/{entry['id']}")


@wiki_group.command(name="show")
@click.argument("entry_id", type=int)
def wiki_show_command(entry_id: int) -> None:
    """Export one wiki entry as an Open Knowledge Format document."""
    print_wiki_document(response_object(api_request("GET", f"/wiki/{entry_id}")))


@wiki_group.command(name="add")
@click.option("--file", "document", required=True, help="OKF Markdown file, or - for stdin.")
def wiki_add_command(document: str) -> None:
    """Create a wiki entry from an Open Knowledge Format document."""
    entry = response_object(api_request("POST", "/wiki", body=wiki_document(document)))
    click.echo(f"created wiki #{entry['id']}: {entry['title']}")
    click.echo(f"{API_URL}/wiki/{entry['id']}")


@wiki_group.command(name="edit")
@click.argument("entry_id", type=int)
@click.option("--file", "document", required=True, help="OKF Markdown file, or - for stdin.")
def wiki_edit_command(entry_id: int, document: str) -> None:
    """Replace a wiki entry from an Open Knowledge Format document."""
    entry = response_object(api_request("PUT", f"/wiki/{entry_id}", body=wiki_document(document)))
    click.echo(f"updated wiki #{entry['id']}: {entry['title']}")
    click.echo(f"{API_URL}/wiki/{entry['id']}")
