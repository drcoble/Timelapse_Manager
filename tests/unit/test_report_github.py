"""Unit tests for the GitHub hand-off URL and its settings (#21)."""

from __future__ import annotations

import urllib.parse

import pytest
from pydantic import ValidationError

from timelapse_manager.config.settings import ReportingGithubSettings, ReportingSettings
from timelapse_manager.reports.github import (
    MAX_URL_LENGTH,
    TRUNCATION_NOTE,
    build_issue_url,
    issue_url,
)

_ENV = {"Version": "0.2.0 (build abc, 2026-10-23)", "OS": "Linux 6.1 aarch64"}


def _query(url: str) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)


def _build(description: str, *, title: str = "Capture stopped", **kw: object) -> str:
    return build_issue_url(
        "drcoble/Timelapse_Manager",
        kw.pop("labels", []),  # type: ignore[arg-type]
        title=title,
        report_key="TLM-20261023-0001",
        area="capture",
        description=description,
        environment=_ENV,
    )


def test_defaults() -> None:
    github = ReportingSettings().github
    assert github.enabled is True
    assert github.repo == "drcoble/Timelapse_Manager"
    assert github.labels == []


def test_no_labels_parameter_by_default() -> None:
    url = _build("It stopped.")
    assert url.startswith("https://github.com/drcoble/Timelapse_Manager/issues/new?")
    assert "labels=" not in url
    query = _query(url)
    assert query["title"] == ["Capture stopped"]
    body = query["body"][0]
    assert "It stopped." in body
    assert "Area: Capture" in body
    assert "Report id: TLM-20261023-0001" in body
    assert "0.2.0 (build abc" in body
    assert "This issue is public" in body


def test_configured_labels_are_sent() -> None:
    url = issue_url("o/r", ["bug", "from app"], "t", "b")
    assert _query(url)["labels"] == ["bug,from app"]


def test_spaces_are_percent_encoded() -> None:
    assert "title=a%20b" in issue_url("o/r", [], "a b", "")


@pytest.mark.parametrize("unit", ["x", "é", "😀", "漢"])
def test_long_text_is_trimmed_under_the_limit(unit: str) -> None:
    title = unit * 200
    description = unit * 20_000
    url = _build(description, title=title)
    assert len(url) <= MAX_URL_LENGTH
    body = _query(url)["body"][0]
    assert TRUNCATION_NOTE in body
    assert _query(url)["title"] == [title]
    # It keeps as much of the description as fits, not just a stub.
    assert body.count(unit) > 100


def test_short_text_is_not_trimmed() -> None:
    url = _build("short")
    assert TRUNCATION_NOTE not in urllib.parse.unquote(url)


@pytest.mark.parametrize(
    "repo",
    ["drcoble", "a/b/c", "https://github.com/a/b", "a/..", "own er/r", "a/b?x=1"],
)
def test_repo_must_be_owner_name(repo: str) -> None:
    with pytest.raises(ValidationError):
        ReportingGithubSettings(repo=repo)


def test_labels_cannot_hold_commas() -> None:
    with pytest.raises(ValidationError):
        ReportingGithubSettings(labels=["a,b"])
    assert ReportingGithubSettings(labels="bug, triage").labels == ["bug", "triage"]
