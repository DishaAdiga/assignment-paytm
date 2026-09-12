import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .logging_conf import log_event
from .metrics import WALLETS_CREATED
from .models import wallets


def get_or_create_wallet(engine, owner_token_hash: str, initial_balance_paise: int = 0):
    """Race-free get-or-create.

    A unique index on owner_token_hash plus INSERT ... ON CONFLICT DO NOTHING
    guarantees that two concurrent callers for the same user converge on a
    single row: only one INSERT wins, the other blocks on the conflicting
    unique index entry until the winner commits, then reads it back.
    """
    with engine.begin() as conn:
        stmt = (
            pg_insert(wallets)
            .values(
                id=str(uuid.uuid4()),
                owner_token_hash=owner_token_hash,
                balance_paise=initial_balance_paise,
            )
            .on_conflict_do_nothing(index_elements=["owner_token_hash"])
            .returning(wallets.c.id)
        )
        row = conn.execute(stmt).fetchone()
        created = row is not None
        if row is None:
            row = conn.execute(
                select(wallets.c.id).where(wallets.c.owner_token_hash == owner_token_hash)
            ).fetchone()
        wallet = conn.execute(select(wallets).where(wallets.c.id == row.id)).fetchone()

    if created:
        log_event("wallet_created", wallet_id=wallet.id)
        WALLETS_CREATED.inc()
    return wallet, created


def get_wallet(engine, wallet_id: str):
    with engine.begin() as conn:
        return conn.execute(select(wallets).where(wallets.c.id == wallet_id)).fetchone()
