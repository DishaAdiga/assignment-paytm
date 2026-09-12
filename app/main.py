import time
import uuid
from pathlib import Path
from typing import Optional

import anyio
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .auth import get_caller_token_hash
from .db import engine, init_db
from .logging_conf import configure_logging, correlation_id_var, log_event
from .metrics import REQUEST_COUNT, REQUEST_LATENCY
from .schemas import (
    TransferCreateRequest,
    TransferResponse,
    WalletCreateRequest,
    WalletResponse,
)
from .transfers import IdempotencyConflict, create_transfer, get_transfer
from .wallets import get_or_create_wallet, get_wallet

configure_logging()

app = FastAPI(title="Wallet Service")

# Open CORS so the bundled test UI (or Postman) can call a deployed URL from
# any origin. There's no cookie/session auth to protect against CSRF here -
# every request must carry an explicit bearer token - so this is safe for a
# demo/testing service.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/ui", StaticFiles(directory=STATIC_DIR, html=True), name="ui")


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/ui/")


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    # Bump the default worker-thread pool so a burst of concurrent sync
    # endpoint calls doesn't queue behind an artificially small limiter.
    anyio.to_thread.current_default_thread_limiter().total_tokens = 100


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    correlation_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    ctx_token = correlation_id_var.set(correlation_id)
    start = time.perf_counter()
    try:
        response = await call_next(request)
    finally:
        correlation_id_var.reset(ctx_token)
    duration = time.perf_counter() - start
    route = request.scope.get("route")
    path_label = getattr(route, "path", request.url.path)
    REQUEST_COUNT.labels(request.method, path_label, str(response.status_code)).inc()
    REQUEST_LATENCY.labels(request.method, path_label).observe(duration)
    response.headers["X-Request-ID"] = correlation_id
    log_event(
        "http_request",
        method=request.method,
        path=path_label,
        status=response.status_code,
        duration_ms=round(duration * 1000, 2),
    )
    return response


def _wallet_response(wallet) -> WalletResponse:
    return WalletResponse(id=wallet.id, balance_paise=wallet.balance_paise, created_at=wallet.created_at)


def _transfer_response(transfer) -> TransferResponse:
    return TransferResponse(
        id=transfer.id,
        from_wallet=transfer.from_wallet_id,
        to_wallet=transfer.to_wallet_id,
        amount_paise=transfer.amount_paise,
        status=transfer.status,
        decline_reason=transfer.decline_reason,
        created_at=transfer.created_at,
        completed_at=transfer.completed_at,
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/wallets", response_model=WalletResponse)
def create_wallet(
    payload: Optional[WalletCreateRequest] = None,
    token_hash: str = Depends(get_caller_token_hash),
):
    initial_balance = payload.initial_balance_paise if payload else 0
    wallet, _created = get_or_create_wallet(engine, token_hash, initial_balance)
    return _wallet_response(wallet)


@app.get("/wallets/{wallet_id}", response_model=WalletResponse)
def read_wallet(wallet_id: str, token_hash: str = Depends(get_caller_token_hash)):
    wallet = get_wallet(engine, wallet_id)
    if wallet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Wallet not found")
    if wallet.owner_token_hash != token_hash:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not the wallet owner")
    return _wallet_response(wallet)


@app.post(
    "/transfers",
    response_model=TransferResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_transfer_endpoint(
    payload: TransferCreateRequest,
    response: Response,
    token_hash: str = Depends(get_caller_token_hash),
):
    if payload.from_wallet == payload.to_wallet:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="from and to must differ")

    from_wallet = get_wallet(engine, payload.from_wallet)
    if from_wallet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="from wallet not found")
    if from_wallet.owner_token_hash != token_hash:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Caller does not own the source wallet"
        )

    to_wallet = get_wallet(engine, payload.to_wallet)
    if to_wallet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="to wallet not found")

    try:
        transfer, is_replay = create_transfer(
            engine, payload.idempotency_key, payload.from_wallet, payload.to_wallet, payload.amount_paise
        )
    except IdempotencyConflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="idempotency_key already used with a different request body",
        )

    if is_replay:
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotent-Replay"] = "true"
    return _transfer_response(transfer)


@app.get("/transfers/{transfer_id}", response_model=TransferResponse)
def read_transfer(transfer_id: str, token_hash: str = Depends(get_caller_token_hash)):
    transfer = get_transfer(engine, transfer_id)
    if transfer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transfer not found")

    from_wallet = get_wallet(engine, transfer.from_wallet_id)
    to_wallet = get_wallet(engine, transfer.to_wallet_id)
    owner_hashes = {w.owner_token_hash for w in (from_wallet, to_wallet) if w is not None}
    if token_hash not in owner_hashes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to view this transfer"
        )

    return _transfer_response(transfer)
