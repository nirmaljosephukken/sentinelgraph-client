"""
Client intake API: the client's systems send transactions and alerts, and read back investigated results.

    uvicorn api.app:app --host 0.0.0.0 --port 8080

Every call needs an API key (Authorization: Bearer sgk_... or X-API-Key), created by an administrator on the
console's Team & access page. Each key is tied to one mapping, so the client sends files in their own format.

    POST /v1/transactions     CSV, JSON lines, a JSON array or {"records": [...]}; ?dry_run=true checks only
    POST /v1/alerts           same formats; each alert's transaction must already be loaded
    GET  /v1/alerts/{id}      status and, when done, verdict, actions, report decision and case id
    POST /v1/alerts/{id}/reply   the cardholder's answer to a case that is waiting for it
    POST /v1/outcomes         what really happened: [{"alert_id": ..., "outcome": "fraud" | "legitimate"}]
    GET  /v1/batches/{id}     the data-quality report of an earlier upload
    GET  /v1/schema           the fields SentinelGraph understands and this key's mapping
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402
from fastapi import Depends, FastAPI, Header, HTTPException, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from starlette.concurrency import run_in_threadpool  # noqa: E402

from agent import access as A  # noqa: E402
from agent import live, obs  # noqa: E402
from agent.ingest import load as L, mapping as M  # noqa: E402
from agent.ingest.schema import ALERT_FIELDS, TXN_FIELDS  # noqa: E402
from agent.policy_config import POLICY  # noqa: E402

MAX_BYTES = 25 * 1024 * 1024
MAX_ROWS = 200_000
app = FastAPI(title="SentinelGraph intake API", version="1.0",
              description="Send transactions and alerts in your own format; read investigated results.")
_write_lock = threading.Lock()
obs.started("api")   # one load at a time per instance keeps the in-memory graph consistent


def key_for(scope: str):
    def dep(authorization: str | None = Header(None), x_api_key: str | None = Header(None)) -> dict:
        key = x_api_key or ((authorization or "").removeprefix("Bearer ").strip() if authorization else "")
        if not key:
            obs.event("api_auth_failed", "WARNING", reason="missing key", scope=scope)
            raise HTTPException(401, "missing API key: send Authorization: Bearer <key> or X-API-Key")
        rec = A.check_api_key(key, scope)
        if rec is None:
            obs.event("api_auth_failed", "WARNING", reason="invalid, revoked or wrong scope", scope=scope,
                      key_id=key[4:12] if key.startswith("sgk_") else "")
            raise HTTPException(403, f"API key is invalid, revoked, or lacks the '{scope}' scope")
        return rec
    return dep


def _actor(rec: dict) -> dict:
    return {"email": f"api-key:{rec['key_id']} ({rec.get('name', '')})", "role": "api"}


async def _frame(request: Request) -> pd.DataFrame:
    body = await request.body()
    if len(body) > MAX_BYTES:
        raise HTTPException(413, f"request larger than {MAX_BYTES // (1024 * 1024)} MB; split the file")
    if not body:
        raise HTTPException(400, "empty request body")
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    try:
        if ctype == "application/json":
            data = json.loads(body)
            records = data.get("records") if isinstance(data, dict) else data
            if not isinstance(records, list):
                raise ValueError("send a JSON array or {\"records\": [...]}")
            df = pd.DataFrame(records, dtype=object)
        elif ctype in ("application/x-ndjson", "application/jsonl"):
            df = M.read_file(body, "upload.jsonl")
        elif ctype in ("text/tab-separated-values",):
            df = M.read_file(body, "upload.tsv")
        else:
            df = M.read_file(body, "upload.csv")
    except (ValueError, json.JSONDecodeError) as ex:
        raise HTTPException(400, f"could not read the body: {ex}") from ex
    if len(df) > MAX_ROWS:
        raise HTTPException(413, f"{len(df)} rows; send at most {MAX_ROWS:,} per request")
    return df


def _mapping(rec: dict) -> M.Mapping:
    try:
        return M.load(rec.get("mapping") or "sentinelgraph")
    except M.MappingError as ex:
        raise HTTPException(500, f"this key's mapping cannot be used: {ex}") from ex


def _public(batch: dict) -> dict:
    rep = batch.get("report") or {}
    return {"batch_id": batch["batch_id"], "status": batch["status"], "reason": batch.get("reason", ""),
            "rows_in": batch.get("rows_in"), "rows_ok": batch.get("rows_ok"), "rows_rejected": batch.get("rows_rejected"),
            "problems": rep.get("problems", []), "rejected_rows": rep.get("rejected_rows", [])[:200],
            "warnings": rep.get("warnings", []), "detectors": rep.get("detectors", {}),
            **({"alert_ids": batch["alert_ids"]} if "alert_ids" in batch else {})}


def _respond(batch: dict) -> JSONResponse:
    code = {"loaded": 200, "checked": 200, "queued": 202, "refused": 422, "error": 500}.get(batch["status"], 200)
    return JSONResponse(_public(batch), status_code=code)


@app.get("/v1/health")
def health():
    return {"ok": True, "policy": POLICY.summary(), "live": live.MODE}


@app.get("/v1/schema")
def schema(rec: dict = Depends(key_for("transactions"))):
    m = _mapping(rec)
    f = lambda fs: [{"field": x.name, "required": x.required, "label": x.label, "feeds": x.feeds,  # noqa: E731
                     **({"choices": [c for c in x.choices if c]} if x.choices else {})} for x in fs]
    return {"mapping": m.name, "title": m.title, "transactions": f(TXN_FIELDS), "alerts": f(ALERT_FIELDS),
            "your_columns": {"transactions": m.transactions, "alerts": m.alerts}}


@app.post("/v1/transactions")
async def transactions(request: Request, dry_run: bool = False, rec: dict = Depends(key_for("transactions"))):
    df, m = await _frame(request), _mapping(rec)

    def run():
        with _write_lock:
            return L.ingest_transactions(live._graph(), df, m, _actor(rec), "api", dry_run=dry_run)
    try:
        return _respond(await run_in_threadpool(run))
    except M.MappingError as ex:
        raise HTTPException(422, str(ex)) from ex


@app.post("/v1/alerts")
async def alerts(request: Request, rec: dict = Depends(key_for("alerts"))):
    df, m = await _frame(request), _mapping(rec)
    try:
        return _respond(await run_in_threadpool(L.ingest_alerts, live._graph(), df, m, _actor(rec), "api"))
    except M.MappingError as ex:
        raise HTTPException(422, str(ex)) from ex


@app.get("/v1/alerts/{alert_id}")
def alert_status(alert_id: str, rec: dict = Depends(key_for("read"))):
    doc = live.store().get(alert_id)
    if not doc:
        raise HTTPException(404, "no such alert")
    out = {k: doc.get(k) for k in ("alert_id", "status", "received_at", "finished_at", "verdict", "fraud_probability",
                                   "pattern", "exposure_usd", "case_status", "final_actions", "sar", "graph_case_id",
                                   "request_type", "request_why", "reply_due", "error") if doc.get(k) is not None}
    out["mode"] = POLICY.mode
    ans = json.loads(doc["answer"]) if doc.get("answer") else None
    if ans:
        out.update(summary=ans["case"].get("summary"), evidence_requests=ans.get("evidence_requests"),
                   actions=ans["next_best_actions"]["final"])
    # Auditors need the policy on every read-back, attributed by the alert's STATE, never by what happens to be
    # stored. Until the case is finished (queued, investigating, awaiting_reply, resuming) the ACTIVE policy is
    # reported and marked provisional: a waiting case is re-investigated under whatever policy is active when the
    # answer arrives. A done case reports only its own record - the policy that produced the decision, None if it
    # was never recorded - so a later policy change can never be misattributed to an old decision. An errored case
    # produced no decision at all, so it reports no policy: a stale first-pass answer must not be dressed up as one.
    if doc.get("status") == "done":
        out.update(policy=(ans or {}).get("policy"), policy_source="case_record")
    elif doc.get("status") == "error":
        out.update(policy=None, policy_source="case_record")
    else:
        out.update(policy=POLICY.summary(), policy_source="active")
    return out


@app.post("/v1/alerts/{alert_id}/reply")
async def alert_reply(alert_id: str, request: Request, rec: dict = Depends(key_for("alerts"))):
    try:
        body = await request.json()
    except ValueError as ex:
        raise HTTPException(400, "send JSON: {\"reply\": \"denied|confirmed|no_reply\", \"channel\": ..., \"text\": ...}") from ex
    if not isinstance(body, dict) or not body.get("reply"):
        raise HTTPException(400, "missing 'reply' (denied, confirmed or no_reply)")
    doc = live.store().get(alert_id)
    if not doc:
        raise HTTPException(404, "no such alert")
    try:
        live.record_reply(alert_id, str(body["reply"]), str(body.get("text") or ""), str(body.get("channel") or ""),
                          _actor(rec))
    except ValueError as ex:
        raise HTTPException(409 if "not waiting" in str(ex) else 422, str(ex)) from ex
    return JSONResponse({"alert_id": alert_id, "status": "resuming"}, status_code=202)


@app.post("/v1/outcomes")
async def post_outcomes(request: Request, rec: dict = Depends(key_for("outcomes"))):
    from agent import outcomes as O
    df = await _frame(request)
    r = await run_in_threadpool(O.bulk, df.to_dict("records"), _actor(rec), "api")
    return JSONResponse(r, status_code=200 if r["recorded"] else 422)


@app.get("/v1/batches/{batch_id}")
def batch(batch_id: str, rec: dict = Depends(key_for("transactions"))):
    doc = A.get_doc("batches", batch_id)
    if not doc:
        raise HTTPException(404, "no such batch")
    for k in ("report", "alert_ids", "result"):
        if isinstance(doc.get(k), str):
            doc[k] = json.loads(doc[k])
    return _public({"batch_id": batch_id, **doc})
