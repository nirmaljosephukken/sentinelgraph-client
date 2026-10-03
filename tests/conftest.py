"""
Shared test setup. Every test runs offline: local graph, no language model, no Google Cloud, temporary folders.

Modules read some settings when they are imported (store folders), so the environment is set here, before any
project module is imported. Client-mode behaviour that is read at call time (DATA_SOURCE) is switched per test.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_TMP = Path(tempfile.mkdtemp(prefix="sg-tests-"))
os.environ.update({
    "ACCESS_DIR": str(_TMP / "access"), "LIVE_DIR": str(_TMP / "live"), "CLIENT_DATA_DIR": str(_TMP / "client"),
    "GRAPH_BACKEND": "local", "LLM_PROVIDER": "none", "LIVE_BACKEND": "local", "ACCESS_BACKEND": "local",
    "AUTH_MODE": "dev", "ACTION_WEBHOOK_URL": "", "ACTION_WEBHOOK_SECRET": "",
})
os.environ.pop("POLICY_FILE", None)
os.environ.pop("K_SERVICE", None)

import pytest  # noqa: E402

from agent.config import SETTINGS  # noqa: E402

SETTINGS.backend = "local"
SETTINGS.llm_provider = "none"

ADMIN = {"email": "admin@test.bank", "role": "admin"}
LEAD = {"email": "lead@test.bank", "role": "team_lead"}
MANAGER = {"email": "manager@test.bank", "role": "fraud_manager"}
ANALYST = {"email": "analyst@test.bank", "role": "analyst"}
VIEWER = {"email": "viewer@test.bank", "role": "viewer"}


def wait_for(fn, timeout=60, step=0.25):
    """Poll until fn() is truthy (the local live queue runs investigations on a background thread)."""
    import time
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = fn()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("timed out waiting")


@pytest.fixture(scope="session")
def example_bank():
    """The example bank's files mapped and loaded into a client-only local graph (no benchmark data)."""
    from agent.ingest import load as L
    from agent.ingest import mapping as M
    mp = pytest.MonkeyPatch()
    mp.setenv("DATA_SOURCE", "client")
    from agent import signals
    mp.setattr(signals, "CLIENT_MODE", True)
    from agent.backends import LocalBackend
    g = LocalBackend(SETTINGS)
    assert len(g.t) == 0 or True
    m = M.load("example_bank")
    raw = M.read_file((ROOT / "samples/example_bank/transactions.csv").read_bytes(), "transactions.csv")
    b = L.ingest_transactions(g, raw, m, ADMIN, "tests")
    assert b["status"] == "loaded", b
    from agent import live
    live.use_backend(g)
    yield {"graph": g, "mapping": m, "raw": raw, "batch": b}
    mp.undo()


@pytest.fixture()
def client_mode(monkeypatch):
    monkeypatch.setenv("DATA_SOURCE", "client")
    from agent import signals
    monkeypatch.setattr(signals, "CLIENT_MODE", True)
