import os

from sqlalchemy import create_engine

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
