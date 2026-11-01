"""The public GitHub hand-off for a problem report (#21).

The app never holds a GitHub credential and never calls GitHub. It builds a
prefilled ``https://github.com/<repo>/issues/new?title=…&body=…`` link from the
report's **already anonymized** text; the user opens it, reviews it, and
submits it from their own GitHub account.

Limits:

* The encoded URL is kept at or under :data:`MAX_URL_LENGTH` characters: the
  description is trimmed and :data:`TRUNCATION_NOTE` added rather than
  failing. GitHub and browsers reject much longer URLs.
* No ``labels`` parameter is sent unless labels are configured: GitHub answers
  404 to a prefilled label from a user who cannot label issues in the
  repository, which is every end user of a public project.
* GitHub cannot take an attachment from a URL; the result screen offers the
  anonymized zip to download and drag in by hand.
"""

from __future__ import annotations

import platform
import sys
import urllib.parse
from collections.abc import Iterable

from sqlalchemy.orm import Session

from ..version import get_app_version, get_build_info
from .snapshot import SnapshotSources, _alembic_revision, _guard
from .store import AREAS

MAX_URL_LENGTH = 7000
TRUNCATION_NOTE = "Description truncated; paste the rest."

# One environment value is never longer than this (a kernel build string can
# be long, and the description should get the URL budget).
_MAX_ENV_VALUE = 120

_FOOTER = (
    "---\n"
    "Prepared by Timelapse Manager's *Report a problem* form. Host names, "
    "addresses, user, project and camera names and paths were replaced with "
    "placeholders such as `host-1` and `camera-1`. **This issue is public: "
    "review the text before you submit it.** The anonymized diagnostic zip "
    "from the report screen can be dragged into this box."
)


def environment_summary(session: Session, sources: SnapshotSources) -> dict[str, str]:
    """App version and build, OS, Python, ffmpeg and database revision.

    The values still go through the report's pseudonymizer before use.
    """
    build = get_build_info()
    revision = _guard("alembic", lambda: _alembic_revision(session))
    summary = {
        "Version": f"{get_app_version()} (build {build['sha']}, {build['date']})",
        "OS": " ".join(
            part
            for part in (platform.system(), platform.release(), platform.machine())
            if part
        ),
        "Python": sys.version.split()[0],
        "ffmpeg": sources.ffmpeg_version,
        "Database revision": revision if isinstance(revision, str) else "unknown",
    }
    return {key: str(value)[:_MAX_ENV_VALUE] for key, value in summary.items()}


def issue_body(
    *,
    report_key: str,
    area: str,
    description: str,
    environment: dict[str, str],
    truncated: bool = False,
) -> str:
    """The Markdown body: description, area, environment, report id, notice."""
    lines = [description.strip() or "_No description given._"]
    if truncated:
        lines += ["", f"> {TRUNCATION_NOTE}"]
    lines += ["", "**Details**", "", f"- Area: {AREAS.get(area, area)}"]
    lines += [f"- {name}: {value}" for name, value in environment.items()]
    lines += [f"- Report id: {report_key}", "", _FOOTER]
    return "\n".join(lines)


def issue_url(repo: str, labels: Iterable[str], title: str, body: str) -> str:
    """``https://github.com/<repo>/issues/new`` with the encoded query."""
    query: dict[str, str] = {"title": title, "body": body}
    label_list = [label for label in labels if label]
    if label_list:
        query["labels"] = ",".join(label_list)
    encoded = urllib.parse.urlencode(query, quote_via=urllib.parse.quote)
    return f"https://github.com/{repo}/issues/new?{encoded}"


def build_issue_url(
    repo: str,
    labels: Iterable[str],
    *,
    title: str,
    report_key: str,
    area: str,
    description: str,
    environment: dict[str, str],
    max_length: int = MAX_URL_LENGTH,
) -> str:
    """The hand-off URL, trimming the description to fit ``max_length``.

    The encoded length grows with the description, so the longest prefix that
    fits is found by bisection over its characters (a multi-byte character
    can encode to a dozen URL characters, so a byte budget would not do).

    :raises ValueError: when even an empty description does not fit, which the
        caller treats as a refusal.
    """
    label_list = list(labels)

    def url_for(text: str, truncated: bool) -> str:
        body = issue_body(
            report_key=report_key,
            area=area,
            description=text,
            environment=environment,
            truncated=truncated,
        )
        return issue_url(repo, label_list, title, body)

    full = url_for(description, False)
    if len(full) <= max_length:
        return full

    def fits(length: int) -> bool:
        return len(url_for(description[:length], True)) <= max_length

    if not fits(0):
        raise ValueError("the GitHub issue URL does not fit even without a description")
    low, high = 0, len(description)
    while low < high:
        middle = (low + high + 1) // 2
        if fits(middle):
            low = middle
        else:
            high = middle - 1
    return url_for(description[:low], True)
