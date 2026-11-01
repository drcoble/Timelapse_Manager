"""``distinct_project_ids`` reads a long id list in chunks (#96).

Refs #96.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import insert, select
from sqlalchemy.orm import Session, sessionmaker

from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.storage.frames import distinct_project_ids

_SQLITE_VARIABLE_LIMIT = 32_766


def _seed(factory: sessionmaker[Session], counts: list[int]) -> list[list[int]]:
    """One project per entry of ``counts``, each with that many frames; return ids."""
    with session_scope(factory) as session:
        camera = Camera(name="cam")
        session.add(camera)
        session.flush()
        projects = [
            Project(camera_id=camera.id, name=f"p{n}") for n in range(len(counts))
        ]
        session.add_all(projects)
        session.flush()
        per_project: list[list[int]] = []
        for project, count in zip(projects, counts, strict=True):
            session.execute(
                insert(Frame),
                [
                    {
                        "project_id": project.id,
                        "sequence_index": seq,
                        "capture_timestamp": datetime(2026, 1, 1),
                        "file_path": f"{seq}.jpg",
                        "width": 1,
                        "height": 1,
                        "file_size_bytes": 1,
                        "capture_status": "captured",
                        "origin": "captured",
                        "lifecycle_state": "active",
                    }
                    for seq in range(1, count + 1)
                ],
            )
            per_project.append(
                list(
                    session.scalars(
                        select(Frame.id).where(Frame.project_id == project.id)
                    )
                )
            )
        return per_project


class TestDistinctProjectIds:
    def test_a_list_past_the_variable_limit_does_not_fail(
        self, migrated_factory: sessionmaker[Session]
    ) -> None:
        [ids] = _seed(migrated_factory, [_SQLITE_VARIABLE_LIMIT + 5_000])

        with session_scope(migrated_factory) as session:
            found = distinct_project_ids(session, ids)

        assert len(found) == 1

    def test_stops_at_the_limit_of_distinct_projects(
        self, migrated_factory: sessionmaker[Session]
    ) -> None:
        first, second = _seed(migrated_factory, [10, 10])

        with session_scope(migrated_factory) as session:
            found = distinct_project_ids(session, first + second)

        assert len(found) == 2

    def test_unknown_ids_contribute_nothing(
        self, migrated_factory: sessionmaker[Session]
    ) -> None:
        with session_scope(migrated_factory) as session:
            assert distinct_project_ids(session, [999_001, 999_002]) == []
            assert distinct_project_ids(session, []) == []
