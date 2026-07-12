import os
import sys
from pathlib import Path

from alembic import context
from logging.config import fileConfig

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

DB_PATH = BASE_DIR / "data" / "fdt_coding.db"
config.set_main_option("sqlalchemy.url", f"sqlite:///{DB_PATH}")

SCHEMA = """
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
);
CREATE INDEX IF NOT EXISTS idx_fdt_coding_olt ON fdt_coding_records(olt_initial);
CREATE INDEX IF NOT EXISTS idx_fdt_coding_site ON fdt_coding_records(site_name);
CREATE INDEX IF NOT EXISTS idx_fdt_coding_fdt_code ON fdt_coding_records(fdt_code);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fdt_coding_unique ON fdt_coding_records(
    olt_initial, site_name, fdt_order, fdt_code
);
CREATE TABLE IF NOT EXISTS fdt_coding_olts (
    olt_initial TEXT PRIMARY KEY,
    hub_id TEXT NOT NULL DEFAULT '',
    olt_name_initial TEXT NOT NULL DEFAULT '',
    olt_hostname TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
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
);
CREATE INDEX IF NOT EXISTS idx_fdt_olt_catalog_name ON fdt_coding_olt_catalog(olt_initial);
"""


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    import sqlite3
    connectable = sqlite3.connect(str(DB_PATH))
    try:
        context.configure(connection=connectable, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()
    finally:
        connectable.close()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
