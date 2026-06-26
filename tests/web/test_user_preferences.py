"""Web integration tests for user display preferences (theme and timezone).

Covers:
- Model column defaults
- Theme preference persists server-side and round-trips
- Preference endpoints set theme and timezone
- Timezone: invalid name is rejected gracefully, valid name is stored
- Migration 006 round-trip (upgrade + downgrade)
"""

from __future__ import annotations

from alembic import command as alembic_command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from tests.conftest import csrf_of


class TestUserModelDefaults:
    """The new preference columns have the correct default values."""

    def test_theme_preference_default_is_system(self, admin_client: TestClient) -> None:
        """A freshly-created user has theme_preference='system'."""
        from timelapse_manager.db.models.user import User
        from timelapse_manager.db.session import session_scope
        from timelapse_manager.runtime import get_context

        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            admin = db.query(User).filter(User.username == "admin").first()
            assert admin is not None
            assert admin.theme_preference == "system"

    def test_viewer_timezone_default_is_none(self, admin_client: TestClient) -> None:
        """A freshly-created user has viewer_timezone=None."""
        from timelapse_manager.db.models.user import User
        from timelapse_manager.db.session import session_scope
        from timelapse_manager.runtime import get_context

        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            admin = db.query(User).filter(User.username == "admin").first()
            assert admin is not None
            assert admin.viewer_timezone is None


