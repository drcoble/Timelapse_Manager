"""camera pinned TLS certificate

Issue #69 (CAM-S1, the pinning half): a camera with a factory self-signed
certificate cannot be verified against a CA bundle. An operator can instead pin
the camera's exact certificate, after confirming its fingerprint:

* ``camera.tls_pinned_cert_pem`` TEXT NULL -- the PEM of the pinned certificate
  (public data, not a secret).

Every existing row reads ``NULL`` (no pin), so no camera's behaviour changes on
upgrade. Nothing captures a pin automatically: it is set only by an explicit
operator action in the camera form.

Native ``ALTER TABLE ... ADD COLUMN``: the ``camera`` table is not rebuilt, so
no ``ON DELETE CASCADE`` from ``project`` can fire. The downgrade is a native
``DROP COLUMN`` (SQLite 3.35 or later; the frozen build bundles 3.53.1).

Revision ID: 039_camera_tls_pinned_cert
Revises: 038_camera_allow_basic_over_http
Create Date: 2026-10-28

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "039_camera_tls_pinned_cert"
down_revision: str | None = "038_camera_allow_basic_over_http"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("camera", sa.Column("tls_pinned_cert_pem", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("camera", "tls_pinned_cert_pem")
