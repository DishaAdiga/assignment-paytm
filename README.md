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

## Testing with Postman

A ready-made collection + environment are in [`postman/`](postman/):

- `postman/wallet-service.postman_collection.json`
- `postman/wallet-service.postman_environment.json`

Import both in Postman, select the "Wallet Service - Local" environment
(edit `base_url` if testing a deployed instance), then run requests in this
order (the collection is organized to match):

1. **Wallets → Create/Get Wallet A** and **Create/Get Wallet B** — each
   saves the returned id into `wallet_a_id` / `wallet_b_id` via a test
   script, so later requests can reference them automatically.
2. **Wallets → Get Wallet A Balance** / **Get Wallet A - wrong owner** —
   shows the balance read and the `403` you get from the wrong token.
3. **Transfers → 1. Create Transfer (A -> B)** — generates a fresh
   `idempotency_key` (via a pre-request script, using Postman's `{{$guid}}`
   dynamic variable) the first time it's run, and saves the resulting
   `transfer_id`.
4. **Transfers → 2. Retry Same Transfer** — resends the exact same body and
   key; asserts the response has the *same* `transfer_id` (idempotent
   replay).
5. **Transfers → 3. Replay With Different Body** — same key, different
   `amount_paise`; asserts `409`.
6. **Transfers → 4. Get Transfer Status** — reads it back by id.
7. **Transfers → 5. Overdraft attempt** — a huge amount; asserts the
   transfer is created but `status: "declined"` /
   `decline_reason: "insufficient_funds"` (not an HTTP error).
8. **Transfers → 6. Transfer from wallet you don't own** — asserts `403`.

Each request has a `pm.test(...)` assertion, so you can also run the whole
"Transfers" folder with the Collection Runner and see pass/fail per step.
Note the Collection Runner executes requests sequentially — it's useful for
functional checks like the ones above, but it does **not** exercise true
concurrency; for the concurrent get-or-create / retry-storm / conservation
invariants, use `scripts/burst_test.py`, which fires real parallel requests.

