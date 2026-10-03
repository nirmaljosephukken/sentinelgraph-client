"""Client intake API (api/app.py) with real keys, on the example bank."""
import pytest
from fastapi.testclient import TestClient

from agent import access as A
from api.app import app
from tests.conftest import ADMIN, ROOT, wait_for


@pytest.fixture(scope="module")
def api(example_bank):
    key = A.create_api_key(ADMIN, "tests", "example_bank", ["transactions", "alerts", "outcomes", "read"])
    ro = A.create_api_key(ADMIN, "read only", "example_bank", ["read"])
    return TestClient(app), {"X-API-Key": key}, {"Authorization": f"Bearer {ro}"}


def test_keys_are_required(api):
    c, _, ro = api
    assert c.post("/v1/transactions", content=b"x").status_code == 401
    assert c.post("/v1/transactions", content=b"x", headers={"X-API-Key": "sgk_deadbeef_" + "x" * 30}).status_code == 403
    assert c.post("/v1/transactions", content=b"x", headers=ro).status_code == 403


def test_dry_run_and_errors(api):
    c, h, _ = api
    csv = (ROOT / "samples/example_bank/transactions.csv").read_bytes()
    r = c.post("/v1/transactions?dry_run=true", content=csv[:20000], headers={**h, "Content-Type": "text/csv"})
    assert r.status_code == 200 and r.json()["status"] == "checked"
    r = c.post("/v1/transactions", content=b"TXN_REF,AMOUNT_PAISE\n1,2\n", headers={**h, "Content-Type": "text/csv"})
    assert r.status_code == 422 and "CARD_TOKEN" in r.json()["detail"]
    assert c.post("/v1/transactions", content=b"", headers=h).status_code == 400


def test_alert_reply_result_outcome(api, client_mode):
    c, h, ro = api
    r = c.post("/v1/alerts", json={"records": [{"ALERT_ID": "API-1", "TXN_REF": "TXN400005260", "ALERT_TYPE": "RULE",
                                               "ALERT_TEXT": "", "ALERT_TIME": ""}]}, headers=h)
    assert r.status_code == 202 and r.json()["alert_ids"] == ["API-1"]
    d = wait_for(lambda: (lambda j: j if j.get("status") in ("awaiting_reply", "done") else None)(
        c.get("/v1/alerts/API-1", headers=ro).json()))
    assert d["status"] == "awaiting_reply" and d["mode"] in ("shadow", "assist")
    assert c.post("/v1/alerts/API-1/reply", json={"reply": "maybe"}, headers=h).status_code == 422
    assert c.post("/v1/alerts/API-1/reply", json={"reply": "denied", "channel": "sms"}, headers=h).status_code == 202
    assert c.post("/v1/alerts/API-1/reply", json={"reply": "denied"}, headers=h).status_code == 409
    d = wait_for(lambda: (lambda j: j if j.get("status") == "done" else None)(c.get("/v1/alerts/API-1", headers=ro).json()))
    assert d["verdict"] == "fraud" and d["policy"]["fingerprint"] and d["actions"]
    r = c.post("/v1/outcomes", json=[{"alert_id": "API-1", "outcome": "fraud"}], headers=h)
    assert r.status_code == 200 and r.json()["recorded"] == 1
    assert c.post("/v1/outcomes", json=[{"alert_id": "API-1", "outcome": "fraud"}], headers=ro).status_code == 403


def test_schema_and_health(api):
    c, h, _ = api
    assert len(c.get("/v1/schema", headers=h).json()["transactions"]) == 24
    assert c.get("/v1/health").json()["ok"]
