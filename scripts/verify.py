#!/usr/bin/env python3
"""Build, inspect, exercise, and restart the actual Docker deployment."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from burst import LOCAL_TOKEN, Probe

ROOT = Path(__file__).resolve().parents[1]


def run(*args, capture=False):
    result = subprocess.run(args, cwd=ROOT, check=True, text=True, capture_output=capture)
    return result.stdout.strip() if capture else None


def ready(probe):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            if probe.request("GET", "/readyz")[0] == 200:
                return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("App never became ready")


def main():
    run("docker", "compose", "up", "--build", "-d", "--wait")
    container = run("docker", "compose", "ps", "-q", "app", capture=True)
    image = run(
        "docker", "container", "inspect",
        "--format", "{{.Config.Image}}", container, capture=True,
    )
    details = json.loads(run("docker", "image", "inspect", image, capture=True))[0]
    assert details["Config"]["User"] == "10001:10001", "Image must run as non-root"
    assert details["Config"]["Healthcheck"]["Test"], "Image must define HEALTHCHECK"
    run("docker", "compose", "--profile", "test", "run", "--build", "--rm", "test")
    run(sys.executable, "scripts/burst.py", "--report", "evidence/burst.json")
    probe = Probe("http://localhost:8000", os.getenv("ADMIN_TOKEN", LOCAL_TOKEN), 2)
    users = probe.fixtures(2)["users"]
    a, b = users
    from uuid import uuid4

    payload = {
        "from": a["wallet_id"],
        "to": b["wallet_id"],
        "amount_paise": 321,
        "idempotency_key": "restart-" + str(uuid4()),
    }
    original = probe.ok("POST", "/transfers", a["token"], payload)
    balances = probe.balances(users)
    run("docker", "compose", "restart", "app")
    ready(probe)
    replay = probe.ok("POST", "/transfers", a["token"], payload)
    assert replay == original and probe.balances(users) == balances
    events = probe.ok("GET", "/events")["records"]
    assert len([e for e in events if e["transfer_id"] == original["id"]]) == 3
    print(
        json.dumps(
            {
                "container_non_root": True,
                "healthcheck_present": True,
                "restart_idempotency": "passed",
                "durable_events": "passed",
            }
        )
    )


if __name__ == "__main__":
    main()