To start a fresh idempotency demo, clear the `idempotency_key` value in the
environment before re-running step 3 (otherwise it reuses the same key and
you'll keep seeing a replay).

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

### Conservation + no-overdraft: the simplest correct mechanism

Each transfer runs in a **single Postgres transaction**:

1. Lock both wallet rows with two `SELECT ... WHERE id = :id FOR UPDATE`
   calls, issued in **ascending wallet-id order**, regardless of which one
   is the sender.
2. Insert the transfer row (`status='processing'`) — this is also the
   idempotency check, see below. This must happen **after** the wallet
   locks, not before: inserting a row with `from_wallet_id`/`to_wallet_id`
   foreign keys makes Postgres implicitly take a share lock on both
   referenced wallet rows, in column order rather than sorted order. Doing
   our own stronger, sorted lock first means that implicit FK lock is
   already held by our own transaction and never has to wait on it.
3. In application code, compare the locked `from` balance against the
   amount. If insufficient, mark the transfer `declined` and commit — no
   balance is touched. Otherwise debit `from`, credit `to`, mark
   `completed`, commit.

Why this is the simplest correct thing:

- Row locks under the default `READ COMMITTED` isolation are enough,
  because the only invariant that matters (`balance >= 0` and
  `debit == credit`) is checked and enforced while holding an exclusive
  lock on exactly the two rows involved. No wider read-set is needed, so
  there's nothing to gain from `SERIALIZABLE`.
- **Deadlock avoidance**: the two lock-acquiring `SELECT ... FOR UPDATE`
  calls are always issued in ascending wallet-`id` order, not by
  sender/receiver role, and always *before* the transfer row is inserted
  (so the insert's implicit foreign-key lock on the wallet rows is already
  covered by our own lock). So a transfer A→B and a concurrent transfer
  B→A both try to lock the *lower* id first, then the *higher* id — locks
  are always acquired in the same global order, which makes a circular
  wait (and hence a deadlock) impossible. Verified this the hard way with
  the burst script: a first version that locked the wallets *after*
  inserting the transfer row still deadlocked, because the FK check inside
  the `INSERT` itself grabs share locks on both wallets in column order,
  independent of any sorting done afterwards. A `balance_paise >= 0`
  `CHECK` constraint is also kept as a defense-in-depth backstop in case
  application logic ever has a bug.
- Rejected alternatives:
  - **`SERIALIZABLE` isolation for the whole transaction** — gives the
    same correctness but converts contention into transaction *aborts*
    (`40001` serialization failures) that the client must detect and
    retry, instead of a plain wait. Under "many concurrent transfers over
    a small set of wallets" (exactly the grading scenario) this means a
    non-trivial abort rate and added retry-loop complexity for no extra
    safety, since explicit row locks already give us exact mutual
    exclusion on the only rows that matter.
  - **Optimistic concurrency (version column + retry loop)** — works, but
    turns high contention on a hot wallet into wasted retries and
    starvation risk; pessimistic locks are simpler here because the
    critical section (a couple of `UPDATE`s) is short.
  - **A single-writer queue/actor per wallet** — would also work but adds
    infrastructure (a durable queue, sharding by wallet) that a single
    Postgres instance's row locks already give us for free at this scale.

### Where idempotency lives

The uniqueness constraint is `UNIQUE (from_wallet_id, idempotency_key)` on
the `transfers` table (scoped to the sender, so unrelated callers can't
collide on the same key string). The `INSERT ... ON CONFLICT DO NOTHING`
that claims this key happens in the **same transaction** that later
performs the debit/credit and sets the final `status` — so the whole thing
commits or rolls back atomically; idempotency bookkeeping is never split
from the money movement.

Concurrency falls out of Postgres's own locking: if two requests race on
the same `(from_wallet_id, idempotency_key)`, the second `INSERT` blocks on
the first's uncommitted row until it commits or rolls back, then re-checks
for a conflict — so the loser always observes the *final*, committed
outcome, never a half-applied one.

On replay, we compare a SHA-256 hash of the semantically relevant fields
(`from`, `to`, `amount_paise`) against the stored hash from the original
request:

- same hash → **idempotent replay**: return the original transfer
  unchanged (`200` + `Idempotent-Replay: true`).
- different hash → **`409 Conflict`**: the key was reused for a different
  request, no new debit/credit is attempted.

### Consistency vs. availability

For a money workload we chose **consistency (CP)**: a single Postgres
primary is the one source of truth for every wallet; every read and write
goes through it with real row-level locking. If that instance (or its
region) is unreachable, the API is **unavailable** rather than risking a
stale balance or a write that could conflict with another once connectivity
returns. We consciously gave up multi-region write availability and
horizontal write scaling — all transfers funnel through one database — and
accepted that as the right trade-off for correctness of money movement over
uptime under partition.

## Observability

- **Logs**: structured JSON, one line per event, each tagged with a
  per-request `correlation_id` (from the incoming `X-Request-ID` header, or
  generated). Domain events logged: `wallet_created`, `transfer_created`,
  `wallet_debited`, `wallet_credited`, `transfer_completed`,
  `transfer_declined`, `idempotent_replay_hit`, `idempotency_conflict`, plus
  one `http_request` line per request with method/path/status/duration.
  Since the app logs to stdout, any host (Docker, Render, Railway, …) makes
  these viewable via its standard log stream/dashboard.
- **Metrics** (`/metrics`, Prometheus text format):
  - `http_requests_total{method,path,status}` — request rate / error rate.
  - `http_request_duration_seconds` (histogram) — compute p99 with
    `histogram_quantile(0.99, rate(http_request_duration_seconds_bucket[5m]))`.
  - Domain counters: `wallets_created_total`, `transfers_created_total`,
    `transfers_declined_insufficient_funds_total`,
    `idempotent_replays_total`, `idempotency_conflicts_total`.

## Deploying (Render + free managed Postgres)

`render.yaml` is a Render Blueprint: a free Postgres instance plus a Docker
web service wired together via `DATABASE_URL`.

1. Push this repo to GitHub.
2. In Render, **New → Blueprint**, point it at the repo (it will read
   `render.yaml` and provision both the database and the web service).
3. Once deployed, Render gives you a public URL — that's what you point
   `scripts/burst_test.py --base-url` at.

The same `Dockerfile` works unmodified on Railway, Fly.io, or Koyeb; just
provide a managed Postgres connection string as `DATABASE_URL` (the app
accepts both `postgres://` and `postgresql://` URLs).

## What's out of scope / notes

- No migration framework: the schema is small and stable, so
  `metadata.create_all()` (idempotent `CREATE TABLE IF NOT EXISTS`-style DDL)
  runs on startup instead of a heavier migration tool.
- No separate `users` table / signup flow: the bearer token itself is the
  user identity (hashed before storage), per "a simple bearer token per
  user identifies the caller."
- `initial_balance_paise` on `POST /wallets` is a pragmatic addition to fund
  wallets for demonstration purposes, since the minimum API defines no
  deposit/mint endpoint.
