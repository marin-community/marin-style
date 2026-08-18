import json
from pathlib import Path
from unittest.mock import Mock, patch

from click.testing import CliRunner

from marin_style.cli import main
from marin_style.echo import EchoConfig, bearer_token, request_json, wiki_document

TEST_CONFIG = EchoConfig(api_url="https://echo.oa.dev", login_cluster="marin")


def test_bearer_token_prefers_cached_marin_login(tmp_path: Path) -> None:
    (tmp_path / "other.json").write_text(json.dumps({"edge_refresh_token": "other"}))
    (tmp_path / "marin.json").write_text(json.dumps({"edge_refresh_token": "preferred"}))

    with patch("marin_style.echo.refresh_human_token", return_value="token") as refresh:
        assert bearer_token(TEST_CONFIG, tmp_path) == "token"

    refresh.assert_called_once_with("preferred")


@patch("marin_style.echo.ambient_token", return_value="ambient")
def test_bearer_token_uses_ambient_credentials_without_login(ambient: Mock, tmp_path: Path) -> None:
    assert bearer_token(TEST_CONFIG, tmp_path) == "ambient"
    ambient.assert_called_once_with()


@patch("marin_style.echo.bearer_token", return_value="iap-token")
@patch("marin_style.echo.requests.request")
def test_request_sends_bearer_and_repeated_query_parameters(request: Mock, _token: Mock) -> None:
    response = Mock(status_code=200)
    response.json.return_value = [{"id": "wiki:1"}]
    request.return_value = response
    params = [("q", "scheduler"), ("domain", "wiki"), ("domain", "pr")]

    assert request_json(TEST_CONFIG, "GET", "/federated-search", params=params) == [{"id": "wiki:1"}]

    request.assert_called_once_with(
        "GET",
        "https://echo.oa.dev/api/federated-search",
        params=params,
        json=None,
        headers={"Authorization": "Bearer iap-token"},
        timeout=30,
        allow_redirects=False,
    )


def test_wiki_document_parses_open_knowledge_format(tmp_path: Path) -> None:
    path = tmp_path / "design.md"
    path.write_text(
        """---
type: wiki-note
title: Share debugging records through Echo
use_when: when an investigation spans Marin repositories
tags:
  - design
  - echo
---

Use Echo's work log for milestones.
"""
    )

    assert wiki_document(str(path)) == {
        "title": "Share debugging records through Echo",
        "use_when": "when an investigation spans Marin repositories",
        "tags": ["design", "echo"],
        "body": "Use Echo's work log for milestones.",
    }


@patch("marin_style.echo.api_request")
def test_work_log_command_posts_distilled_milestone(api_request: Mock) -> None:
    api_request.return_value = {"id": 9, "title": "Cause isolated"}

    result = CliRunner().invoke(
        main,
        [
            "echo",
            "work-log",
            "add",
            "--project",
            "vllm:issue-123",
            "--title",
            "Cause isolated",
            "--body",
            "The trace identifies preemption.",
        ],
    )

    assert result.exit_code == 0, result.output
    api_request.assert_called_once_with(
        TEST_CONFIG,
        "POST",
        "/work_log",
        body={
            "project": "vllm:issue-123",
            "title": "Cause isolated",
            "body": "The trace identifies preemption.",
        },
    )
    assert "https://echo.oa.dev/conversation" in result.output
