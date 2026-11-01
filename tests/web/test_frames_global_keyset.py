"""Web tests for the cross-project ("All Projects") global frame cursor.

The single-project browser pages on per-project ``sequence_index``; the global
grid pages on the frame primary key ``id`` (globally monotonic, never null). The
presence of ``project_id`` is the SOLE mode discriminator on /frames and
/frames/batch, so these tests assert the two modes page on different keys and
the global cursor spans projects newest-first.
"""

from __future__ import annotations

import datetime

from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context


def _seed_two_projects() -> dict[str, list[int]]:
    """Seed two projects with three frames each; return frame ids per project.

    Project A's frames are inserted first, so they receive the LOWER ids; project
    B's frames receive the higher ids. Global id-desc ordering therefore yields
    B's frames before A's.
    """
    ctx = get_context()
    out: dict[str, list[int]] = {"A": [], "B": [], "pa": [], "pb": []}
    with session_scope(ctx.session_factory) as db:
        for key, pname in (("A", "alpha"), ("B", "bravo")):
            cam = Camera(name=f"cam-{pname}", address="127.0.0.1", protocol="http")
            db.add(cam)
            db.flush()
            proj = Project(
                camera_id=cam.id,
                name=f"proj-{pname}",
                capture_interval_seconds=60,
                lifecycle_state="active",
                operational_status="idle",
                storage_path=f"/tmp/{pname}",
            )
            db.add(proj)
            db.flush()
            out["pa" if key == "A" else "pb"].append(proj.id)
            for i in range(3):
                f = Frame(
                    project_id=proj.id,
                    sequence_index=i,
                    capture_timestamp=datetime.datetime(2026, 1, 1)
                    + datetime.timedelta(minutes=i),
                    lifecycle_state="active",
                    capture_status="captured",
                )
                db.add(f)
                db.flush()
                out[key].append(f.id)
    return out


def test_all_projects_grid_spans_projects(admin_client: TestClient) -> None:
    """Bare /frames shows frames from BOTH projects in one grid."""
    ids = _seed_two_projects()
    html = admin_client.get("/frames").text
    # Every seeded frame from both projects appears as a tile.
    for fid in ids["A"] + ids["B"]:
        assert f'id="frame-tile-{fid}"' in html
    # Both project names are labelled on the tiles.
    assert "proj-alpha" in html and "proj-bravo" in html


def test_all_projects_newest_first_by_id(admin_client: TestClient) -> None:
    """Global grid orders newest-first by frame id (B's frames before A's)."""
    ids = _seed_two_projects()
    html = admin_client.get("/frames").text
    # The highest id (B's last frame) must appear before the lowest (A's first).
    highest = max(ids["B"])
    lowest = min(ids["A"])
    assert html.index(f"frame-tile-{highest}") < html.index(f"frame-tile-{lowest}")


def test_global_batch_before_is_strictly_older_by_id(admin_client: TestClient) -> None:
    """/frames/batch with no project_id pages on id: before=<id> → id < cursor."""
    ids = _seed_two_projects()
    # Cursor = B's lowest id; older frames are exactly project A's three frames.
    cursor = min(ids["B"])
    html = admin_client.get(f"/frames/batch?before={cursor}").text
    for fid in ids["A"]:
        assert f'id="frame-tile-{fid}"' in html  # strictly older → present
    for fid in ids["B"]:
        assert f'id="frame-tile-{fid}"' not in html  # >= cursor → absent
    # The global sentinel (if any) carries before=<id> and NO project_id.
    assert "project_id=" not in html


def test_single_project_batch_still_pages_on_sequence(
    admin_client: TestClient,
) -> None:
    """With project_id present, the batch pages on sequence_index, not id."""
    ids = _seed_two_projects()
    pa = ids["pa"][0]
    # before=<seq> on project A: seq 0..2; before=2 → seq 0 and 1 remain.
    html = admin_client.get(f"/frames/batch?project_id={pa}&before=2").text
    # A's frame with sequence_index 2 is the cursor → excluded; 0 and 1 present.
    # (This is sequence_index paging — id paging would exclude different rows,
    # since A's ids and seq values diverge once B's frames hold the high ids.)
    assert f'id="frame-tile-{ids["A"][0]}"' in html  # seq 0
    assert f'id="frame-tile-{ids["A"][1]}"' in html  # seq 1
    assert f'id="frame-tile-{ids["A"][2]}"' not in html  # seq 2 == cursor
    # B's frames never appear in a project-A-scoped batch.
    for fid in ids["B"]:
        assert f'id="frame-tile-{fid}"' not in html


def test_soft_delete_swap_keeps_project_label_in_global_grid(
    admin_client: TestClient,
) -> None:
    """A tile mutated in the All-Projects grid keeps its project label.

    The soft-delete HTMX swap re-renders one tile; in global mode it must carry
    all_projects=1 so the swapped-in tile still shows its owning project.
    """
    ids = _seed_two_projects()
    pa = ids["pa"][0]
    fid = ids["A"][0]
    csrf = csrf_of(admin_client, "/frames")
    resp = admin_client.post(
        f"/projects/{pa}/frames/{fid}/soft-delete?all_projects=1",
        data={"csrf_token": csrf},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 200
    # The swapped tile re-renders with its project label intact.
    assert "proj-alpha" in resp.text
    assert 'class="frame-tile-project"' in resp.text


def test_soft_delete_swap_has_no_label_in_single_project_grid(
    admin_client: TestClient,
) -> None:
    """In single-project mode the swapped tile shows no project label."""
    ids = _seed_two_projects()
    pa = ids["pa"][0]
    fid = ids["A"][0]
    csrf = csrf_of(admin_client, "/frames")
    resp = admin_client.post(
        f"/projects/{pa}/frames/{fid}/soft-delete",
        data={"csrf_token": csrf},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 200
    assert 'class="frame-tile-project"' not in resp.text


# --- #226: the picker's "All Projects" option requests ``project_id=`` ----------


def test_empty_project_id_is_the_all_projects_grid(admin_client: TestClient) -> None:
    """``/frames?project_id=`` (the "All Projects" option) is not a 422."""
    ids = _seed_two_projects()
    bare = admin_client.get("/frames")
    empty = admin_client.get("/frames?project_id=")
    assert empty.status_code == 200
    for fid in ids["A"] + ids["B"]:
        assert f'id="frame-tile-{fid}"' in empty.text
    assert "proj-alpha" in empty.text and "proj-bravo" in empty.text
    assert empty.text.count("frame-tile-") == bare.text.count("frame-tile-")


def test_blank_project_id_works_on_batch_and_since(admin_client: TestClient) -> None:
    ids = _seed_two_projects()
    newest = max(ids["A"] + ids["B"])
    batch = admin_client.get(f"/frames/batch?project_id=&before={newest}")
    assert batch.status_code == 200
    assert f"frame-tile-{newest}" not in batch.text  # strictly older
    since = admin_client.get("/frames/since?after=0&project_id=")
    assert since.status_code == 200
    assert since.json() == {"count": len(ids["A"] + ids["B"])}


def test_a_non_numeric_project_id_is_still_rejected(admin_client: TestClient) -> None:
    assert admin_client.get("/frames?project_id=abc").status_code == 422
