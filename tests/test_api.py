"""Client intake API (api/app.py) with real keys, on the example bank."""
import dataclasses
import json

import pytest
from fastapi.testclient import TestClient

from agent import access as A
from agent.policy_config import POLICY
from api.app import app
from tests.conftest import ADMIN, ROOT, wait_for

TXN = "TXN400005260"   # an example-bank transaction the engine wants the cardholder's word on before deciding


@pytest.fixture(scope="module")
def api(example_bank):
    key = A.create_api_key(ADMIN, "tests", "example_bank", ["transactions", "alerts", "outcomes", "read"])
    ro = A.create_api_key(ADMIN, "read only", "example_bank", ["read"])
    return TestClient(app), {"X-API-Key": key}, {"Authorization": f"Bearer {ro}"}


def _submit(api, alert_id: str) -> dict:
    """Submits one alert and returns its read-back once the engine is waiting for the cardholder."""
    c, h, ro = api
    r = c.post("/v1/alerts", json={"records": [{"ALERT_ID": alert_id, "TXN_REF": TXN, "ALERT_TYPE": "RULE",
                                               "ALERT_TEXT": "", "ALERT_TIME": ""}]}, headers=h)
    assert r.status_code == 202 and r.json()["alert_ids"] == [alert_id]
    d = wait_for(lambda: (lambda j: j if j.get("status") in ("awaiting_reply", "done") else None)(
        c.get(f"/v1/alerts/{alert_id}", headers=ro).json()))
    assert d["status"] == "awaiting_reply"
    return d


def _finish(api, alert_id: str) -> dict:
    """Answers for the cardholder and returns the read-back once the case is done."""
    c, h, ro = api
    assert c.post(f"/v1/alerts/{alert_id}/reply", json={"reply": "denied", "channel": "sms"}, headers=h).status_code == 202
    return wait_for(lambda: (lambda j: j if j.get("status") == "done" else None)(
        c.get(f"/v1/alerts/{alert_id}", headers=ro).json()))


def _activate_other_policy(monkeypatch):
    """Makes the API's ACTIVE policy visibly different from the one the investigations ran under."""
    from api import app as api_module
    other = dataclasses.replace(api_module.POLICY, version="99.9", fingerprint="deadbeef")
    monkeypatch.setattr(api_module, "POLICY", other)
    return other


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
    d = _submit(api, "API-1")
    assert d["mode"] in ("shadow", "assist")
    assert d["policy"] == POLICY.summary() and d["policy_source"] == "active"
    assert c.post("/v1/alerts/API-1/reply", json={"reply": "maybe"}, headers=h).status_code == 422
    d = _finish(api, "API-1")
    assert c.post("/v1/alerts/API-1/reply", json={"reply": "denied"}, headers=h).status_code == 409
    assert d["verdict"] == "fraud" and d["actions"]
    assert d["policy"]["fingerprint"] == POLICY.fingerprint and d["policy_source"] == "case_record"
    r = c.post("/v1/outcomes", json=[{"alert_id": "API-1", "outcome": "fraud"}], headers=h)
    assert r.status_code == 200 and r.json()["recorded"] == 1
    assert c.post("/v1/outcomes", json=[{"alert_id": "API-1", "outcome": "fraud"}], headers=ro).status_code == 403


def test_waiting_case_reports_the_active_policy_as_provisional(api, client_mode, monkeypatch):
    """While the cardholder is being asked, the read-back carries today's policy and says so: the case will be
    re-investigated under whatever policy is active when the answer arrives."""
    c, _, ro = api
    _submit(api, "API-2")
    other = _activate_other_policy(monkeypatch)
    d = c.get("/v1/alerts/API-2", headers=ro).json()
    assert d["status"] == "awaiting_reply"
    assert d["policy_source"] == "active" and d["policy"] == other.summary()
    assert d["policy"]["fingerprint"] == "deadbeef"


def test_done_case_reports_its_own_policy_not_the_active_one(api, client_mode, monkeypatch):
    """A finished decision is attributed to the policy that produced it, even when the active policy differs."""
    c, _, ro = api
    _submit(api, "API-3")
    done = _finish(api, "API-3")
    recorded = done["policy"]
    assert recorded["fingerprint"] == POLICY.fingerprint
    _activate_other_policy(monkeypatch)
    d = c.get("/v1/alerts/API-3", headers=ro).json()
    assert d["status"] == "done" and d["policy_source"] == "case_record"
    assert d["policy"] == recorded and d["policy"]["fingerprint"] != "deadbeef"


def test_finished_case_without_recorded_policy_does_not_claim_the_active_one(api, client_mode):
    """An old case whose record carries no policy - or no answer at all - must not be dressed up with today's."""
    from agent import live
    c, _, ro = api
    store = live.store()
    _submit(api, "API-4")
    _finish(api, "API-4")
    doc = dict(store.get("API-4"))
    ans = json.loads(doc["answer"])
    ans.pop("policy", None)
    doc.update(alert_id="API-OLD", answer=json.dumps(ans))
    store.put("API-OLD", doc)
    d = c.get("/v1/alerts/API-OLD", headers=ro).json()
    assert d["policy_source"] == "case_record" and d["policy"] is None
    store.put("API-BARE", {"alert_id": "API-BARE", "status": "done", "verdict": "fraud", "received_at": "2026-01-01T00:00:00Z"})
    d = c.get("/v1/alerts/API-BARE", headers=ro).json()
    assert d["policy_source"] == "case_record" and d["policy"] is None and "actions" not in d


def test_resuming_case_reports_the_active_policy(api, client_mode, monkeypatch):
    """Between the cardholder's answer and the re-run the case is still provisional: active policy, marked so."""
    from agent import live
    c, h, ro = api
    store = live.store()
    _submit(api, "API-5")
    store.put("API-RESUMING", {**store.get("API-5"), "alert_id": "API-RESUMING", "status": "resuming"})
    other = _activate_other_policy(monkeypatch)
    d = c.get("/v1/alerts/API-RESUMING", headers=ro).json()
    assert d["status"] == "resuming" and d["policy_source"] == "active" and d["policy"] == other.summary()


def test_errored_case_policy_attribution(api, client_mode):
    """A failed resume leaves the first-pass answer in the doc; no decision was produced under it, so the read-back
    reports no policy rather than attributing the stale first pass."""
    from agent import live
    c, _, ro = api
    store = live.store()
    _submit(api, "API-6")
    doc = dict(store.get("API-6"))
    ans = json.loads(doc["answer"])
    ans["policy"] = POLICY.summary()   # the first pass recorded a policy; it must still not be reported
    store.put("API-ERR", {**doc, "alert_id": "API-ERR", "answer": json.dumps(ans), "status": "error",
                          "error": "RuntimeError: graph unavailable"})
    d = c.get("/v1/alerts/API-ERR", headers=ro).json()
    assert d["status"] == "error" and d["error"] and d["policy_source"] == "case_record" and d["policy"] is None


def test_schema_and_health(api):
    c, h, _ = api
    assert len(c.get("/v1/schema", headers=h).json()["transactions"]) == 24
    assert c.get("/v1/health").json()["ok"]
