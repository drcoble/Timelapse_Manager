"""add ldap tls ca cert trust-anchor path

Adds one nullable column to ``ldap_settings``:

* ``tls_ca_cert_path`` -- an optional filesystem path to a CA-certificate (PEM)
  trust anchor used to validate the directory's LDAPS / StartTLS certificate
  against a private or internal CA, without modifying the host OS trust store.
  Nullable; when unset, certificate validation falls back to the platform trust
  store. This is non-secret plain configuration (a path, not a credential), so it
  is stored verbatim -- never masked or encrypted. Certificate validation always
  stays ``CERT_REQUIRED``; there is no skip-verification option.

Revision ID: 008_add_ldap_tls_ca_cert
Revises: 007_add_ldap_auth_columns
Create Date: 2026-06-16

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "008_add_ldap_tls_ca_cert"
down_revision: str | None = "007_add_ldap_auth_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("ldap_settings") as batch_op:
        batch_op.add_column(
            sa.Column("tls_ca_cert_path", sa.String(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("ldap_settings") as batch_op:
        batch_op.drop_column("tls_ca_cert_path")
