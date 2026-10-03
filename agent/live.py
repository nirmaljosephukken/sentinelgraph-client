"""
Live alert stream: alerts in, investigated cases out.

    producer (dispute intake page, bank feed replay)  --publish-->  Pub/Sub topic `fraud-alerts`
    Pub/Sub push subscription  -->  Cloud Run worker (worker/app.py)  -->  process(): run the investigation
    every stage is written to Firestore (`live_alerts`), which the console's Live queue page reads

LIVE_BACKEND=gcp   Pub/Sub + Firestore (Cloud Run)
LIVE_BACKEND=local (default) an in-process thread + JSON files in data/live/, same code path, no cloud needed
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent.config import ROOT, SETTINGS

MODE = os.getenv("LIVE_BACKEND", "local").lower()
TOPIC = os.getenv("PUBSUB_TOPIC", "fraud-alerts")
COLLECTION = os.getenv("FIRESTORE_COLLECTION", "live_alerts")
LOCAL_DIR = Path(os.getenv("LIVE_DIR", ROOT / "data" / "live"))


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------------------------- stores
class LocalStore:
    def __init__(self, d: Path = LOCAL_DIR):
        self.d = d
        self.d.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def put(self, alert_id: str, fields: dict) -> None:
        with self._lock:
            p = self.d / f"{alert_id}.json"
            doc = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
            doc.update(fields)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, default=str), encoding="utf-8")
            os.replace(tmp, p)   # atomic: a reader never sees a half-written file

    def get(self, alert_id: str) -> dict | None:
        p = self.d / f"{alert_id}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def recent(self, limit: int = 30) -> list[dict]:
        docs = []
        for p in self.d.glob("*.json"):
            try:
                docs.append(json.loads(p.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                continue
        return sorted(docs, key=lambda d: d.get("received_at", ""), reverse=True)[:limit]

    def by_status(self, status: str) -> list[dict]:
        return [d for d in self.recent(100000) if d.get("status") == status]


class FirestoreStore:
    def __init__(self):
        from google.cloud import firestore
        self.db = firestore.Client(project=SETTINGS.gcp_project or None)
        self.col = self.db.collection(COLLECTION)

    def put(self, alert_id: str, fields: dict) -> None:
        # answers and traces are stored as JSON text: compact, and free of Firestore's nesting limits
        self.col.document(alert_id).set(fields, merge=True)

    def get(self, alert_id: str) -> dict | None:
        s = self.col.document(alert_id).get()
        return s.to_dict() if s.exists else None

    def recent(self, limit: int = 30) -> list[dict]:
        from google.cloud import firestore
        q = self.col.order_by("received_at", direction=firestore.Query.DESCENDING).limit(limit)
        return [d.to_dict() for d in q.stream()]

    def by_status(self, status: str) -> list[dict]:
        from google.cloud.firestore_v1.base_query import FieldFilter
        return [d.to_dict() for d in self.col.where(filter=FieldFilter("status", "==", status)).stream()]


_store = None


def store():
    global _store
    if _store is None:
        _store = FirestoreStore() if MODE == "gcp" else LocalStore()
    return _store


# ---------------------------------------------------------------------------------------------- publish
def publish(alert: dict, source: str) -> str:
    """Queue an alert. Returns its id. The worker picks it up from Pub/Sub (or a local thread)."""
    aid = alert["case_id"]
    store().put(aid, {"alert_id": aid, "status": "queued", "source": source, "received_at": now(),
                      "alert": json.dumps(alert), "case_id": aid, "txn_id": alert["flagged_txn_id"],
                      "card_id": alert["card_id"], "trigger": alert["trigger_type"],
                      "trigger_text": alert["trigger_text"][:300],
                      "flagged": bool((alert.get("safety") or {}).get("flagged"))})
    if MODE == "gcp":
        from google.cloud import pubsub_v1
        pub = pubsub_v1.PublisherClient()
        path = pub.topic_path(SETTINGS.gcp_project, TOPIC)
        msg_id = pub.publish(path, json.dumps(alert).encode("utf-8"), source=source, alert_id=aid).result(timeout=30)
        store().put(aid, {"pubsub_message_id": msg_id})
    else:
        threading.Thread(target=process, args=(alert,), daemon=True).start()
    return aid


# ---------------------------------------------------------------------------------------------- worker job
_backend = None
_run_lock = threading.Lock()  # one investigation at a time per process (the MCP session is shared)


def _graph():
    global _backend
    if _backend is None:
        from agent.backends import get_backend
        _backend = get_backend(SETTINGS)
    return _backend


def use_backend(g) -> None:
    global _backend
    _backend = g


def process(alert: dict, reply: dict | None = None) -> dict:
    """Investigate one alert and record every stage. Safe to call twice for the same alert (Pub/Sub may redeliver).

    reply: the cardholder's real answer to a case that is awaiting it; the investigation runs again with it."""
    from agent.llm import LLM
    from agent.orchestrator import Investigation
    from agent.simulator import real_reply
    st = store()
    aid = alert["case_id"]
    doc = st.get(aid) or {}
    if doc.get("status") == "done":
        return doc
    if reply is None and doc.get("status") in ("awaiting_reply", "resuming"):
        return doc   # a redelivered first message: the case is already waiting for its reply
    if not doc:
        st.put(aid, {"alert_id": aid, "status": "queued", "source": "pubsub", "received_at": now(),
                     "alert": json.dumps(alert), "case_id": aid, "txn_id": alert["flagged_txn_id"],
                     "card_id": alert["card_id"], "trigger": alert["trigger_type"],
                     "trigger_text": alert["trigger_text"][:300]})
    t0 = time.time()
    st.put(aid, {"status": "investigating", "started_at": now(), "steps": 0})
    rep = real_reply(**reply) if reply else None
    steps = {"n": 0}

    def emit(ev):  # light progress updates for the queue page
        steps["n"] += 1
        if ev["kind"] in ("tool", "decision", "memory") and steps["n"] % 3 == 0:
            st.put(aid, {"steps": steps["n"], "last_step": str(ev.get("title", ""))[:160]})

    try:
        with _run_lock:
            inv = Investigation(_graph(), LLM(SETTINGS), alert, emit=emit, use_llm_investigator=False, reply=rep)
            ans = inv.run()
        c = ans["case"]
        waiting = c["status"] == "awaiting_evidence"
        fields = {"status": "awaiting_reply" if waiting else "done", "finished_at": now(),
                  "seconds": round(time.time() - t0, 1),
                  "verdict": c["verdict"], "fraud_probability": c["fraud_probability"], "pattern": c["pattern"],
                  "exposure_usd": c["exposure_usd"], "case_status": c["status"],
                  "final_actions": [f"{a['action']} ({a['route']})" for a in ans["next_best_actions"]["final"]],
                  "sar": bool(ans["sar"]["file"]), "graph_case_id": c["graph_case_id"],
                  "written_to_graph": c["written_to_graph"], "graph_calls": ans["tool_calls"],
                  "answer": json.dumps(ans, default=str), "events": json.dumps(inv.events, default=str)}
        if waiting:
            due = datetime.now(timezone.utc) + timedelta(hours=inv.awaiting["wait_hours"])
            fields.update(request_type=inv.awaiting["type"], request_why=inv.awaiting["why"], asked_at=now(),
                          reply_due=due.strftime("%Y-%m-%dT%H:%M:%SZ"))
        st.put(aid, fields)
        from agent import obs
        obs.event("investigation_done", alert_id=aid, seconds=fields["seconds"], verdict=c["verdict"],
                  status=fields["status"], source=doc.get("source", "pubsub"), resumed=bool(reply))
        if not waiting:
            from agent import actions
            actions.after_investigation(alert, ans)
        return fields
    except Exception as e:  # recorded, not raised: a failed alert should not be redelivered forever
        st.put(aid, {"status": "error", "finished_at": now(), "error": f"{type(e).__name__}: {str(e)[:300]}"})
        from agent import obs
        obs.event("investigation_error", "ERROR", alert_id=aid, error=f"{type(e).__name__}: {str(e)[:300]}")
        return {"status": "error"}


