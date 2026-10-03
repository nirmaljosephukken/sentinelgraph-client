"""
One-shot TigerGraph setup: schema -> vector attributes -> data -> case memory -> knowledge -> queries.

    python -m graph.setup_graph               # everything (idempotent; skips finished steps)
    python -m graph.setup_graph --only queries  # just (re)install queries
    python -m graph.setup_graph --only data --limit 20000   # quick smoke load

Data is loaded with RESTPP upserts in parallel batches (no file upload needed, works on Savanna).
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import math
import re
import sys
import time

import pandas as pd

from agent.backends import cc_embedding_text, norm_cc
from agent.config import ROOT, SETTINGS
from agent.embeddings import embed
from agent import knowledge
from agent.tg_client import TGClient

G = ROOT / "graph"


def device_vid(profile: str) -> str:
    return (profile or "").strip()


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def _gsql_file(tg: TGClient, path, graph: str) -> str:
    text = path.read_text(encoding="utf-8").replace("FraudGraph", graph)
    out = tg.gsql(text)
    return out


def step_schema(tg: TGClient) -> None:
    graph = tg.graph
    existing = tg.gsql("SHOW GRAPH *")
    if re.search(rf"Graph {re.escape(graph)}\(", existing):
        log(f"schema: graph {graph} exists, skipping")
        return
    out = _gsql_file(tg, G / "schema.gsql", graph)
    if "succeeded" not in out.lower() and "success" not in out.lower():
        raise SystemExit(out[-2000:])
    tg._token = None  # re-issue token now that the graph exists
    out = _gsql_file(tg, G / "vector_schema.gsql", graph)
    if "succeeded" not in out.lower():
        log("vector attributes unsupported -> fallback LIST<DOUBLE> embeddings")
        _gsql_file(tg, G / "vector_schema_fallback.gsql", graph)
    log("schema: created")


def _vector_mode(tg: TGClient) -> str:
    out = tg.gsql(f"USE GRAPH {tg.graph}\nLS")
    return "native" if re.search(r"emb\(|VECTOR", out) and "emb_list" not in out else "fallback"


def _chunks(items, n):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def _upsert_parallel(tg: TGClient, jobs: list[tuple[dict, dict]], label: str, workers: int = 4) -> None:
    t0 = time.time()
    done = 0
    with cf.ThreadPoolExecutor(workers) as ex:
        futs = [ex.submit(tg.upsert, v, e) for v, e in jobs]
        for f in cf.as_completed(futs):
            f.result()
            done += 1
            if done % 10 == 0 or done == len(futs):
                log(f"  {label}: {done}/{len(futs)} batches ({time.time() - t0:.0f}s)")


def _s(x) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return ""
    s = str(x)
    return "" if s in ("nan", "<NA>") else (s[:-2] if s.endswith(".0") else s)


def _f(x, missing=-1.0) -> float:
    try:
        f = float(x)
        return missing if math.isnan(f) else f
    except (TypeError, ValueError):
        return missing


def step_data(tg: TGClient, limit: int | None = None, batch: int = 15000) -> None:
    log("data: reading prepared transactions")
    t = pd.read_csv(SETTINGS.prepared_dir / "txn.csv.gz", low_memory=False)
    if limit:
        t = t.head(limit)
    t = t.sort_values(["card_id", "ts", "txn_id"]).reset_index(drop=True)
    t["txn_id"] = t["txn_id"].astype(str)
    t["device_profile"] = t["device_profile"].fillna("")

    # ---- small vertex types
    cards = t.groupby("card_id").agg(customer_id=("customer_id", "first"), network=("network", "first"),
                                     card_type=("card_type", "first"), n_txns=("txn_id", "size"))
    custs = cards.groupby("customer_id").size()
    clients = t.groupby("client_id").agg(card_id=("card_id", "first"), region=("addr1", "first"))
    devs = t[t.device_profile != ""].groupby("device_profile").agg(n=("txn_id", "size"), dtype_=("device_type", "first"))
    V = {
        "Customer": {c: {"n_cards": {"value": int(n)}} for c, n in custs.items()},
        "Card": {c: {"customer_id": {"value": r.customer_id}, "network": {"value": _s(r.network)},
                     "card_type": {"value": _s(r.card_type)}, "n_txns": {"value": int(r.n_txns)}}
                 for c, r in cards.iterrows()},
        "Client": {c: {"card_id": {"value": r.card_id}, "home_region": {"value": _s(r.region)},
                       "open_day": {"value": int(c.split("|")[-1])}} for c, r in clients.iterrows()},
        "DeviceProfile": {device_vid(d): {"device_type": {"value": _s(r.dtype_)}, "n_txns": {"value": int(r.n)}}
                          for d, r in devs.iterrows() if device_vid(d)},
        "EmailDomain": {e: {} for e in pd.concat([t.p_email, t.r_email]).dropna().unique()},
        "BillingRegion": {_s(a): {} for a in t.addr1.dropna().unique()},
    }
    jobs = []
    for vt, items in V.items():
        keys = list(items)
        for ks in _chunks(keys, 20000):
            jobs.append(({vt: {k: items[k] for k in ks}}, None))
    E_small = []
    for c, r in cards.iterrows():
        E_small.append(("Customer", r.customer_id, "HAS_CARD", "Card", c))
    for c, r in clients.iterrows():
        E_small.append(("Client", c, "CLIENT_CARD", "Card", r.card_id))
    for es in _chunks(E_small, 30000):
        e: dict = {}
        for st, sid, et, tt, tid in es:
            e.setdefault(st, {}).setdefault(sid, {}).setdefault(et, {}).setdefault(tt, {})[tid] = {}
        jobs.append((None, e))
    _upsert_parallel(tg, jobs, "entities")

    # ---- transactions + their edges, streamed batch by batch (bounded memory)
    cols = list(t.columns)
    prev: dict[str, tuple[str, str]] = {}

    def build(chunk_df):
        vt: dict = {}
        e: dict = {}

        def edge(st, sid, et, tt, tid, attrs=None):
            e.setdefault(st, {}).setdefault(sid, {}).setdefault(et, {}).setdefault(tt, {})[tid] = attrs or {}

        for tup in chunk_df.itertuples(index=False, name=None):
            r = dict(zip(cols, tup))
            x = r["txn_id"]
            dp = r["device_profile"] or ""
            vt[x] = {
                "ts": {"value": r["ts"]}, "amount": {"value": float(r["amount"])}, "product": {"value": _s(r["product"])},
                "channel": {"value": _s(r["channel"])}, "risk_score": {"value": _f(r["risk_score"], 0.0)},
                "model_score": {"value": _f(r["model_score"], 0.0)}, "addr1": {"value": _s(r["addr1"])},
                "addr2": {"value": _s(r["addr2"])}, "dist1": {"value": _f(r["dist1"])},
                "p_email": {"value": _s(r["p_email"])}, "r_email": {"value": _s(r["r_email"])},
                "device_status": {"value": _s(r["device_status"])}, "proxy_type": {"value": _s(r["proxy"])},
                "device_profile": {"value": dp}, "device_type": {"value": _s(r["device_type"])},
                "m4": {"value": _s(r["M4"])}, "m6": {"value": _s(r["M6"])}, "d1": {"value": _f(r["D1"])},
                "card_id": {"value": r["card_id"]}, "customer_id": {"value": r["customer_id"]},
                "client_id": {"value": r["client_id"]},
            }
            edge("Card", r["card_id"], "CARD_TXN", "Txn", x)
            edge("Client", r["client_id"], "CLIENT_TXN", "Txn", x)
            if device_vid(dp):
                edge("Txn", x, "TXN_DEVICE", "DeviceProfile", device_vid(dp))
            if _s(r["p_email"]):
                edge("Txn", x, "TXN_P_EMAIL", "EmailDomain", _s(r["p_email"]))
            if _s(r["r_email"]):
                edge("Txn", x, "TXN_R_EMAIL", "EmailDomain", _s(r["r_email"]))
            if _s(r["addr1"]):
                edge("Txn", x, "TXN_REGION", "BillingRegion", _s(r["addr1"]))
            p = prev.get(r["card_id"])
            if p:
                gap = int((pd.Timestamp(r["ts"]) - pd.Timestamp(p[1])).total_seconds())
                edge("Txn", p[0], "NEXT_TXN", "Txn", x, {"gap_s": {"value": gap}})
            prev[r["card_id"]] = (x, r["ts"])
        return {"Txn": vt}, e

    def send(v, e):
        tg.upsert(vertices=v)
        tg.upsert(edges=e)

    t0 = time.time()
    n_batches = math.ceil(len(t) / batch)
    with cf.ThreadPoolExecutor(3) as ex:
        inflight = []
        for i, start in enumerate(range(0, len(t), batch)):
            v, e = build(t.iloc[start:start + batch])
            inflight.append(ex.submit(send, v, e))
            del v, e
            if len(inflight) >= 3:
                inflight.pop(0).result()
            if (i + 1) % 5 == 0:
                log(f"  transactions: {i + 1}/{n_batches} batches ({time.time() - t0:.0f}s)")
        for fu in inflight:
            fu.result()
    log(f"data: loaded {len(t):,} transactions")


def step_memory(tg: TGClient, mode: str) -> None:
    cc = pd.read_csv(SETTINGS.prepared_dir / "closed_cases.csv", dtype=str).fillna("")
    rows = [norm_cc(r) for r in cc.rename(columns={"case_id": "id"}).to_dict("records")]
    jobs = []
    for chunk in _chunks(rows, 1000):
        v: dict = {"ClosedCase": {}, "Pattern": {}}
        e: dict = {}
        for c in chunk:
            emb = embed(*cc_embedding_text(c))
            attrs = {k: {"value": c[k]} for k in ("customer_id", "card_id", "outcome", "pattern", "n_txns",
                                                   "exposure_usd", "actions_taken", "report_filed", "analyst_notes",
                                                   "txn_ids", "connected_card_ids", "first_fraud_txn_id")}
            attrs["opened_at"] = {"value": c["opened_at"]}
            attrs["closed_at"] = {"value": c["closed_at"]}
            attrs["emb" if mode == "native" else "emb_list"] = {"value": emb}
            v["ClosedCase"][c["id"]] = attrs
            v["Pattern"][c["pattern"]] = {}
            ce = e.setdefault("ClosedCase", {}).setdefault(c["id"], {})
            ce.setdefault("CC_CARD", {}).setdefault("Card", {})[c["card_id"]] = {}
            ce.setdefault("CC_PATTERN", {}).setdefault("Pattern", {})[c["pattern"]] = {}
            for x in filter(None, c["txn_ids"].split("|")):
                ce.setdefault("CC_TXN", {}).setdefault("Txn", {})[x] = {}
            for k in filter(None, c["connected_card_ids"].split("|")):
                ce.setdefault("CC_CONNECTED_CARD", {}).setdefault("Card", {})[k] = {}
        jobs.append((v, e))
    _upsert_parallel(tg, jobs, "closed cases", workers=2)
    log(f"memory: loaded {len(rows):,} closed cases with embeddings ({mode})")


def step_knowledge(tg: TGClient, mode: str) -> None:
    ch = knowledge.chunks()
    v = {"PolicyChunk": {c["id"]: {"doc": {"value": c["doc"]}, "section": {"value": c["section"]},
                                   "text": {"value": c["text"]},
                                   ("emb" if mode == "native" else "emb_list"): {"value": c["emb"]}} for c in ch}}
    tg.upsert(vertices=v)
    log(f"knowledge: loaded {len(ch)} policy/typology/regulatory chunks")


def step_queries(tg: TGClient, mode: str) -> None:
    files = [G / "queries" / "investigation.gsql", G / "queries" / "ingest.gsql",
             G / "queries" / ("graphrag_vector.gsql" if mode == "native" else "graphrag_fallback.gsql")]
    for f in files:
        out = _gsql_file(tg, f, tg.graph)
        bad = [ln for ln in out.splitlines() if "error" in ln.lower() or "fail" in ln.lower()]
        log(f"queries: created {f.name}" + (f" with messages: {bad[:5]}" if bad else ""))
    out = tg.gsql(f"USE GRAPH {tg.graph}\nINSTALL QUERY ALL", timeout=3600)
    tail = out.strip().splitlines()[-5:]
    log("queries: install -> " + " | ".join(tail))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["schema", "data", "memory", "knowledge", "queries"])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--empty", action="store_true",
                    help="client graph: schema, policy knowledge and queries only; transactions arrive via the intake")
    a = ap.parse_args()
    tg = TGClient()
    tg.token()
    steps = [a.only] if a.only else (["schema", "knowledge", "queries"] if a.empty else
                                     ["schema", "data", "memory", "knowledge", "queries"])
    if "schema" in steps:
        step_schema(tg)
        tg._token = None
        tg.token()
    mode = _vector_mode(tg)
    log(f"vector mode: {mode}")
    if "data" in steps:
        step_data(tg, a.limit)
    if "memory" in steps:
        step_memory(tg, mode)
    if "knowledge" in steps:
        step_knowledge(tg, mode)
    if "queries" in steps:
        step_queries(tg, mode)
    log("done")


if __name__ == "__main__":
    sys.exit(main())
