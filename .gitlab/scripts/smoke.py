#!/usr/bin/env python3
"""Post-deploy smoke test for SentinelGraph on Cloud Run.

    python .gitlab/scripts/smoke.py <console-url> [<api-url>]

Checks the console's Streamlit health endpoint, the intake API's health and OpenAPI document, and p95 latency.
Exits non-zero on any failure so CI can roll back or open an incident. Standard library only.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

console = (sys.argv[1] if len(sys.argv) > 1 else "").rstrip("/")
api = (sys.argv[2] if len(sys.argv) > 2 else "").rstrip("/")
if not console:
    print("smoke: no console URL", file=sys.stderr)
    sys.exit(2)

failed = 0


def get(url: str, timeout: float = 15.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "sentinelgraph-smoke/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def check(name, fn):
    global failed
    t0 = time.time()
    try:
        fn()
        print(f"PASS {name} ({int((time.time() - t0) * 1000)} ms)")
    except Exception as e:  # noqa: BLE001
        failed = 1
        print(f"FAIL {name}: {e}")


def console_health():
    code, body = get(f"{console}/_stcore/health")
    assert code == 200 and body.strip() == b"ok", f"status={code} body={body[:80]!r}"


def console_page():
    code, body = get(console, timeout=30)
    assert code == 200 and b"<html" in body.lower(), f"status={code}"


def api_health():
    code, body = get(f"{api}/v1/health")
    assert code == 200, f"status={code} body={body[:120]!r}"
    data = json.loads(body)
    assert data.get("ok") is True or data.get("status") in ("ok", "healthy"), data


def api_openapi():
    code, body = get(f"{api}/openapi.json")
    assert code == 200, f"status={code}"
    paths = json.loads(body).get("paths", {})
    for p in ("/v1/transactions", "/v1/alerts", "/v1/outcomes"):
        assert p in paths, f"missing {p}"


def api_rejects_anonymous():
    code, _ = get(f"{api}/v1/schema")
    assert code in (401, 403), f"expected 401/403 without a key, got {code}"


def latency():
    samples = []
    for _ in range(8):
        t = time.time()
        get(f"{console}/_stcore/health")
        samples.append(time.time() - t)
    samples.sort()
    p95 = samples[max(0, int(len(samples) * 0.95) - 1)]
    assert p95 < 1.5, f"p95={p95:.2f}s"


check("console health", console_health)
check("console page renders", console_page)
if api:
    check("api health", api_health)
    check("api openapi lists intake routes", api_openapi)
    check("api refuses anonymous calls", api_rejects_anonymous)
check("console p95 latency < 1.5 s", latency)

print("SMOKE FAILED" if failed else "SMOKE OK")
sys.exit(failed)