class TestThemePreference:
    """Theme preference is persisted and round-trips correctly."""

    def test_post_theme_stores_preference(self, admin_client: TestClient) -> None:
        """POST /account/theme with a valid value updates the DB record."""
        from timelapse_manager.db.models.user import User
        from timelapse_manager.db.session import session_scope
        from timelapse_manager.runtime import get_context

        csrf = csrf_of(admin_client, "/")
        resp = admin_client.post(
            "/account/theme",
            data={"theme": "dark", "csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 204

        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            admin = db.query(User).filter(User.username == "admin").first()
            assert admin is not None
            assert admin.theme_preference == "dark"

    def test_post_theme_light(self, admin_client: TestClient) -> None:
        """POST /account/theme with 'light' stores 'light'."""
        from timelapse_manager.db.models.user import User
        from timelapse_manager.db.session import session_scope
        from timelapse_manager.runtime import get_context

        csrf = csrf_of(admin_client, "/")
        admin_client.post(
            "/account/theme",
            data={"theme": "light", "csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            admin = db.query(User).filter(User.username == "admin").first()
            assert admin is not None
            assert admin.theme_preference == "light"

    def test_post_theme_invalid_value_ignored(self, admin_client: TestClient) -> None:
        """POST /account/theme with an invalid value is silently ignored (204)."""
        from timelapse_manager.db.models.user import User
        from timelapse_manager.db.session import session_scope
        from timelapse_manager.runtime import get_context

        csrf = csrf_of(admin_client, "/")
        resp = admin_client.post(
            "/account/theme",
            data={"theme": "rainbow", "csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 204
        # Theme remains at the default
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            admin = db.query(User).filter(User.username == "admin").first()
            assert admin is not None
            assert admin.theme_preference == "system"

    def test_theme_preference_visible_in_preferences_page(
        self, admin_client: TestClient
    ) -> None:
        """The preferences page renders the stored theme."""
        csrf = csrf_of(admin_client, "/")
        admin_client.post(
            "/account/theme",
            data={"theme": "dark", "csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp = admin_client.get("/account/preferences")
        assert resp.status_code == 200
        assert 'value="dark"' in resp.text

    def test_viewer_can_set_theme(self, viewer_client: TestClient) -> None:
        """Non-admin users can also set their theme preference."""
        csrf = csrf_of(viewer_client, "/")
        resp = viewer_client.post(
            "/account/theme",
            data={"theme": "light", "csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 204


class TestTimezonePreference:
    """Timezone preference is validated and persisted correctly."""

    def test_post_valid_timezone_stores_it(self, admin_client: TestClient) -> None:
        """POST /account/timezone with a valid IANA name stores it."""
        from timelapse_manager.db.models.user import User
        from timelapse_manager.db.session import session_scope
        from timelapse_manager.runtime import get_context

        csrf = csrf_of(admin_client, "/")
        resp = admin_client.post(
            "/account/timezone",
            data={"timezone": "America/New_York", "csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 204

        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            admin = db.query(User).filter(User.username == "admin").first()
            assert admin is not None
            assert admin.viewer_timezone == "America/New_York"

    def test_post_invalid_timezone_ignored(self, admin_client: TestClient) -> None:
        """POST /account/timezone with an invalid name silently ignores it (204)."""
        from timelapse_manager.db.models.user import User
        from timelapse_manager.db.session import session_scope
        from timelapse_manager.runtime import get_context

        csrf = csrf_of(admin_client, "/")
        resp = admin_client.post(
            "/account/timezone",
            data={"timezone": "Not/A/Real/Zone", "csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 204

        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            admin = db.query(User).filter(User.username == "admin").first()
            assert admin is not None
            assert admin.viewer_timezone is None  # unchanged

    def test_post_timezone_blank_is_noop(self, admin_client: TestClient) -> None:
        """Posting a blank timezone is a no-op (does not crash, returns 204)."""
        from timelapse_manager.db.models.user import User
        from timelapse_manager.db.session import session_scope
        from timelapse_manager.runtime import get_context

        csrf = csrf_of(admin_client, "/")
        resp = admin_client.post(
            "/account/timezone",
            data={"timezone": "", "csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 204
        # Value remains None (empty string is skipped)
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            admin = db.query(User).filter(User.username == "admin").first()
            assert admin is not None
            assert admin.viewer_timezone is None

    def test_timezone_appears_in_preferences_page(
        self, admin_client: TestClient
    ) -> None:
        """After setting a timezone, it appears in the preferences page."""
        csrf = csrf_of(admin_client, "/")
        admin_client.post(
            "/account/timezone",
            data={"timezone": "Europe/London", "csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp = admin_client.get("/account/preferences")
        assert resp.status_code == 200
        assert "Europe/London" in resp.text

    def test_viewer_can_set_timezone(self, viewer_client: TestClient) -> None:
        """Non-admin users can also set their timezone preference."""
        csrf = csrf_of(viewer_client, "/")
        resp = viewer_client.post(
            "/account/timezone",
            data={"timezone": "Asia/Tokyo", "csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 204


class TestPreferencesPage:
    """Account preferences page renders correctly for all roles."""

    def test_preferences_page_renders_for_admin(self, admin_client: TestClient) -> None:
        """Admin can access the preferences page."""
        resp = admin_client.get("/account/preferences")
        assert resp.status_code == 200
        assert "Display Preferences" in resp.text
        assert "Colour Theme" in resp.text
        assert "Display Timezone" in resp.text

    def test_preferences_page_renders_for_viewer(
        self, viewer_client: TestClient
    ) -> None:
        """Viewer-role user can access the preferences page."""
        resp = viewer_client.get("/account/preferences")
        assert resp.status_code == 200
        assert "Display Preferences" in resp.text

    def test_preferences_page_unauthenticated_redirects(
        self, anon_client: TestClient
    ) -> None:
        """Unauthenticated access is redirected to login."""
        resp = anon_client.get("/account/preferences", follow_redirects=False)
        assert resp.status_code in (302, 303, 307, 401)


class TestMigration006:
    """Migration 006 adds and removes the preference columns correctly."""

    def test_migration_006_upgrade_adds_columns(self, alembic_cfg: Config) -> None:
        """Running upgrade to 006 creates both preference columns on user."""
        alembic_command.upgrade(alembic_cfg, "006_add_user_preferences")
        url = alembic_cfg.get_main_option("sqlalchemy.url")
        assert url is not None
        engine = create_engine(url)
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("user")}
        assert "theme_preference" in cols
        assert "viewer_timezone" in cols
        engine.dispose()

    def test_migration_006_downgrade_removes_columns(self, alembic_cfg: Config) -> None:
        """Downgrading from 006 removes the preference columns."""
        alembic_command.upgrade(alembic_cfg, "006_add_user_preferences")
        alembic_command.downgrade(alembic_cfg, "005_add_project_campaign_bounds")
        url = alembic_cfg.get_main_option("sqlalchemy.url")
        assert url is not None
        engine = create_engine(url)
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("user")}
        assert "theme_preference" not in cols
        assert "viewer_timezone" not in cols
        engine.dispose()

    def test_migration_006_theme_default_is_system(self, alembic_cfg: Config) -> None:
        """After upgrade, an inserted row gets theme_preference='system' by default."""
        alembic_command.upgrade(alembic_cfg, "006_add_user_preferences")
        url = alembic_cfg.get_main_option("sqlalchemy.url")
        assert url is not None
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO user (username, auth_source, role, enabled, "
                    "password_hash) VALUES ('testuser', 'local', 'viewer', 1, 'x')"
                )
            )
            row = conn.execute(
                text(
                    "SELECT theme_preference, viewer_timezone FROM user "
                    "WHERE username='testuser'"
                )
            ).fetchone()
        engine.dispose()
        assert row is not None
        assert row[0] == "system"
        assert row[1] is None
