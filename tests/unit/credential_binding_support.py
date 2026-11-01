"""Let a test that exercises default-credential substitution run without an app.

The global default login reaches a camera only when the camera resolves inside
``ssrf.allowed_private_subnets`` (#67-S3, owner decision OD-1), and that policy
is read from the running application's settings. A unit test that builds its own
supervisor or listener has no application context, so it imports the autouse
fixture below, which supplies the policy the fixtures' ``10.x`` / ``192.168.x``
addresses sit in. Not a test module.
"""

from __future__ import annotations

import pytest

from timelapse_manager.security import credential_binding

ALLOWED = ["10.0.0.0/8", "192.168.0.0/16"]


@pytest.fixture(autouse=True)
def _allowed_private_subnets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat ``10.0.0.0/8`` and ``192.168.0.0/16`` as the allowed camera subnets."""
    monkeypatch.setattr(credential_binding, "_configured_subnets", lambda: ALLOWED)
