import os

from sqlalchemy import create_engine, text

from .models import metadata


def _normalize_database_url(url: str) -> str:
    """Accept Heroku/Render-style postgres:// URLs and force the psycopg2 driver."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


DATABASE_URL = _normalize_database_url(
    os.environ.get("DATABASE_URL", "postgresql+psycopg2://wallet:wallet@localhost:5432/wallet")
)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=20,
    max_overflow=40,
    pool_timeout=30,
    future=True,
)


def init_db() -> None:
    metadata.create_all(engine)
    _migrate_reversal_column()


def _migrate_reversal_column() -> None:
    """In-place migration for DBs created before reversals existed.

    create_all() only creates missing tables, it never alters existing
    ones, so a table from an older deploy needs this column/constraint
    added by hand. Safe to run on every startup.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE transfers ADD COLUMN IF NOT EXISTS "
                "reversal_of_transfer_id VARCHAR(36) REFERENCES transfers(id)"
            )
        )
        conn.execute(
            text(
                "DO $$ BEGIN "
                "IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_transfers_reversal_of') THEN "
                "ALTER TABLE transfers ADD CONSTRAINT uq_transfers_reversal_of UNIQUE (reversal_of_transfer_id); "
                "END IF; END $$;"
            )
        )
