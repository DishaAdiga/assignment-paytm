"""SQLAlchemy Core table definitions for the wallet service."""
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    func,
)

metadata = MetaData()

wallets = Table(
    "wallets",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("owner_token_hash", String(64), nullable=False, unique=True),
    Column("balance_paise", BigInteger, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("balance_paise >= 0", name="ck_wallets_balance_nonneg"),
)

transfers = Table(
    "transfers",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("idempotency_key", String(255), nullable=False),
    Column("from_wallet_id", String(36), ForeignKey("wallets.id"), nullable=False),
    Column("to_wallet_id", String(36), ForeignKey("wallets.id"), nullable=False),
    Column("amount_paise", BigInteger, nullable=False),
    Column("status", String(20), nullable=False),
    Column("decline_reason", String(64)),
    Column("request_hash", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("completed_at", DateTime(timezone=True)),
    # Set only on a reversal row, pointing back at the transfer it reverses.
    # Unique (Postgres excludes NULLs from uniqueness checks) so a given
    # transfer can have at most one reversal ever - this is what blocks a
    # double-refund even if a different idempotency key is used the second time.
    Column("reversal_of_transfer_id", String(36), ForeignKey("transfers.id")),
    CheckConstraint("amount_paise > 0", name="ck_transfers_amount_positive"),
    CheckConstraint("status in ('processing','completed','declined')", name="ck_transfers_status"),
    # Scope idempotency to the sender: replaying the same key from the same
    # source wallet applies once; unrelated senders can't collide on a key.
    UniqueConstraint("from_wallet_id", "idempotency_key", name="uq_transfer_sender_idempotency"),
    UniqueConstraint("reversal_of_transfer_id", name="uq_transfers_reversal_of"),
)
