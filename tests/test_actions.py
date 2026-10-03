"""Handing actions to the bank's systems (agent/actions.py): signature, retries, no double send, shadow mode."""
import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agent import actions as ACT
from agent import policy_config


@pytest.fixture()
def receiver(monkeypatch):
    got, fail_first = [], {"n": 1}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
            got.append((self.headers["X-SentinelGraph-Event-Id"], hmac.compare_digest(sig, self.headers["X-SentinelGraph-Signature"]),
                        json.loads(body)))
            code = 503 if fail_first["n"] else 200
            fail_first["n"] = max(0, fail_first["n"] - 1)
            self.send_response(code)
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("ACTION_WEBHOOK_URL", f"http://127.0.0.1:{srv.server_address[1]}/actions")
    monkeypatch.setenv("ACTION_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setattr(ACT.time, "sleep", lambda s: None)
    yield got
    srv.shutdown()


def _assist(monkeypatch):
    import dataclasses
    monkeypatch.setattr(ACT, "POLICY", dataclasses.replace(policy_config.POLICY, mode="assist"))


def test_assist_mode_sends_signed_retries_and_never_twice(receiver, monkeypatch):
    _assist(monkeypatch)
    ev = {"event_id": "A-1:BLOCK_CARD", "type": "action.approved", "alert_id": "A-1", "action": "BLOCK_CARD"}
    r = ACT.dispatch(ev)
    assert r["status"] == "sent" and r["attempts"] == 2              # first try got 503
    assert all(ok for _, ok, _ in receiver) and receiver[-1][0] == "A-1:BLOCK_CARD"
    n = len(receiver)
    assert ACT.dispatch(ev)["status"] == "sent" and len(receiver) == n  # already delivered: not sent again


def test_shadow_mode_sends_nothing(receiver, monkeypatch):
    import dataclasses
    monkeypatch.setattr(ACT, "POLICY", dataclasses.replace(policy_config.POLICY, mode="shadow"))
    r = ACT.dispatch({"event_id": "S-1:MONITOR_CARD", "type": "action.executed", "alert_id": "S-1", "action": "MONITOR_CARD"})
    assert r["status"] == "shadow" and receiver == []


def test_unreachable_receiver_is_recorded(monkeypatch):
    _assist(monkeypatch)
    monkeypatch.setenv("ACTION_WEBHOOK_URL", "http://127.0.0.1:9/nowhere")
    monkeypatch.setattr(ACT.time, "sleep", lambda s: None)
    r = ACT.dispatch({"event_id": "U-1:X", "type": "action.executed", "alert_id": "U-1", "action": "X"}, attempts=2)
    assert r["status"] == "failed" and r["attempts"] == 2


def test_rejected_and_shadow_decisions_are_never_sent(monkeypatch):
    _assist(monkeypatch)
    assert ACT.after_decision("A-2", {"case": {}}, {"action": "BLOCK_CARD", "route": "L1"}, "rejected", {}) is None
    assert ACT.after_decision("A-2", {"case": {}}, {"action": "BLOCK_CARD", "route": "L1"}, "agreed", {}) is None
