# Wallet Service

A small peer-to-peer wallet API (FastAPI + PostgreSQL) with race-free
get-or-create, exactly-once transfers, and no-overdraft/conservation
guarantees under concurrency.

## Quickstart

```bash
docker compose up --build
```

This brings up Postgres + the API on `http://localhost:8000`. Interactive
API docs at `http://localhost:8000/docs`.

Auth is a simple bearer token: any non-empty string identifies a caller
(the token is hashed with SHA-256 and used as the wallet owner key - there is
no separate signup step).

## Test UI

A small, dependency-free HTML/JS console is served by the app itself at
`http://localhost:8000/ui/` (or `/` redirects there). It lets you:

- set the base URL (defaults to the current origin, so it also works
  against a deployed instance if you host the page yourself) and the
  bearer token to act as;
- get-or-create a wallet and check its balance;
- submit a transfer (with a "Generate" button for the idempotency key) and
  copy wallet ids between fields;
- look up a transfer's status;
- see a running log of every request/response made from the page.

It's meant for quick manual poking, not for the concurrency invariants —
use `scripts/burst_test.py` for those.

## API

All endpoints require `Authorization: Bearer <token>`.

### `POST /wallets` — get-or-create

```bash
curl -s -X POST http://localhost:8000/wallets \
  -H "Authorization: Bearer alice" \
  -H "Content-Type: application/json" \
  -d '{"initial_balance_paise": 100000}'
```

`initial_balance_paise` (optional, default `0`) is only applied the *first*
time a wallet is created for that token; it's not part of the minimum spec
but is included since the spec defines no other way to fund a wallet, and
the burst scripts need funded wallets to exercise transfers.

```json
{"id": "…", "balance_paise": 100000, "created_at": "…"}
```

### `GET /wallets/{id}` — balance (owner only)

```bash
curl -s http://localhost:8000/wallets/<id> -H "Authorization: Bearer alice"
```

### `POST /transfers` — move money

The caller must own the `from` wallet.

```bash
curl -s -X POST http://localhost:8000/transfers \
  -H "Authorization: Bearer alice" \
  -H "Content-Type: application/json" \
  -d '{"from": "<wallet_a>", "to": "<wallet_b>", "amount_paise": 2500, "idempotency_key": "order-42"}'
```

- `201` new transfer processed (`status`: `completed` or `declined`).
- `200` + `Idempotent-Replay: true` header — retry of the same key/body,
  original result returned unchanged.
- `409` — same `idempotency_key`, different body.
- `403` — caller doesn't own the `from` wallet.

### `GET /transfers/{id}` — status (visible to either party)

```bash
curl -s http://localhost:8000/transfers/<id> -H "Authorization: Bearer alice"
```

### `POST /transfers/{id}/reverse` — refund a completed transfer

Moves the transfer's exact amount back from recipient to sender. Visible to
either party of the original transfer. Reuses the same sorted-lock +
lock-protected-balance-check primitive as `POST /transfers`, with roles
swapped, so it inherits the same conservation/no-overdraft guarantees.

```bash
curl -s -X POST http://localhost:8000/transfers/<id>/reverse \
  -H "Authorization: Bearer alice" \
  -H "Content-Type: application/json" \
  -d '{"idempotency_key": "reverse-order-42"}'
```

- `201` new reversal processed (`status`: `completed` or `declined`).
- `200` + `Idempotent-Replay: true` — retry of the same reversal key, same
  result returned unchanged.
- `409` — the transfer isn't `completed` (nothing to reverse), or it has
  already been reversed (via a `UNIQUE` constraint on
  `reversal_of_transfer_id`, so at most one reversal per transfer ever
  exists regardless of which idempotency key is used), or the same
  reversal key was reused with a different body.
- A `declined`/`insufficient_funds` result (not an error) if the recipient
  no longer has the funds to refund.

### `GET /metrics` — Prometheus exposition format

### `GET /health` — liveness/health check (used by the Docker `HEALTHCHECK`)

## Burst / invariant scripts

```bash
pip install -r scripts/requirements.txt
./scripts/burst.sh https://your-deployed-url 20
# or directly:
python scripts/burst_test.py --base-url https://your-deployed-url --scenario all
```

Runs, against any deployed URL:

1. **Concurrent get-or-create** — N simultaneous `POST /wallets` for a brand
   new user, asserts exactly one wallet id comes back.
2. **Idempotent retry storm** — the same transfer (same key) fired K times
   concurrently, asserts one transfer id, byte-identical response bodies,
   and exactly one debit/credit.
3. **Conservation under contention** — many concurrent transfers over a
   small set of wallets (including opposite-direction pairs at once),
   asserts total balance is unchanged and no balance went negative.
