from prometheus_client import Counter, Histogram

REQUEST_COUNT = Counter(
    "http_requests_total", "Total HTTP requests", ["method", "path", "status"]
)
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds", "HTTP request latency in seconds", ["method", "path"]
)

WALLETS_CREATED = Counter("wallets_created_total", "Wallets created")
TRANSFERS_CREATED = Counter("transfers_created_total", "Transfers that completed successfully")
TRANSFERS_DECLINED = Counter(
    "transfers_declined_insufficient_funds_total", "Transfers declined for insufficient funds"
)
IDEMPOTENT_REPLAYS = Counter("idempotent_replays_total", "Idempotency key replay hits")
IDEMPOTENCY_CONFLICTS = Counter(
    "idempotency_conflicts_total", "Same idempotency key reused with a different request body"
)
