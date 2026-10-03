"""
Cloud Run worker: receives alerts from the Pub/Sub push subscription and investigates them.

    uvicorn worker.app:app --host 0.0.0.0 --port 8080

The service is private (no unauthenticated access); Pub/Sub calls it with an OIDC token for the
sentinelgraph-run service account, which holds roles/run.invoker on it.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, Request, Response  # noqa: E402
from starlette.concurrency import run_in_threadpool  # noqa: E402

from agent import live  # noqa: E402

app = FastAPI(title="SentinelGraph worker")
from agent import obs  # noqa: E402
obs.started("worker")


@app.get("/healthz")
def healthz():
    return {"ok": True, "mode": live.MODE}


@app.post("/pubsub")
async def pubsub_push(request: Request):
    body = await request.json()
    msg = (body or {}).get("message") or {}
    try:
        alert = json.loads(base64.b64decode(msg.get("data", "")).decode("utf-8"))
        alert["case_id"]
    except Exception:
        return Response(status_code=204)  # malformed: acknowledge so it is not redelivered
    reply = alert.pop("_reply", None)    # a case resuming with the cardholder's real answer
    result = await run_in_threadpool(live.process, alert, reply)
    return {"alert_id": alert["case_id"], "status": result.get("status")}


@app.post("/tasks/sweep")
async def sweep():
    """Cloud Scheduler, every 15 minutes: overdue replies continue as 'no reply' (R4)."""
    resumed = await run_in_threadpool(live.sweep_overdue)
    return {"resumed": resumed}