4. **Reversal storm** — reverses the same completed transfer K times
   concurrently, asserts exactly one reversal is applied, the funds are
   fully back, and reversing it again afterwards (with a new idempotency
   key) is cleanly rejected with `409` instead of double-refunding.

Each scenario exits non-zero on failure so it can be used as a CI gate.

## Design write-up

### Handling transfers safely

Each transfer is handled inside a **single Postgres transaction**.

The flow is:

1. Lock both wallet rows using `SELECT ... FOR UPDATE`.

   The wallets are always locked in **ascending wallet ID order**, regardless of which wallet is sending the money.

2. Insert the transfer with `status = 'processing'`.

   This insert also handles idempotency, so it happens in the same transaction as the balance update.

   It is important that we lock the wallets **before** inserting the transfer. The foreign keys on `from_wallet_id` and `to_wallet_id` cause Postgres to take locks on those wallet rows during the insert. By locking the wallets ourselves first, and doing it in a fixed order, we avoid lock-order problems.

3. Check the sender's balance.

   * If the balance is too low, mark the transfer as `declined` and commit. The balances are not changed.
   * If there is enough money, debit the sender, credit the receiver, mark the transfer as `completed`, and commit.

This is enough to prevent overdrafts because the balance check and balance update happen while both wallet rows are locked.

We don't need `SERIALIZABLE` isolation here. `READ COMMITTED` with the two row locks is enough because every transfer only needs to protect the two wallets involved.

### Avoiding deadlocks

The important part is that wallets are always locked in the same order.

For example, if one request transfers from wallet 10 to wallet 20, while another transfers from wallet 20 to wallet 10, both requests will try to lock wallet 10 first and wallet 20 second.

This prevents them from getting stuck waiting for each other.

The wallet locks also happen **before** the transfer row is inserted. This matters because the foreign-key checks during the insert can also lock the referenced wallet rows.

I initially had the locks after the transfer insert, and the burst test showed that this could deadlock. Moving the wallet locks before the insert fixed that.

There is also a `CHECK (balance_paise >= 0)` constraint on the wallet table as an extra safety net. The application should prevent negative balances, but the database constraint protects us if there is ever a bug in the application code.

### Idempotency

Idempotency is handled by a unique constraint on:

`(from_wallet_id, idempotency_key)`

This means the same idempotency key can be reused by different wallets, but not twice by the same sender.

The transfer row is inserted with:

`INSERT ... ON CONFLICT DO NOTHING`

This happens inside the **same transaction** as the balance changes. So the idempotency check and the actual money movement either both succeed or both roll back.

If two requests arrive at the same time with the same idempotency key, Postgres handles the race for us. The second insert waits for the first transaction to finish and then checks for the conflict again.

That means the second request sees the final result of the first request, rather than a partially completed transfer.

For a retry, we also compare a SHA-256 hash of the important request fields:

`from`, `to`, and `amount_paise`

There are two cases:

* **Same hash:** This is a retry of the original request. Return the existing transfer with `200` and `Idempotent-Replay: true`.
* **Different hash:** The same idempotency key was used for a different transfer. Return `409 Conflict` and don't move any money.


## Deploying (Render + free managed Postgres)

`render.yaml` is a Render Blueprint: a free Postgres instance plus a Docker
web service wired together via `DATABASE_URL`.

1. Push this repo to GitHub.
2. In Render, **New -> Blueprint**, point it at the repo (it will read
   `render.yaml` and provision both the database and the web service).
3. Once deployed, Render gives you a public URL

## Testing using Render URL
1. Wallet card: Initial balance 10000 -> click Get / Create Wallet (this is Alice's wallet). Click Use as From.
2. Change Bearer token to bob -> Get / Create Wallet with balance 0 (Bob's wallet). Click Use as To.
3. Switch Bearer token back to alice (Basically, before hitting transfer amount, bearer token must be set to user who has been marked as 'Use as From')
4. Create transfer card: From/To are already filled. Amount 5000, click Generate for idempotency key, click Submit Transfer -> expect status: completed. The transfer id auto-fills into the Transfer status card.
5. Transfer status card: click Get Transfer Status to confirm completed.
6. Click Generate next to "Reversal idempotency key", then click Reverse Transfer -> expect a new transfer with status: completed and reversal_of pointing back at the original id.
7. Check balances: switch token to alice, paste Alice's wallet id, Check Balance -> should be back to 10000. Switch to bob, check his wallet -> should be 0.
8. Click Reverse Transfer again with the same key -> same result replayed (200, Idempotent-Replay behavior — same reversal id, no change).
9. Click Generate for a new reversal key and click Reverse Transfer again -> expect an error response with 409 ("transfer has already been reversed") shown in the Activity log.
