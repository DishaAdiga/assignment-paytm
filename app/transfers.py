import hashlib
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .logging_conf import log_event
from .metrics import (
    IDEMPOTENCY_CONFLICTS,
    IDEMPOTENT_REPLAYS,
    TRANSFERS_CREATED,
    TRANSFERS_DECLINED,
)
from .models import transfers, wallets


class IdempotencyConflict(Exception):
    """Same idempotency key reused with a different request body."""

    def __init__(self, existing):
        self.existing = existing


def _request_hash(from_id: str, to_id: str, amount_paise: int) -> str:
    payload = json.dumps(
        {"from": from_id, "to": to_id, "amount_paise": amount_paise}, sort_keys=True
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def create_transfer(engine, idempotency_key: str, from_id: str, to_id: str, amount_paise: int):
    req_hash = _request_hash(from_id, to_id, amount_paise)
    transfer_id = str(uuid.uuid4())

    with engine.begin() as conn:
        # Lock both wallet rows with separate SELECT ... FOR UPDATE calls,
        # issued in ascending-id order, BEFORE inserting the transfer row.
        # This has to happen first: inserting a row with from_wallet_id /
        # to_wallet_id foreign keys makes Postgres implicitly take a share
        # lock on both referenced wallet rows, in column order rather than
        # sorted order. If we let the INSERT do that first, two opposite-
        # direction transfers can deadlock on those implicit FK locks even
        # though our own explicit locking is sorted. Taking the stronger
        # FOR UPDATE lock ourselves first, in a fixed global order, means
        # the INSERT's implicit lock is already held by our own transaction
        # and never has to wait.
        balances = {}
        for wallet_id in sorted([from_id, to_id]):
            row = conn.execute(
                select(wallets.c.id, wallets.c.balance_paise)
                .where(wallets.c.id == wallet_id)
                .with_for_update()
            ).fetchone()
            if row is not None:
                balances[row.id] = row.balance_paise

        insert_stmt = (
            pg_insert(transfers)
            .values(
                id=transfer_id,
                idempotency_key=idempotency_key,
                from_wallet_id=from_id,
                to_wallet_id=to_id,
                amount_paise=amount_paise,
                status="processing",
                request_hash=req_hash,
            )
            .on_conflict_do_nothing(index_elements=["from_wallet_id", "idempotency_key"])
            .returning(transfers.c.id)
        )
        inserted = conn.execute(insert_stmt).fetchone()

        if inserted is None:
            # Someone already used this (sender, key) pair. Postgres made us
            # wait for that transaction to finish, so this read sees the
            # final, committed outcome - never a half-applied transfer.
            existing = conn.execute(
                select(transfers).where(
                    transfers.c.from_wallet_id == from_id,
                    transfers.c.idempotency_key == idempotency_key,
                )
            ).fetchone()
            if existing.request_hash != req_hash:
                log_event(
                    "idempotency_conflict",
                    transfer_id=existing.id,
                    idempotency_key=idempotency_key,
                )
                IDEMPOTENCY_CONFLICTS.inc()
                raise IdempotencyConflict(existing)
            log_event(
                "idempotent_replay_hit",
                transfer_id=existing.id,
                idempotency_key=idempotency_key,
            )
            IDEMPOTENT_REPLAYS.inc()
            return existing, True

        log_event(
            "transfer_created",
            transfer_id=transfer_id,
            from_wallet=from_id,
            to_wallet=to_id,
            amount_paise=amount_paise,
        )

        from_balance = balances.get(from_id)
        now = datetime.now(timezone.utc)

        if from_balance is None or from_balance < amount_paise:
            conn.execute(
                update(transfers)
                .where(transfers.c.id == transfer_id)
                .values(status="declined", decline_reason="insufficient_funds", completed_at=now)
            )
            log_event("transfer_declined", transfer_id=transfer_id, reason="insufficient_funds")
            TRANSFERS_DECLINED.inc()
        else:
            conn.execute(
                update(wallets)
                .where(wallets.c.id == from_id)
                .values(balance_paise=wallets.c.balance_paise - amount_paise)
            )
            log_event(
                "wallet_debited", transfer_id=transfer_id, wallet_id=from_id, amount_paise=amount_paise
            )
            conn.execute(
                update(wallets)
                .where(wallets.c.id == to_id)
                .values(balance_paise=wallets.c.balance_paise + amount_paise)
            )
            log_event(
                "wallet_credited", transfer_id=transfer_id, wallet_id=to_id, amount_paise=amount_paise
            )
            conn.execute(
                update(transfers)
                .where(transfers.c.id == transfer_id)
                .values(status="completed", completed_at=now)
            )
            log_event("transfer_completed", transfer_id=transfer_id)
            TRANSFERS_CREATED.inc()

        final = conn.execute(select(transfers).where(transfers.c.id == transfer_id)).fetchone()

    return final, False


def get_transfer(engine, transfer_id: str):
    with engine.begin() as conn:
        return conn.execute(select(transfers).where(transfers.c.id == transfer_id)).fetchone()