# ---------------------------------------------------------------------------------------------- replies
def record_reply(alert_id: str, kind: str, text: str = "", channel: str = "", actor: dict | None = None) -> dict:
    """The cardholder answered (or the wait ran out). Checks the case is waiting, records who said what, and
    queues the case to resume with that answer. Raises ValueError when the case is not waiting for a reply."""
    from agent import access as A
    from agent.simulator import real_reply
    actor = actor or {"email": "system", "role": "system"}
    doc = store().get(alert_id)
    if not doc:
        raise ValueError(f"no alert {alert_id}")
    if doc.get("status") != "awaiting_reply":
        raise ValueError(f"{alert_id} is not waiting for a reply (status: {doc.get('status')})")
    r = real_reply(kind, text, channel, actor.get("email", ""), now())   # validates kind and channel
    payload = {"kind": r.kind, "text": r.text, "channel": r.channel, "recorded_by": r.recorded_by,
               "received_at": r.received_at}
    store().put(alert_id, {"status": "resuming", "reply": json.dumps(payload)})
    A.audit(actor.get("email", "system"), actor.get("role", "system"), "customer_reply_recorded", alert_id,
            {"reply": kind, "channel": channel, "request": doc.get("request_type")})
    alert = json.loads(doc["alert"])
    if MODE == "gcp":
        from google.cloud import pubsub_v1
        pub = pubsub_v1.PublisherClient()
        path = pub.topic_path(SETTINGS.gcp_project, TOPIC)
        pub.publish(path, json.dumps({**alert, "_reply": payload}).encode("utf-8"), source="reply",
                    alert_id=alert_id).result(timeout=30)
    else:
        threading.Thread(target=process, args=(alert, payload), daemon=True).start()
    return payload


def sweep_overdue() -> list[str]:
    """R4: cases whose reply is overdue continue as 'no reply'. Run every few minutes (Cloud Scheduler)."""
    out = []
    t = now()
    for d in store().by_status("awaiting_reply"):
        if d.get("reply_due") and d["reply_due"] <= t:
            try:
                record_reply(d["alert_id"], "no_reply", "No reply before the deadline", "timeout",
                             {"email": "system:reply-timeout", "role": "system"})
                out.append(d["alert_id"])
            except ValueError:
                pass
    return out


def bank_feed(n: int = 3, offset: int = 0) -> list[dict]:
    """A few alerts from the benchmark pack, as the bank's own alerting system would send them."""
    import csv
    with open(SETTINGS.prepared_dir / "case_pack.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    out = []
    for r in rows[offset: offset + n]:
        a = dict(r)
        a["case_id"] = f"FEED-{r['case_id'].split('-')[1]}-{int(time.time()) % 10000:04d}"
        out.append(a)
    return out
