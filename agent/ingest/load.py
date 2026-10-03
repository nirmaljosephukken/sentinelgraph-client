"""
Load mapped client data into the investigation graph and queue client alerts.

    transactions:  mapped rows -> TigerGraph upserts (vertices + edges, then degree recount)    on Cloud Run
                               -> the in-memory graph + data/client/ batch files               locally
    alerts:        mapped rows -> the flagged transaction must already be in the graph
                               -> the same live queue the console and the dispute intake use

Every batch, accepted or refused, is recorded with its data-quality report, and written to the audit log.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from agent import access as A
from agent.config import ROOT
from agent.ingest import mapping as M
from agent.ingest.schema import TS_FMT
from agent.safety import check, mask

CLIENT_DIR = Path(os.getenv("CLIENT_DATA_DIR", ROOT / "data" / "client"))
DEMO_GRAPH = "FraudGraph"


class IngestError(RuntimeError):
    pass


def _v(x):
    """A mapped cell as a plain value: None for empty or missing (pandas 3 gives NaN where pandas 2 gave None)."""
    if x is None:
        return None
    try:
        if pd.isna(x):
            return None
    except (TypeError, ValueError):
        pass
    return None if isinstance(x, str) and not x.strip() else x


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_batch_id(kind: str) -> str:
    return f"{'TX' if kind == 'transactions' else 'AL'}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"


# ---------------------------------------------------------------------------------------------- graph writes
def _s(x) -> str:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return ""
    return str(x)


def _f(x, missing: float = -1.0) -> float:
    try:
        v = float(x)
        return missing if pd.isna(v) else v
    except (TypeError, ValueError):
        return missing


def _tg_payload(df: pd.DataFrame) -> tuple[dict, dict]:
    V: dict = {k: {} for k in ("Customer", "Card", "Client", "DeviceProfile", "EmailDomain", "BillingRegion", "Txn")}
    E: dict = {}

    def edge(st, sid, et, tt, tid):
        E.setdefault(st, {}).setdefault(sid, {}).setdefault(et, {}).setdefault(tt, {})[tid] = {}

    for r in df.to_dict("records"):
        x, card, cust, client = r["id"], r["card_id"], r["customer_id"], r["client_id"]
        V["Customer"].setdefault(cust, {})
        V["Card"][card] = {"customer_id": {"value": cust}, "network": {"value": _s(r["network"])},
                           "card_type": {"value": _s(r["card_type"])}}
        parts = client.split("|")
        V["Client"][client] = {"card_id": {"value": card}, "home_region": {"value": _s(r["addr1"])},
                               "open_day": {"value": int(parts[-1]) if parts[-1].lstrip("-").isdigit() else 0}}
        dp = _s(r["device_profile"]).strip()
        if dp:
            V["DeviceProfile"][dp] = {"device_type": {"value": _s(r["device_type"])}}
        for em in (_s(r["p_email"]), _s(r["r_email"])):
            if em:
                V["EmailDomain"].setdefault(em, {})
        if _s(r["addr1"]):
            V["BillingRegion"].setdefault(_s(r["addr1"]), {})
        V["Txn"][x] = {
            "ts": {"value": r["ts"]}, "amount": {"value": float(r["amount"])}, "product": {"value": _s(r["product"])},
            "channel": {"value": _s(r["channel"])}, "risk_score": {"value": _f(r["risk_score"], 0.0)},
            "model_score": {"value": _f(r["model_score"], -1.0)}, "addr1": {"value": _s(r["addr1"])},
            "addr2": {"value": _s(r["addr2"])}, "dist1": {"value": _f(r["dist1"])},
            "p_email": {"value": _s(r["p_email"])}, "r_email": {"value": _s(r["r_email"])},
            "device_status": {"value": _s(r["device_status"])}, "proxy_type": {"value": _s(r["proxy"])},
            "device_profile": {"value": dp}, "device_type": {"value": _s(r["device_type"])},
            "m4": {"value": _s(r["m4"])}, "m6": {"value": _s(r["m6"])}, "d1": {"value": _f(r["d1"])},
            "card_id": {"value": card}, "customer_id": {"value": cust}, "client_id": {"value": client}}
        edge("Customer", cust, "HAS_CARD", "Card", card)
        edge("Client", client, "CLIENT_CARD", "Card", card)
        edge("Card", card, "CARD_TXN", "Txn", x)
        edge("Client", client, "CLIENT_TXN", "Txn", x)
        if dp:
            edge("Txn", x, "TXN_DEVICE", "DeviceProfile", dp)
        if _s(r["p_email"]):
            edge("Txn", x, "TXN_P_EMAIL", "EmailDomain", _s(r["p_email"]))
        if _s(r["r_email"]):
            edge("Txn", x, "TXN_R_EMAIL", "EmailDomain", _s(r["r_email"]))
        if _s(r["addr1"]):
            edge("Txn", x, "TXN_REGION", "BillingRegion", _s(r["addr1"]))
    return {k: v for k, v in V.items() if v}, E


def _to_tigergraph(g, df: pd.DataFrame, chunk: int = 5000) -> dict:
    tg = g.tg
    if tg.graph == DEMO_GRAPH and os.getenv("INGEST_TO_DEMO_GRAPH") != "1":
        raise IngestError(f"TG_GRAPH is the demo graph {DEMO_GRAPH!r}. Create a separate graph for this client "
                          f"(python graph/setup_graph.py --empty with TG_GRAPH set) before loading client data.")
    for start in range(0, len(df), chunk):
        v, e = _tg_payload(df.iloc[start:start + chunk])
        tg.upsert(vertices=v)
        tg.upsert(edges=e)
    devs = sorted({d for d in df["device_profile"] if d})
    tg.run_query("refresh_counts", {"cards": sorted(df["card_id"].unique().tolist()), "devs": devs,
                                    "custs": sorted(df["customer_id"].unique().tolist())})
    return {"target": f"TigerGraph graph {tg.graph}"}


def _to_local(g, df: pd.DataFrame, batch_id: str) -> dict:
    CLIENT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(CLIENT_DIR / f"{batch_id}.csv.gz", index=False, compression="gzip")
    g.add_transactions(df)
    return {"target": f"local graph (data/client/{batch_id}.csv.gz)"}


def write_transactions(g, df: pd.DataFrame, batch_id: str) -> dict:
    if hasattr(g, "add_transactions"):
        return _to_local(g, df, batch_id)
    if hasattr(g, "tg"):
        return _to_tigergraph(g, df)
    raise IngestError(f"the {getattr(g, 'name', '?')} backend cannot take client data")


# ---------------------------------------------------------------------------------------------- batches
def _record(batch: dict, actor: dict) -> None:
    A.put_doc("batches", batch["batch_id"], {k: (json.dumps(v, default=str) if isinstance(v, (dict, list)) else v)
                                             for k, v in batch.items()})
    A.audit(actor.get("email", "?"), actor.get("role", "?"), f"data_{batch['kind']}_{batch['status']}",
            detail={"batch": batch["batch_id"], "mapping": batch["mapping"], "rows_in": batch["rows_in"],
                    "rows_ok": batch["rows_ok"], "rows_rejected": batch["rows_rejected"], "source": batch["source"]})
    flagged = (batch.get("report") or {}).get("flagged_values") or {}
    if flagged:
        A.audit(actor.get("email", "?"), actor.get("role", "?"), "input_flagged",
                detail={"source": f"{batch['kind']} batch {batch['batch_id']}", "fields": sorted(flagged),
                        "reasons": ["values read like instructions to the agent"]})


def recent_batches(limit: int = 50) -> list[dict]:
    out = []
    for d in A.list_docs("batches"):
        for k in ("report", "alert_ids", "result"):
            if isinstance(d.get(k), str):
                try:
                    d[k] = json.loads(d[k])
                except ValueError:
                    pass
        out.append(d)
    return sorted(out, key=lambda d: d.get("received_at", ""), reverse=True)[:limit]


def ingest_transactions(g, raw: pd.DataFrame, m: M.Mapping, actor: dict, source: str, dry_run: bool = False) -> dict:
    """Map, check and load a transaction file. Returns the batch record (with the data-quality report)."""
    bid = new_batch_id("transactions")
    ok, rep = M.apply(raw, m, "transactions")
    batch = {"batch_id": bid, "kind": "transactions", "mapping": m.name, "source": source, "received_at": _now(),
             "actor": actor.get("email", "?"), "rows_in": rep.rows_in, "rows_ok": rep.rows_ok,
             "rows_rejected": rep.n_rejected, "report": rep.to_dict()}
    if rep.refused:
        batch.update(status="refused", reason=rep.refused)
    elif dry_run:
        batch.update(status="checked", reason="dry run: checked only, nothing loaded")
        return batch
    elif not len(ok):
        batch.update(status="refused", reason="no rows passed the checks")
    else:
        try:
            batch.update(status="loaded", result=write_transactions(g, ok, bid),
                         first_ts=str(ok["ts"].min()), last_ts=str(ok["ts"].max()),
                         cards=int(ok["card_id"].nunique()))
        except Exception as ex:  # noqa: BLE001  recorded so the client sees why
            batch.update(status="error", reason=f"{type(ex).__name__}: {str(ex)[:300]}")
    _record(batch, actor)
    return batch


def ingest_alerts(g, raw: pd.DataFrame, m: M.Mapping, actor: dict, source: str, publish=None) -> dict:
    """Map and check client alerts; each one must point at a transaction already in the graph. Queues them."""
    from agent import live
    publish = publish or live.publish
    bid = new_batch_id("alerts")
    ok, rep = M.apply(raw, m, "alerts")
    batch = {"batch_id": bid, "kind": "alerts", "mapping": m.name, "source": source, "received_at": _now(),
             "actor": actor.get("email", "?"), "rows_in": rep.rows_in}
    queued, now = [], datetime.now(timezone.utc).strftime(TS_FMT)
    if not rep.refused:
        for i, r in ok.iterrows():
            try:
                x = g.txn_context(str(r["flagged_txn_id"]))["txn"]
            except Exception:  # noqa: BLE001  unknown id in either backend
                rep.rejected.append({"row": int(i) + 2, "field": "flagged_txn_id", "value": r["flagged_txn_id"],
                                     "problem": "transaction not found: load the transaction before its alert"})
                continue
            rs = _v(r["risk_score"]) if _v(r["risk_score"]) is not None else x.get("risk_score")
            text, _pii = mask(_v(r["trigger_text"]) or "")
            sf = check(text)
            trig = _v(r["trigger_type"]) or "risk_score"
            if sf["flagged"]:
                text = (f"{text} (alert text flagged for possible manipulation: {'; '.join(sf['reasons'][:3])}; "
                        f"routed to an analyst)")
                trig = "analyst_request" if trig == "customer_report" else trig
                A.audit(actor.get("email", "?"), actor.get("role", "?"), "input_flagged", str(r["case_id"]),
                        {"source": f"alert batch {bid}", "reasons": sf["reasons"]})
            alert = {"case_id": str(r["case_id"]), "opened_at": _v(r["opened_at"]) or now,
                     "trigger_type": trig, "safety": {**sf, "pii_masked": _pii},
                     "trigger_text": text or f"Client alert on transaction {x['id']} "
                                             f"({x['amount']:,.2f}, {x['channel'] or 'unknown channel'})",
                     "flagged_txn_id": x["id"], "card_id": _v(r["card_id"]) or x["card_id"],
                     "customer_id": _v(r["customer_id"]) or x.get("customer_id", ""),
                     "risk_score": "" if rs is None else f"{float(rs):.2f}"}
            publish(alert, f"{source} · batch {bid}")
            queued.append(alert["case_id"])
    rep.rows_ok = len(queued)
    status = "refused" if rep.refused else ("queued" if queued else "refused")
    batch.update(status=status, rows_ok=len(queued), rows_rejected=rep.n_rejected, report=rep.to_dict(),
                 alert_ids=queued, reason=rep.refused or ("" if queued else "no alert passed the checks"))
    _record(batch, actor)
    return batch
