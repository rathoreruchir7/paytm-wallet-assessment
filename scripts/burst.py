#!/usr/bin/env python3
"""Black-box invariant probes. Python 3 standard library only; no pip install."""

import argparse
import concurrent.futures
import json
import os
import random
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from uuid import uuid4

LOCAL_TOKEN = "local-assessment-admin-token-change-before-deploy"


class Probe:
    def __init__(self, base_url, admin_token, concurrency):
        self.base = base_url.rstrip("/")
        self.admin_token, self.concurrency = admin_token, concurrency
        self.latencies = []

    def request(self, method, path, token=None, body=None):
        headers = {"Content-Type": "application/json", "X-Correlation-ID": "burst-" + str(uuid4())}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(
            self.base + path,
            method=method,
            headers=headers,
            data=json.dumps(body).encode() if body is not None else None,
        )
        start = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=45) as response:
                result = response.status, response.read()
        except urllib.error.HTTPError as error:
            result = error.code, error.read()
        self.latencies.append((time.perf_counter() - start) * 1000)
        try:
            return result[0], json.loads(result[1])
        except ValueError:
            raise AssertionError(
                f"Non-JSON HTTP {result[0]} from {path}; wait for the service to wake"
            ) from None

    def ok(self, method, path, token=None, body=None):
        status, value = self.request(method, path, token, body)
        assert status == 200, f"{method} {path}: HTTP {status}: {value}"
        return value

    def burst(self, calls):
        gate = threading.Event()

        def invoke(call):
            gate.wait()
            return call()

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.concurrency) as executor:
            futures = [executor.submit(invoke, call) for call in calls]
            gate.set()
            return [future.result() for future in futures]

    def balances(self, users):
        return {
            u["wallet_id"]: self.ok("GET", "/wallets/" + u["wallet_id"], u["token"])["balance_paise"]
            for u in users
        }

    def fixtures(self, count=4, initial=10_000):
        return self.ok(
            "POST", "/admin/fixtures", self.admin_token, {"count": count, "initial_balance_paise": initial}
        )

    def wallet_race(self, n):
        fresh = self.fixtures()["fresh_user"]
        results = self.burst([lambda: self.ok("POST", "/wallets", fresh["token"]) for _ in range(n)])
        assert len({r["id"] for r in results}) == 1, "Duplicate wallets for one user"
        assert all(r == results[0] and r["balance_paise"] == 0 for r in results)
        return {"case": "concurrent_get_or_create", "requests": n, "unique_wallets": 1, "passed": True}

    def storm(self, k):
        users = self.fixtures(2)["users"]
        a, b = users
        before = self.balances(users)
        body = {
            "from": a["wallet_id"],
            "to": b["wallet_id"],
            "amount_paise": 137,
            "idempotency_key": "storm-" + str(uuid4()),
        }
        results = self.burst([lambda: self.ok("POST", "/transfers", a["token"], body) for _ in range(k)])
        assert all(r == results[0] for r in results), "Replay result changed"
        assert results[0]["status"] == "succeeded"
        after = self.balances(users)
        assert after[a["wallet_id"]] == before[a["wallet_id"]] - 137
        assert after[b["wallet_id"]] == before[b["wallet_id"]] + 137
        status, _ = self.request("POST", "/transfers", a["token"], {**body, "amount_paise": 138})
        assert status == 409, "Same key with different body must conflict"
        assert self.balances(users) == after
        assert self.ok("GET", "/transfers/" + results[0]["id"], a["token"]) == results[0]
        return {
            "case": "idempotent_retry_storm",
            "requests": k,
            "unique_transfers": 1,
            "debit_paise": 137,
            "credit_paise": 137,
            "different_body_status": 409,
            "passed": True,
        }

    def contention(self, n):
        users = self.fixtures()["users"]
        before = self.balances(users)
        rng, calls = random.Random(2026), []
        for i in range(n):
            # Explicit alternating opposite-direction pairs plus random contention.
            a, b = (
                (users[0], users[1])
                if i % 4 == 0
                else ((users[1], users[0]) if i % 4 == 1 else rng.sample(users, 2))
            )
            body = {
                "from": a["wallet_id"],
                "to": b["wallet_id"],
                "amount_paise": rng.randint(1, 100),
                "idempotency_key": "contention-" + str(uuid4()),
            }
            calls.append(lambda a=a, body=body: self.ok("POST", "/transfers", a["token"], body))
        results = self.burst(calls)
        expected = dict(before)
        succeeded, declined = 0, 0
        for row in results:
            assert row["status"] in ("succeeded", "declined")
            if row["status"] == "succeeded":
                succeeded += 1
                expected[row["from"]] -= row["amount_paise"]
                expected[row["to"]] += row["amount_paise"]
            else:
                declined += 1
        after = self.balances(users)
        assert succeeded > 0, "Workload must actually move money"
        assert len({r["id"] for r in results}) == n
        assert sum(before.values()) == sum(after.values()), "Conservation violated"
        assert min(after.values()) >= 0, "Negative wallet balance"
        assert after == expected, "Balances do not reconcile with successful receipts"
        return {
            "case": "conservation_under_contention",
            "requests": n,
            "succeeded": succeeded,
            "declined": declined,
            "before_paise": sum(before.values()),
            "after_paise": sum(after.values()),
            "min_balance_paise": min(after.values()),
            "receipts_reconciled": True,
            "passed": True,
        }

    def overdraft(self, n):
        users = self.fixtures(2, initial=1000)["users"]
        a, b = users
        calls = []
        for _ in range(n):
            body = {
                "from": a["wallet_id"],
                "to": b["wallet_id"],
                "amount_paise": 600,
                "idempotency_key": "overdraft-" + str(uuid4()),
            }
            calls.append(lambda body=body: self.ok("POST", "/transfers", a["token"], body))
        results = self.burst(calls)
        assert sum(r["status"] == "succeeded" for r in results) == 1
        assert sum(r["reason"] == "insufficient_funds" for r in results) == n - 1
        after = self.balances(users)
        assert after == {a["wallet_id"]: 400, b["wallet_id"]: 1600}
        return {
            "case": "concurrent_overdraft",
            "requests": n,
            "succeeded": 1,
            "declined": n - 1,
            "source_balance_paise": 400,
            "passed": True,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.getenv("BASE_URL", "http://localhost:8000"))
    parser.add_argument(
        "--case", choices=["all", "wallets", "idempotency", "contention", "overdraft"], default="all"
    )
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument(
        "--requests", type=int, default=50, help="Wallet race, storm and overdraft request counts"
    )
    parser.add_argument("--transfers", type=int, default=400)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.concurrency < 1 or args.requests < 2 or args.transfers < 2:
        parser.error("Use concurrency >= 1 and request/transfer counts >= 2")
    token = os.getenv("ADMIN_TOKEN")
    if not token and urllib.parse.urlparse(args.base_url).hostname in ("localhost", "127.0.0.1"):
        token = LOCAL_TOKEN
    if not token:
        parser.error("Set ADMIN_TOKEN in the environment for a deployed service")
    probe = Probe(args.base_url, token, args.concurrency)
    probe.ok("GET", "/readyz")
    report = {"target": args.base_url, "concurrency": args.concurrency, "cases": []}
    cases = [
        ("wallets", lambda: probe.wallet_race(args.requests)),
        ("idempotency", lambda: probe.storm(args.requests)),
        ("contention", lambda: probe.contention(args.transfers)),
        ("overdraft", lambda: probe.overdraft(args.requests)),
    ]
    for name, run in cases:
        if args.case in ("all", name):
            result = run()
            report["cases"].append(result)
            print(json.dumps(result), flush=True)
    values = sorted(probe.latencies)
    report["client_latency_ms"] = {
        "p50": round(statistics.median(values), 2),
        "p99": round(values[min(len(values) - 1, int(0.99 * len(values)))], 2),
    }
    report["passed"] = True
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"all_selected_probes_passed": True, **report["client_latency_ms"]}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, urllib.error.URLError, TimeoutError) as exc:
        print(json.dumps({"passed": False, "error": str(exc)}), file=sys.stderr)
        sys.exit(1)
