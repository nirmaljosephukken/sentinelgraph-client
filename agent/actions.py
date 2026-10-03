"""
Hand actions to the bank's own systems.

SentinelGraph never touches a card directly. In assist mode, each action that is cleared to happen (an auto action
when a case finishes, or an L1/L2 action when a person approves it) is POSTed as JSON to ACTION_WEBHOOK_URL, where
the bank's card-management or case system carries it out. In shadow mode nothing is sent.

Each request carries:
    X-SentinelGraph-Event-Id    stable per alert and action, so the receiver can ignore repeats
    X-SentinelGraph-Signature   sha256=<HMAC-SHA256 of the body with ACTION_WEBHOOK_SECRET>
Every attempt and its result is recorded (Firestore collection <prefix>-action_events, or data/access locally).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone

from agent import access as A
from agent.policy_config import POLICY


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def dispatch(event: dict, attempts: int = 3) -> dict:
    """Send one action event. Returns the stored record (status sent | failed | not_sent | shadow)."""
    eid = event["event_id"]
    rec = {**event, "created_at": _now(), "attempts": 0}
    url, secret = os.getenv("ACTION_WEBHOOK_URL", ""), os.getenv("ACTION_WEBHOOK_SECRET", "")
    done = A.get_doc("action_events", eid)
    if done and done.get("status") == "sent":
        return done                                   # already delivered: never send an action twice
    if POLICY.shadow:
        rec.update(status="shadow", detail="shadow mode: recorded, not sent")
    elif not url:
        rec.update(status="not_sent", detail="no ACTION_WEBHOOK_URL configured; carry the action out by hand")
    else:
        import requests
        body = json.dumps(event, sort_keys=True, default=str).encode()
        headers = {"Content-Type": "application/json", "X-SentinelGraph-Event-Id": eid,
                   "X-SentinelGraph-Signature": sign(body, secret) if secret else "unsigned"}
        for i in range(attempts):
            rec["attempts"] = i + 1
            try:
                r = requests.post(url, data=body, headers=headers, timeout=10)
                if r.status_code < 300:
                    rec.update(status="sent", detail=f"HTTP {r.status_code}", sent_at=_now())
                    break
                rec.update(status="failed", detail=f"HTTP {r.status_code}: {r.text[:200]}")
                if 400 <= r.status_code < 500 and r.status_code != 429:
                    break                              # the receiver refused it; retrying will not help
            except Exception as ex:  # noqa: BLE001
                rec.update(status="failed", detail=f"{type(ex).__name__}: {str(ex)[:200]}")
            time.sleep(1.5 * (i + 1))
        if rec["status"] == "failed":
            from agent import obs
            obs.event("action_send_failed", "ERROR", event_id=eid, detail=rec["detail"], alert_id=event.get("alert_id"))
            A.audit("system:webhook", "system", "action_send_failed", event.get("alert_id", ""),
                    {"event_id": eid, "detail": rec["detail"]})
    A.put_doc("action_events", eid, {k: (json.dumps(v, default=str) if isinstance(v, (dict, list)) else v)
                                     for k, v in rec.items()})
    return rec


def _event(kind: str, alert_id: str, answer: dict, action: dict, card_id: str = "", **extra) -> dict:
    c = answer.get("case", {})
    return {"event_id": f"{alert_id}:{action['action']}", "type": kind, "alert_id": alert_id,
            "case_id": c.get("graph_case_id"), "action": action["action"], "route": action.get("route"),
            "reason": action.get("reason"), "card_id": card_id,
            "affected_txn_ids": c.get("affected_txn_ids", []), "exposure": c.get("exposure_usd"),
            "verdict": c.get("verdict"), "policy": f"{POLICY.label} ({POLICY.fingerprint})", "at": _now(), **extra}


def after_investigation(alert: dict, answer: dict) -> list[dict]:
    """Auto actions of a finished case."""
    return [dispatch(_event("action.executed", alert["case_id"], answer, a, card_id=alert.get("card_id", "")))
            for a in answer["next_best_actions"]["final"] if a.get("route") == "auto"]


def after_decision(alert_id: str, answer: dict, action: dict, decision: str, actor: dict,
                   card_id: str = "") -> dict | None:
    """An L1/L2 action a person approved. Rejections and shadow agree/disagree are recorded, never sent."""
    if decision != "approved":
        return None
    return dispatch(_event("action.approved", alert_id, answer, action, card_id=card_id, approved_by=actor.get("email"),
                           approver_role=actor.get("role")))


def recent(limit: int = 200) -> list[dict]:
    return sorted(A.list_docs("action_events"), key=lambda d: d.get("created_at", ""), reverse=True)[:limit]
