"""baseline - current fdt_coding schema

Revision ID: 001_baseline
Revises: None
Create Date: 2026-05-31
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS fdt_coding_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            olt_initial TEXT NOT NULL DEFAULT '',
            hub_id TEXT NOT NULL DEFAULT '',
            olt_name_initial TEXT NOT NULL DEFAULT '',
            olt_hostname TEXT NOT NULL DEFAULT '',
            site_name TEXT NOT NULL,
            fdt_order INTEGER,
            fdt_id TEXT NOT NULL DEFAULT '',
            fdt_code TEXT NOT NULL,
            index_code TEXT NOT NULL DEFAULT '',
            cluster_initial TEXT NOT NULL DEFAULT '',
            source_spreadsheet_id TEXT NOT NULL DEFAULT '',
            sheet_name TEXT NOT NULL DEFAULT '',
            source_row INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_fdt_coding_olt ON fdt_coding_records(olt_initial)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_fdt_coding_site ON fdt_coding_records(site_name)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_fdt_coding_fdt_code ON fdt_coding_records(fdt_code)")
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_fdt_coding_unique ON fdt_coding_records(
            olt_initial, site_name, fdt_order, fdt_code
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS fdt_coding_olts (
            olt_initial TEXT PRIMARY KEY,
            hub_id TEXT NOT NULL DEFAULT '',
            olt_name_initial TEXT NOT NULL DEFAULT '',
            olt_hostname TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS fdt_coding_olt_catalog (
            olt_code TEXT PRIMARY KEY,
            olt_initial TEXT NOT NULL DEFAULT '',
            olt_name_initial TEXT NOT NULL DEFAULT '',
            olt_hostname TEXT NOT NULL DEFAULT '',
            olt_vendor TEXT NOT NULL DEFAULT '',
            olt_area TEXT NOT NULL DEFAULT '',
            source_row INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_fdt_olt_catalog_name ON fdt_coding_olt_catalog(olt_initial)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS fdt_coding_olt_catalog")
    op.execute("DROP TABLE IF EXISTS fdt_coding_olts")
    op.execute("DROP TABLE IF EXISTS fdt_coding_records")
