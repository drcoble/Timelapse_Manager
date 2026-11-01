"""Filing a report: validate, store the row, build and save the bundle.

:func:`file_report` is the one entry point the web form and the API share. It
runs synchronously (callers run it off the event loop) in three short steps so
the database writer lock is never held while the snapshot is built:

1. claim a ``TLM-YYYYMMDD-NNNN`` key and insert the row (rate limit checked
   in the same transaction);
2. build the snapshot from a read-only session and write the zip;
3. record the local save, mark the configured destinations pending, and
   write the audit event.

Retention is not applied here: it runs only from the periodic housekeeping
pass, so one user's filing can never evict another user's report.

Delivery to email/webhook/Gitea happens afterwards, off the request path (see
:mod:`.delivery`). A failure in step 2 leaves the row stored and flagged
``failed`` rather than losing what the user wrote.

**Anonymized reports (#21).** While ``reporting.github.enabled`` (the default)
every report is anonymized; otherwise the reporter may ask for it. Then one
:class:`~.anonymize.Pseudonymizer` is built first and the title and description
are scrubbed and anonymized *before* the row is inserted, so the stored text is
the anonymized text. The zip is built, anonymized and verified (fail closed):
on any leak or error the row is deleted and :class:`ReportAnonymizationError`
is raised -- nothing is stored or delivered. With GitHub enabled the result
also carries the prefilled issue URL, itself verified.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from functools import partial

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..db.models import Camera, Project, Report
from ..db.session import session_scope
from ..logging import redact_text
from ..monitoring.events import EventType, log_event
from ..services.errors import AppError, ValidationError
from .anonymize import (
    GENERIC_REFUSAL,
    KIND_LABELS,
    GenericNameError,
    Pseudonymizer,
    ReportAnonymizationError,
    Verifier,
    collect_entities,
    verify_all,
)
from .github import build_issue_url, environment_summary
from .redaction import Scrubber, collect_secrets
from .snapshot import (
    ReportMeta,
    RequestContext,
    SnapshotSources,
    build_bundle,
    build_members,
)
from .store import (
    KEY_ALLOCATION_ATTEMPTS,
    REFUSED_ACTION,
    Attachment,
    check_rate_limit,
    check_reported_at,
    next_report_key,
    normalise_area,
    set_delivery,
    utcnow,
    validate_text,
    write_zip,
)

logger = logging.getLogger(__name__)

# The role that may see the names behind a D16 refusal.
ADMIN_ROLE = "admin"


@dataclass(frozen=True)
class Reporter:
    """Who is filing: the user id (``None`` offline), username and role."""

    user_id: int | None
    username: str | None
    role: str | None


@dataclass(frozen=True)
class Submission:
    """What the reporter submitted, already parsed by the transport."""

    title: str
    description: str
    area: str | None
    reported_at: datetime | None
    project_id: int | None
    camera_id: int | None
    include_snapshot: bool
    attachment: Attachment | None
    source: str
    # Asked for on the form while GitHub is off; forced on while it is on.
    anonymize: bool = False


@dataclass(frozen=True)
class FiledReport:
    """The outcome shown to the reporter."""

    report_id: int
    report_key: str
    saved: bool
    pending_destinations: tuple[str, ...]
    anonymized: bool = False
    # The prefilled public issue (GitHub hand-off enabled), else ``None``.
    github_issue_url: str | None = None


def should_anonymize(settings: Settings, requested: bool) -> bool:
    """Every report is anonymized while the GitHub hand-off is on (D9)."""
    return settings.reporting.github.enabled or requested


def configured_destinations(settings: Settings) -> tuple[str, ...]:
    """The optional destinations that are configured, in delivery order."""
    reporting = settings.reporting
    destinations: list[str] = []
    if reporting.email_to:
        destinations.append("email")
    if reporting.webhook_url:
        destinations.append("webhook")
    if reporting.gitea.configured:
        destinations.append("gitea")
    return tuple(destinations)


def _resolve_scope(
    session: Session, project_id: int | None, camera_id: int | None
) -> tuple[int | None, int | None]:
    """Drop ids that do not exist; fill the camera from the project."""
    if project_id is not None:
        project = session.get(Project, project_id)
        if project is None:
            project_id = None
        elif camera_id is None:
            camera_id = project.camera_id
    if camera_id is not None and session.get(Camera, camera_id) is None:
        camera_id = None
    return project_id, camera_id


def _claim_row(
    session_factory: sessionmaker[Session],
    settings: Settings,
    submission: Submission,
    reporter: Reporter,
    now: datetime,
    *,
    anonymize: bool = False,
) -> tuple[int, ReportMeta]:
    """Insert the report row under a fresh key (step 1).

    With ``anonymize`` the submission's title and description are already the
    anonymized ones; they are stored as given.
    """
    reported_at = (
        check_reported_at(submission.reported_at, now)
        if submission.reported_at is not None
        else now
    )
    for attempt in range(KEY_ALLOCATION_ATTEMPTS):
        try:
            with session_scope(session_factory) as session:
                check_rate_limit(
                    session,
                    reporter.user_id,
                    per_hour=settings.reporting.rate_limit_per_hour,
                    now=now,
                )
                project_id, camera_id = _resolve_scope(
                    session, submission.project_id, submission.camera_id
                )
                key = next_report_key(session, now)
                report = Report(
                    report_key=key,
                    user_id=reporter.user_id,
                    username=reporter.username,
                    area=normalise_area(submission.area),
                    project_id=project_id,
                    camera_id=camera_id,
                    title=submission.title.strip(),
                    description=submission.description,
                    reported_at=reported_at,
                    created_at=now,
                    delivery_state="pending",
                    anonymized=anonymize,
                )
                set_delivery(report, "local", "pending")
                session.add(report)
                session.flush()
                meta = ReportMeta(
                    report_key=key,
                    title=report.title,
                    description=report.description,
                    area=report.area,
                    reported_at=reported_at,
                    created_at=now,
                    username=reporter.username,
                    role=reporter.role,
                    project_id=project_id,
                    camera_id=camera_id,
                    include_snapshot=submission.include_snapshot,
                    source=submission.source,
                    anonymize=anonymize,
                )
                return int(report.id), meta
        except IntegrityError:
            # Another report took this key between our read and insert.
            if attempt == KEY_ALLOCATION_ATTEMPTS - 1:
                raise
    raise RuntimeError("unreachable")  # pragma: no cover


def build_report_zip(
    session: Session,
    settings: Settings,
    meta: ReportMeta,
    context: RequestContext,
    sources: SnapshotSources,
    attachment: Attachment | None,
    *,
    extra_secrets: Iterable[str] = (),
    anonymizer: Pseudonymizer | None = None,
    verifier: Verifier | None = None,
    extra_checks: Iterable[Callable[[], None]] = (),
    os_user: str | None = None,
) -> bytes:
    """Build a redacted bundle. Shared by :func:`file_report` and the CLI.

    With ``meta.anonymize`` the bundle is also anonymized and verified; the
    pseudonymizer and verifier are built here unless the caller passes the
    ones it already used for the title and description.

    :raises ReportAnonymizationError: when an anonymized bundle fails
        verification (or anonymizing raised).
    """
    members = build_members(session, settings, meta, context, sources)
    scrubber = Scrubber(collect_secrets(session, settings, extra=extra_secrets))
    if not meta.anonymize:
        return build_bundle(members, meta, attachment, scrubber)
    if anonymizer is None:
        anonymizer = Pseudonymizer(collect_entities(session, settings, os_user=os_user))
    if verifier is None:
        verifier = anonymizer.verifier(
            scrubber, refuse_weak=settings.reporting.github.enabled
        )
    try:
        return build_bundle(
            members,
            meta,
            None,
            scrubber,
            anonymizer=anonymizer,
            verifier=verifier,
            extra_checks=extra_checks,
        )
    except AppError:
        raise
    except Exception as exc:
        # Fail closed: an anonymizer that raised has not proved anything.
        logger.exception("anonymizing problem report %s failed", meta.report_key)
        raise ReportAnonymizationError(GENERIC_REFUSAL) from exc


def file_report(
    session_factory: sessionmaker[Session],
    settings: Settings,
    submission: Submission,
    reporter: Reporter,
    *,
    context: RequestContext,
    sources: SnapshotSources,
    extra_secrets: Iterable[str] = (),
) -> FiledReport:
    """Validate and store a report, and build its zip. Synchronous.

    :raises ValidationError: for bad input (title, time window).
    :raises ReportTooLargeError: for over-size text.
    :raises ReportRateLimitedError: past the hourly limit.
    :raises ReportAnonymizationError: an anonymized report that could not be
        proved clean (nothing is stored); :class:`~.anonymize.GenericNameError`
        when a short or generic name would reach a GitHub issue (D16).
    """
    validate_text(
        submission.title,
        submission.description,
        max_text_bytes=settings.reporting.max_text_bytes,
    )
    if not settings.reporting.enabled:
        raise ValidationError("Problem reports are disabled.")
    if should_anonymize(settings, submission.anonymize):
        return _file_anonymized(
            session_factory,
            settings,
            submission,
            reporter,
            context=context,
            sources=sources,
            extra_secrets=extra_secrets,
        )
    now = utcnow()
    report_id, meta = _claim_row(session_factory, settings, submission, reporter, now)

    zip_bytes: bytes | None = None
    error: str | None = None
    try:
        with session_scope(session_factory) as session:
            zip_bytes = build_report_zip(
                session,
                settings,
                meta,
                context,
                sources,
                submission.attachment,
                extra_secrets=extra_secrets,
            )
        write_zip(settings, meta.report_key, zip_bytes)
    except Exception as exc:  # noqa: BLE001 - keep the row; flag the failure
        logger.exception("building problem report %s failed", meta.report_key)
        error = type(exc).__name__
        zip_bytes = None
    return _finish(
        session_factory, settings, report_id, meta, reporter, zip_bytes, error
    )


def _finish(
    session_factory: sessionmaker[Session],
    settings: Settings,
    report_id: int,
    meta: ReportMeta,
    reporter: Reporter,
    zip_bytes: bytes | None,
    error: str | None,
    *,
    github_issue_url: str | None = None,
) -> FiledReport:
    """Record the local save, mark destinations pending, audit (step 3)."""

    pending = configured_destinations(settings) if zip_bytes is not None else ()
    with session_scope(session_factory) as session:
        report = session.get(Report, report_id)
        assert report is not None
        if zip_bytes is not None:
            report.zip_path = f"{meta.report_key}.zip"
            report.zip_size_bytes = len(zip_bytes)
            set_delivery(report, "local", "ok")
            for destination in pending:
                set_delivery(report, destination, "pending")
        else:
            set_delivery(report, "local", "failed", error=error)
        log_event(
            session,
            scope="system",
            scope_id=None,
            level="info",
            message=(
                f"user '{reporter.username or 'unknown'}' filed problem report "
                f"{meta.report_key}"
            ),
            type=EventType.AUDIT_CONTROL_ACTION.value,
            metadata={
                "action": "report.filed",
                "report_id": meta.report_key,
                "area": meta.area,
                "project_id": meta.project_id,
                "anonymized": meta.anonymize,
            },
            actor_user_id=reporter.user_id,
        )
    return FiledReport(
        report_id=report_id,
        report_key=meta.report_key,
        saved=zip_bytes is not None,
        pending_destinations=pending,
        anonymized=meta.anonymize,
        github_issue_url=github_issue_url,
    )


def _anonymize_text(pseudonymizer: Pseudonymizer, scrubber: Scrubber, text: str) -> str:
    """Pattern redaction, then the secret scrub, then the pseudonymizer.

    In that order: a pasted ``rtsp://user:pass@host`` loses its password
    before its host is replaced, so neither reaches a public URL.
    """
    return pseudonymizer.text(scrubber.scrub_text(redact_text(text)))


def _anonymize_reporter_text(
    pseudonymizer: Pseudonymizer, scrubber: Scrubber, text: str
) -> str:
    """As :func:`_anonymize_text`, with kind-less ``name-N`` placeholders.

    The title and description are the reporter's own words: a kinded or
    snapshot-numbered placeholder would tell them whether a word they typed
    is a user, a camera or a host on this install (review finding 4).
    """
    return pseudonymizer.user_text(scrubber.scrub_text(redact_text(text)))


def _log_generic_names(report_key: str, error: GenericNameError) -> None:
    """Tell administrators that, and why, a report was refused (D16)."""
    counts: dict[str, set[str]] = {}
    for hit in error.hits:
        counts.setdefault(KIND_LABELS.get(hit.kind, "name"), set()).add(hit.value)
    summary = ", ".join(
        f"{len(values)} {kind}{'' if len(values) == 1 else 's'}"
        for kind, values in sorted(counts.items())
    )
    # Kinds and counts only (review finding 7): the value would land in the
    # next report's log and refuse that one too. An administrator who files
    # the report sees the names in the form error.
    logger.warning(
        "problem report %s refused for GitHub: short or generic %s; an "
        "administrator filing it sees which, or set reporting.github.enabled=false",
        report_key,
        summary,
    )


def _discard(
    session_factory: sessionmaker[Session],
    report_id: int | None,
    report_key: str | None,
    reporter: Reporter,
    reason: str,
) -> None:
    """Delete a refused report's row and record the attempt.

    No zip was written. The ``report.refused`` audit event carries only the
    key and the error code (never a name or a value); it counts toward the
    reporter's hourly limit, because the refused attempt still built a full
    snapshot, and it keeps the key from being issued again.
    """
    with session_scope(session_factory) as session:
        report = session.get(Report, report_id) if report_id is not None else None
        if report is not None:
            session.delete(report)
        log_event(
            session,
            scope="system",
            scope_id=None,
            level="info",
            message=(
                f"problem report {report_key or '(no id yet)'} refused "
                f"({reason}); nothing stored"
            ),
            type=EventType.AUDIT_CONTROL_ACTION.value,
            metadata={
                "action": REFUSED_ACTION,
                "report_id": report_key,
                "reason": reason,
            },
            actor_user_id=reporter.user_id,
        )


def _file_anonymized(
    session_factory: sessionmaker[Session],
    settings: Settings,
    submission: Submission,
    reporter: Reporter,
    *,
    context: RequestContext,
    sources: SnapshotSources,
    extra_secrets: Iterable[str],
) -> FiledReport:
    """File an anonymized report; refuse (and store nothing) on any leak."""
    github = settings.reporting.github
    try:
        with session_scope(session_factory) as session:
            scrubber = Scrubber(collect_secrets(session, settings, extra=extra_secrets))
            pseudonymizer = Pseudonymizer(collect_entities(session, settings))
        verifier = pseudonymizer.verifier(scrubber, refuse_weak=github.enabled)
        title = _anonymize_reporter_text(
            pseudonymizer, scrubber, submission.title.strip()
        )
        description = _anonymize_reporter_text(
            pseudonymizer, scrubber, submission.description
        )
        # The stored row (and email/webhook/Gitea) carry the reporter's
        # placeholder, not their username; admins still have ``user_id``.
        stored_reporter = replace(
            reporter, username=pseudonymizer.reporter_name(reporter.username)
        )
    except Exception as exc:
        # An entity source that could not be read refuses too (finding 2).
        if not isinstance(exc, AppError):
            logger.exception("anonymizing a problem report failed")
        _discard(session_factory, None, None, reporter, "report_not_anonymized")
        raise ReportAnonymizationError(GENERIC_REFUSAL) from exc
    text_checks = [
        partial(verifier.check_text, "title", title),
        partial(verifier.check_text, "description", description),
    ]
    anonymized = replace(
        submission, title=title, description=description, attachment=None
    )
    now = utcnow()
    report_id, meta = _claim_row(
        session_factory, settings, anonymized, stored_reporter, now, anonymize=True
    )
    url: str | None = None
    try:
        with session_scope(session_factory) as session:
            zip_bytes = build_report_zip(
                session,
                settings,
                meta,
                context,
                sources,
                None,
                extra_secrets=extra_secrets,
                anonymizer=pseudonymizer,
                verifier=verifier,
                extra_checks=text_checks,
            )
            if github.enabled:
                environment = {
                    name: _anonymize_text(pseudonymizer, scrubber, value)
                    for name, value in environment_summary(session, sources).items()
                }
                url = build_issue_url(
                    github.repo,
                    github.labels,
                    title=meta.title,
                    report_key=meta.report_key,
                    area=meta.area,
                    description=meta.description,
                    environment=environment,
                )
                verify_all(
                    [partial(verifier.check_url, url)],
                    snapshot_optional=submission.source == "web",
                )
    except Exception as exc:
        reason = exc.code if isinstance(exc, AppError) else "report_not_anonymized"
        _discard(session_factory, report_id, meta.report_key, reporter, reason)
        if isinstance(exc, GenericNameError):
            _log_generic_names(meta.report_key, exc)
            raise exc.for_viewer(reveal=reporter.role == ADMIN_ROLE) from None
        if isinstance(exc, AppError):
            logger.warning(
                "problem report %s refused (%s); nothing stored",
                meta.report_key,
                exc.code,
            )
            raise
        logger.exception("anonymizing problem report %s failed", meta.report_key)
        raise ReportAnonymizationError(GENERIC_REFUSAL) from exc

    # A failed write is not a leak: as for any report, the (anonymized) row
    # is kept and flagged so an administrator sees it.
    saved: bytes | None = zip_bytes
    error: str | None = None
    try:
        write_zip(settings, meta.report_key, zip_bytes)
    except Exception as exc:  # noqa: BLE001 - keep the row; flag the failure
        logger.exception("saving problem report %s failed", meta.report_key)
        error = type(exc).__name__
        saved = None
    return _finish(
        session_factory,
        settings,
        report_id,
        meta,
        reporter,
        saved,
        error,
        github_issue_url=url,
    )
