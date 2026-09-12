#!/usr/bin/env python3
"""One-command burst test for the wallet service's grading invariants.

Usage:
    pip install -r scripts/requirements.txt
    python scripts/burst_test.py --base-url https://your-deployed-url --scenario all
"""
import argparse
import concurrent.futures
import secrets
import sys
import uuid

import requests


def new_token() -> str:
    return "test-" + uuid.uuid4().hex


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def create_wallet(base_url, token, initial_balance_paise=0):
    r = requests.post(
        f"{base_url}/wallets",
        json={"initial_balance_paise": initial_balance_paise},
        headers=auth_headers(token),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def get_wallet(base_url, token, wallet_id):
    r = requests.get(f"{base_url}/wallets/{wallet_id}", headers=auth_headers(token), timeout=30)
    r.raise_for_status()
    return r.json()


def post_transfer(base_url, token, from_id, to_id, amount, idem_key):
    body = {"from": from_id, "to": to_id, "amount_paise": amount, "idempotency_key": idem_key}
    return requests.post(f"{base_url}/transfers", json=body, headers=auth_headers(token), timeout=30)


def scenario_concurrent_get_or_create(base_url, n=20):
    print(f"\n[1] Concurrent get-or-create: {n} simultaneous POST /wallets for a brand-new user")
    token = new_token()
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(create_wallet, base_url, token) for _ in range(n)]
        results = [f.result() for f in futures]
    ids = {r["id"] for r in results}
    ok = len(ids) == 1
    print(f"    distinct wallet ids returned: {len(ids)} -> {'PASS' if ok else 'FAIL'}")
    if not ok:
        sys.exit(1)


def scenario_idempotent_retry_storm(base_url, k=20):
    print(f"\n[2] Idempotent retry storm: the same transfer fired {k} times concurrently")
    token_a, token_b = new_token(), new_token()
    starting_balance = 100_000
    amount = 2_500
    wallet_a = create_wallet(base_url, token_a, initial_balance_paise=starting_balance)
    wallet_b = create_wallet(base_url, token_b, initial_balance_paise=0)
    idem_key = uuid.uuid4().hex

    with concurrent.futures.ThreadPoolExecutor(max_workers=k) as pool:
        futures = [
            pool.submit(post_transfer, base_url, token_a, wallet_a["id"], wallet_b["id"], amount, idem_key)
            for _ in range(k)
        ]
        responses = [f.result() for f in futures]

    bodies = [r.json() for r in responses]
    transfer_ids = {b["id"] for b in bodies}
    identical_bodies = all(b == bodies[0] for b in bodies)
    ok = len(transfer_ids) == 1 and identical_bodies
    print(f"    distinct transfer ids: {len(transfer_ids)}, identical response bodies: {identical_bodies} -> {'PASS' if ok else 'FAIL'}")

    balance_a = get_wallet(base_url, token_a, wallet_a["id"])["balance_paise"]
    balance_b = get_wallet(base_url, token_b, wallet_b["id"])["balance_paise"]
    expected_a, expected_b = starting_balance - amount, amount
    ok_balance = balance_a == expected_a and balance_b == expected_b
    print(f"    balance_a={balance_a} (expected {expected_a}), balance_b={balance_b} (expected {expected_b}) -> {'PASS' if ok_balance else 'FAIL'}")

    if not (ok and ok_balance):
        sys.exit(1)


def scenario_conservation_under_contention(base_url, n_wallets=4, n_transfers=100):
    print(f"\n[3] Conservation under contention: {n_transfers} concurrent transfers among {n_wallets} wallets")
    tokens = [new_token() for _ in range(n_wallets)]
    starting_balance = 1_000_000
    wallets_info = [create_wallet(base_url, t, initial_balance_paise=starting_balance) for t in tokens]
    wallet_ids = [w["id"] for w in wallets_info]
    total_before = starting_balance * n_wallets

    jobs = []
    rng = secrets.SystemRandom()
    for _ in range(n_transfers):
        a, b = rng.sample(range(n_wallets), 2)
        amount = rng.randrange(1, 50_000)
        jobs.append((tokens[a], wallet_ids[a], wallet_ids[b], amount, uuid.uuid4().hex))

    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        futures = [pool.submit(post_transfer, base_url, *job) for job in jobs]
        responses = [f.result() for f in futures]

    completed = sum(1 for r in responses if r.ok and r.json().get("status") == "completed")
    declined = sum(1 for r in responses if r.ok and r.json().get("status") == "declined")
    print(f"    {completed} completed, {declined} declined (insufficient funds)")

    balances = [get_wallet(base_url, tok, wid)["balance_paise"] for tok, wid in zip(tokens, wallet_ids)]
    total_after = sum(balances)
    no_negative = all(b >= 0 for b in balances)
    conserved = total_after == total_before
    print(f"    total_before={total_before}, total_after={total_after} -> {'PASS' if conserved else 'FAIL'}")
    print(f"    no negative balances: {no_negative} -> {'PASS' if no_negative else 'FAIL'}")

    if not (conserved and no_negative):
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument(
        "--scenario", default="all", choices=["all", "get_or_create", "idempotent", "conservation"]
    )
    parser.add_argument("--n", type=int, default=20, help="concurrency for each scenario")
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")

    if args.scenario in ("all", "get_or_create"):
        scenario_concurrent_get_or_create(base_url, n=args.n)
    if args.scenario in ("all", "idempotent"):
        scenario_idempotent_retry_storm(base_url, k=args.n)
    if args.scenario in ("all", "conservation"):
        scenario_conservation_under_contention(base_url, n_transfers=max(args.n * 5, 100))

    print("\nAll scenarios PASSED")


if __name__ == "__main__":
    main()
