"""
Graph backends behind the agent's tools.

TigerGraphBackend  - production path. Every read is an installed GSQL query, called through the
                     TigerGraph MCP server when available, otherwise through RESTPP. Case memory is
                     written back with REST upserts. Vector retrieval uses TigerGraph's vector index.
LocalBackend       - an in-memory mirror of the same queries over data/prepared (pandas). Used for
                     offline development, unit tests and CI, and as a reference implementation that the
                     GSQL results are checked against. It returns exactly the same shapes.

All transaction rows share one normalised shape (see `norm_txn`).
"""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

from agent.config import SETTINGS, Settings
from agent.embeddings import cosine, embed
from agent import knowledge

TXN_FIELDS = ["id", "ts", "amount", "product", "channel", "risk_score", "model_score", "addr1", "addr2",
              "dist1", "p_email", "r_email", "device_status", "proxy", "device_profile", "device_type",
              "m4", "m6", "d1", "card_id", "customer_id", "client_id"]


def _num(x, missing=None):
    if x is None:
        return missing
    try:
        f = float(x)
    except (TypeError, ValueError):
        return missing
    if math.isnan(f) or f == -1.0:
        return missing
    return f


def _str(x) -> str:
    if x is None:
        return ""
    if isinstance(x, float) and math.isnan(x):
        return ""
    s = str(x)
    return "" if s in ("nan", "None", "<NA>") else s


TXN_COLS = ["id", "ts", "amount", "product", "channel", "risk_score", "model_score", "addr1", "addr2", "dist1",
            "p_email", "r_email", "device_status", "proxy", "device_profile", "device_type", "m4", "m6", "d1",
            "card_id", "customer_id", "client_id", "network", "card_type"]


def norm_txn(a: dict) -> dict:
    """Normalise a transaction from either backend into one plain dict."""
    addr1 = _str(a.get("addr1"))
    if addr1.endswith(".0"):
        addr1 = addr1[:-2]
    return {
        "id": _str(a.get("id") or a.get("txn_id")),
        "ts": _str(a.get("ts"))[:19],
        "amount": round(float(a.get("amount") or 0.0), 2),
        "product": _str(a.get("product")),
        "channel": _str(a.get("channel")),
        "risk_score": _num(a.get("risk_score"), 0.0),
        "model_score": _num(a.get("model_score"), 0.0),
        "model_scored": _num(a.get("model_score")) is not None,
        "addr1": addr1,
        "addr2": _str(a.get("addr2")).replace(".0", ""),
        "dist1": _num(a.get("dist1")),
        "p_email": _str(a.get("p_email")),
        "r_email": _str(a.get("r_email")),
        "device_status": _str(a.get("device_status")),
        "proxy": _str(a.get("proxy_type", a.get("proxy"))),
        "device_profile": _str(a.get("device_profile")),
        "device_type": _str(a.get("device_type")),
        "m4": _str(a.get("m4") or a.get("M4")),
        "m6": _str(a.get("m6") or a.get("M6")),
        "d1": _num(a.get("d1") if "d1" in a else a.get("D1")),
        "card_id": _str(a.get("card_id")),
        "customer_id": _str(a.get("customer_id")),
        "client_id": _str(a.get("client_id")),
        "via_device": bool(a.get("@via_device", a.get("via_device", False))),
        "via_email": bool(a.get("@via_email", a.get("via_email", False))),
    }


CC_FIELDS = ["id", "customer_id", "card_id", "opened_at", "closed_at", "outcome", "pattern", "n_txns",
             "exposure_usd", "actions_taken", "report_filed", "analyst_notes", "txn_ids", "connected_card_ids",
             "first_fraud_txn_id"]


def norm_cc(a: dict) -> dict:
    d = {k: a.get(k) for k in CC_FIELDS}
    d["id"] = _str(a.get("id") or a.get("case_id"))
    for k in ("customer_id", "card_id", "outcome", "pattern", "actions_taken", "report_filed", "analyst_notes",
              "txn_ids", "connected_card_ids", "first_fraud_txn_id"):
        d[k] = _str(d.get(k))
    d["opened_at"] = _str(d.get("opened_at"))[:19]
    d["closed_at"] = _str(d.get("closed_at"))[:19]
    d["n_txns"] = int(float(d.get("n_txns") or 0))
    d["exposure_usd"] = float(d.get("exposure_usd") or 0.0)
    return d


def cc_embedding_text(c: dict) -> tuple[str, list[str]]:
    toks = [c.get("pattern", ""), c.get("outcome", ""), "report_" + str(c.get("report_filed", "")).lower()]
    return c.get("analyst_notes", ""), toks


# =====================================================================================
class GraphBackend:
    name = "abstract"
    transport_log: list[str]

    def txn_context(self, txn_id: str) -> dict: ...
    def card_window(self, card_id: str, t0: str, t1: str) -> list[dict]: ...
    def card_profile(self, card_id: str, before: str) -> dict: ...
    def client_history(self, client_id: str) -> list[dict]: ...
    def device_fanout(self, device: str, t0: str, t1: str) -> dict: ...
    def peer_txns(self, txn_id: str, t0: str, t1: str, lo: float, hi: float, use_email: bool) -> list[dict]: ...
    def prior_cases(self, customer_id: str) -> dict: ...
    def device_cases(self, device: str) -> dict: ...
    def amount_band_scan(self, t0: str, t1: str, lo: float, hi: float) -> list[dict]: ...
    def device_ring_scan(self, t0: str, t1: str, min_cards: int, max_profile_txns: int) -> list[dict]: ...
    def similar_closed_cases(self, vec: list[float], k: int) -> list[tuple[dict, float]]: ...
    def similar_agent_cases(self, vec: list[float], k: int) -> list[tuple[dict, float]]: ...
    def policy_search(self, vec: list[float], k: int) -> list[tuple[dict, float]]: ...
    def write_case(self, record: dict) -> bool: ...
    def graph_stats(self) -> dict: ...


# =====================================================================================
class LocalBackend(GraphBackend):
    """Pandas mirror of the GSQL queries (reference implementation / offline mode)."""
    name = "local"

    def __init__(self, s: Settings = SETTINGS):
        import pandas as pd
        self.s = s
        self.transport_log = []
        d = s.prepared_dir
        # DATA_SOURCE: demo (the benchmark dataset), client (only data loaded through the intake) or both
        source = os.getenv("DATA_SOURCE", "both").lower()
        frames, cc = [], []
        if source in ("demo", "both"):
            t = pd.read_csv(d / "txn.csv.gz", low_memory=False, usecols=["txn_id"] + TXN_COLS[1:16] + ["M4", "M6", "D1"]
                            + TXN_COLS[19:])
            frames.append(t.rename(columns={"txn_id": "id", "M4": "m4", "M6": "m6", "D1": "d1"}))
            ccf = pd.read_csv(d / "closed_cases.csv", dtype=str).fillna("")
            cc = [norm_cc(r) for r in ccf.rename(columns={"case_id": "id"}).to_dict("records")]
        if source in ("client", "both"):
            cdir = Path(os.getenv("CLIENT_DATA_DIR", Path(s.prepared_dir).parent / "client"))
            for f in sorted(cdir.glob("*.csv.gz")) if cdir.exists() else []:
                frames.append(pd.read_csv(f, dtype={"id": str, "card_id": str, "customer_id": str, "client_id": str,
                                                   "addr1": str, "addr2": str}, keep_default_na=True))
        frames = [f for f in frames if len(f)]
        t = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame({c: pd.Series(dtype=object) for c in TXN_COLS})
        t["id"] = t["id"].astype(str)
        t = t.drop_duplicates("id", keep="last")
        self._index(t, cc)
        self.case_dir = Path(os.getenv("AGENT_CASES_DIR") or
                             Path(self.s.prepared_dir).parent / ("agent_cases_client" if source == "client" else "agent_cases_local"))
        self.case_dir.mkdir(parents=True, exist_ok=True)

    def add_transactions(self, df) -> int:
        """Client intake: add (or replace, by id) transactions in the in-memory graph. Returns the new total."""
        import pandas as pd
        new = df.copy()
        new["id"] = new["id"].astype(str)
        t = self.t[~self.t["id"].isin(set(new["id"]))]
        new = new[[c for c in new.columns if c in TXN_COLS]]
        self._index_txns(pd.concat([t, new], ignore_index=True) if len(t) else new.reset_index(drop=True))
        return len(self.t)

    def _index(self, t, cc: list[dict]) -> None:
        """Build the in-memory graph from a transactions frame and closed cases (dataset or a generated world)."""
        self._index_txns(t)
        self.cc = cc
        self.cc_by_card: dict[str, list[dict]] = {}
        self.cc_conn_by_card: dict[str, list[dict]] = {}
        self.cc_by_txn: dict[str, list[dict]] = {}
        for c in self.cc:
            self.cc_by_card.setdefault(c["card_id"], []).append(c)
            for k in filter(None, c["connected_card_ids"].split("|")):
                self.cc_conn_by_card.setdefault(k, []).append(c)
            for x in filter(None, c["txn_ids"].split("|")):
                self.cc_by_txn.setdefault(x, []).append(c)
        self.cc_emb = [embed(*cc_embedding_text(c)) for c in self.cc]
        self.chunks = knowledge.chunks()

    def _index_txns(self, t) -> None:
        t = t.copy()
        t["id"] = t["id"].astype(str)
        t["device_profile"] = t["device_profile"].fillna("")
        t = t.sort_values(["ts", "id"]).reset_index(drop=True)
        self.t = t
        self.by_id = {k: i for i, k in enumerate(t["id"].values)}
        self.by_card = t.groupby("card_id").indices
        self.by_client = t.groupby("client_id").indices
        # .groups keeps the row labels of the full (re-indexed) frame; .indices on a filtered frame would give
        # positions inside the subset and point device lookups at the wrong rows
        self.by_device = {k: list(v) for k, v in t[t.device_profile != ""].groupby("device_profile").groups.items()}
        self.by_email = t.groupby("p_email").indices
        self.cards = t.groupby("card_id").agg(customer_id=("customer_id", "first"), network=("network", "first"),
                                              card_type=("card_type", "first"), n_txns=("id", "size"))

    def _log(self, q):
        self.transport_log.append(f"local:{q}")

    def _rows(self, idx) -> list[dict]:
        if len(idx) == 0:
            return []
        return [norm_txn(r) for r in self.t.iloc[sorted(idx)].to_dict("records")]

    def _window(self, idx, t0, t1):
        ts = self.t["ts"].values
        return [i for i in idx if t0 <= ts[i] <= t1]

    # ---- queries -------------------------------------------------------------
    def txn_context(self, txn_id):
        self._log("txn_context")
        i = self.by_id[str(txn_id)]
        row = norm_txn(self.t.iloc[i].to_dict())
        card = self.cards.loc[row["card_id"]]
        sibs = [{"id": k, **self.cards.loc[k].to_dict()} for k in self.cards.index
                if k.startswith(row["customer_id"] + "-")]
        return {"txn": row, "card": {"id": row["card_id"], **card.to_dict()},
                "customer": {"id": row["customer_id"], "n_cards": len(sibs)}, "customer_cards": sibs}

    def card_window(self, card_id, t0, t1):
        self._log("card_window")
        return self._rows(self._window(self.by_card.get(card_id, []), t0, t1))

    def card_profile(self, card_id, before):
        self._log("card_profile")
        rows = [r for r in self._rows(self.by_card.get(card_id, [])) if r["ts"] < before]
        def cnt(key):
            out: dict[str, int] = {}
            for r in rows:
                v = r[key]
                if key in ("device_profile", "p_email") and not v:
                    continue
                out[v] = out.get(v, 0) + 1
            return out
        return {"n": len(rows), "amount_sum": sum(r["amount"] for r in rows),
                "amount_max": max([r["amount"] for r in rows], default=0.0), "amounts": [r["amount"] for r in rows],
                "products": cnt("product"), "regions": cnt("addr1"), "devices": cnt("device_profile"),
                "emails": cnt("p_email"), "channels": cnt("channel"),
                "first_ts": rows[0]["ts"] if rows else "", "last_ts": rows[-1]["ts"] if rows else ""}

    def client_history(self, client_id):
        self._log("client_history")
        return self._rows(self.by_client.get(client_id, []))

    def device_fanout(self, device, t0, t1):
        self._log("device_fanout")
        idx = self.by_device.get(device, [])
        return {"device": {"id": device, "n_txns": len(idx)}, "txns": self._rows(self._window(idx, t0, t1))}

    def peer_txns(self, txn_id, t0, t1, lo, hi, use_email):
        self._log("peer_txns")
        i = self.by_id[str(txn_id)]
        x = self.t.iloc[i]
        out: dict[int, dict] = {}
        amt = self.t["amount"].values
        def add(idx, flag):
            for j in self._window(idx, t0, t1):
                if j != i and lo <= amt[j] <= hi:
                    out.setdefault(j, {"via_device": False, "via_email": False})[flag] = True
        if x.device_profile:
            add(self.by_device.get(x.device_profile, []), "via_device")
        if use_email and isinstance(x.p_email, str):
            add(self.by_email.get(x.p_email, []), "via_email")
        rows = []
        for j, flags in sorted(out.items()):
            r = norm_txn(self.t.iloc[j].to_dict())
            r.update(flags)
            rows.append(r)
        return rows

    def prior_cases(self, customer_id):
        self._log("prior_cases")
        cards = [k for k in self.cards.index if k.startswith(customer_id + "-")]
        subj = [c for k in cards for c in self.cc_by_card.get(k, [])]
        conn = [c for k in cards for c in self.cc_conn_by_card.get(k, [])]
        mine = [json.loads(p.read_text()) for p in self.case_dir.glob("*.json")]
        mine = [m for m in mine if any(k in m.get("card_ids", []) for k in cards)]
        return {"closed_cases": subj, "connected_closed_cases": conn, "agent_cases": mine}

    def device_cases(self, device):
        self._log("device_cases")
        ids = self.t["id"].values
        seen, out = set(), []
        for j in self.by_device.get(device, []):
            for c in self.cc_by_txn.get(ids[j], []):
                if c["id"] not in seen:
                    seen.add(c["id"])
                    out.append(c)
        mine = [json.loads(p.read_text()) for p in self.case_dir.glob("*.json")]
        mine = [m for m in mine if device in m.get("device_profiles", [])]
        return {"closed_cases": out, "agent_cases": mine}

    def amount_band_scan(self, t0, t1, lo, hi):
        self._log("amount_band_scan")
        t = self.t
        m = (t.channel == "online") & (t.amount >= lo) & (t.amount < hi) & (t.ts >= t0) & (t.ts <= t1)
        return [norm_txn(r) for r in t[m].to_dict("records")]

    def device_ring_scan(self, t0, t1, min_cards, max_profile_txns):
        self._log("device_ring_scan")
        out = []
        for dev, idx in self.by_device.items():
            if len(idx) > max_profile_txns or len(dev) <= 12:
                continue
            rows = self._rows(self._window(idx, t0, t1))
            cards = {r["card_id"] for r in rows}
            if len(cards) >= min_cards:
                out.append({"id": dev, "n_txns": len(idx), "cards": sorted(cards), "n": len(rows),
                            "n_new": sum(r["device_status"] == "New" for r in rows),
                            "n_proxy": sum(bool(r["proxy"]) for r in rows),
                            "amount": sum(r["amount"] for r in rows),
                            "model_sum": sum(r["model_score"] for r in rows)})
        return out

    def similar_closed_cases(self, vec, k):
        self._log("similar_closed_cases")
        sc = sorted(((cosine(vec, e), i) for i, e in enumerate(self.cc_emb)), reverse=True)[:k]
        return [(self.cc[i], s) for s, i in sc]

    def similar_agent_cases(self, vec, k):
        self._log("similar_agent_cases")
        mine = [json.loads(p.read_text()) for p in self.case_dir.glob("*.json")]
        sc = sorted(((cosine(vec, m["emb"]), m["id"], m) for m in mine if m.get("emb")), reverse=True,
                    key=lambda x: x[0])[:k]
        return [(m, s) for s, _, m in sc]

    def policy_search(self, vec, k):
        self._log("policy_search")
        sc = sorted(((cosine(vec, c["emb"]), c["id"], c) for c in self.chunks), reverse=True, key=lambda x: x[0])[:k]
        return [({kk: vv for kk, vv in c.items() if kk != "emb"}, s) for s, _, c in sc]

    def write_case(self, record):
        self._log("write_case")
        (self.case_dir / f"{record['id']}.json").write_text(json.dumps(record, indent=1))
        return False  # not written to TigerGraph

    def graph_stats(self):
        return {"txns": len(self.t), "cards": len(self.cards), "device_profiles": len(self.by_device),
                "closed_cases": len(self.cc), "agent_cases": len(list(self.case_dir.glob('*.json'))),
                "policy_chunks": len(self.chunks)}


# =====================================================================================
def _vertices(results: list[dict], key: str | None = None) -> list[dict]:
    """Collect vertex dicts from a TigerGraph result list (optionally only under `key`)."""
    out = []
    for block in results:
        for k, v in block.items():
            if key and k != key:
                continue
            if isinstance(v, list):
                for item in v:
                    if isinstance(item, dict) and "attributes" in item:
                        a = dict(item["attributes"])
                        a.setdefault("id", item.get("v_id"))
                        a["_type"] = item.get("v_type")
                        out.append(a)
    return out


def _value(results: list[dict], key: str, default=None):
    for block in results:
        if key in block:
            return block[key]
    return default


VERTEX_PARAMS = {"x", "c", "cl", "d", "u", "f"}


class TigerGraphBackend(GraphBackend):
    name = "tigergraph"

    def __init__(self, s: Settings = SETTINGS):
        from agent.tg_client import TGClient
        self.s = s
        self.tg = TGClient(s)
        self.tg.token()
        self.transport_log = []
        self.mcp = None
        self.vector_mode = "native"
        if s.use_mcp:
            try:
                from agent.mcp_bridge import TigerGraphMCP
                self.mcp = TigerGraphMCP(s, token=self.tg._token)
            except Exception as e:  # MCP optional; REST fallback keeps the agent running
                self.transport_log.append(f"mcp_unavailable:{type(e).__name__}:{str(e)[:120]}")
        self._probe_vector_mode()

    def _probe_vector_mode(self):
        try:
            self.tg.run_query("policy_search", {"qv": [0.0] * 255 + [1.0], "k": 1})
            self.vector_mode = "native"
        except Exception:
            self.vector_mode = "fallback"

    def q(self, name: str, params: dict) -> list[dict]:
        if self.mcp is not None:
            try:
                t0 = time.time()
                # VERTEX<T> parameters are passed as 1-tuples (JSON lists) per pyTigerGraph's current convention
                mparams = {k: ([v] if k in VERTEX_PARAMS else v) for k, v in params.items()}
                res = self.mcp.run_installed_query(name, mparams)
                self.transport_log.append(f"mcp:{name}:{time.time() - t0:.2f}s")
                return res
            except Exception as e:
                self.transport_log.append(f"mcp_error:{name}:{str(e)[:80]}")
        t0 = time.time()
        res = self.tg.run_query(name, params)
        self.transport_log.append(f"restpp:{name}:{time.time() - t0:.2f}s")
        return res

    def txn_context(self, txn_id):
        r = self.q("txn_context", {"x": str(txn_id)})
        txn = norm_txn(_vertices(r, "txn")[0])
        cards = _vertices(r, "card")
        sibs = _vertices(r, "customer_cards")
        return {"txn": txn, "card": cards[0] if cards else {"id": txn["card_id"]},
                "customer": {"id": txn["customer_id"], "n_cards": len(sibs)}, "customer_cards": sibs}

    def card_window(self, card_id, t0, t1):
        rows = [norm_txn(v) for v in _vertices(self.q("card_window", {"c": card_id, "t0": t0, "t1": t1}), "txns")]
        return sorted(rows, key=lambda r: (r["ts"], r["id"]))

    def card_profile(self, card_id, before):
        r = self.q("card_profile", {"c": card_id, "before_t": before})
        keys = ["n", "amount_sum", "amount_max", "amounts", "products", "regions", "devices", "emails",
                "channels", "first_ts", "last_ts"]
        out = {k: _value(r, k) for k in keys}
        out["regions"] = {k.replace(".0", ""): v for k, v in (out.get("regions") or {}).items()}
        out["devices"] = {k: v for k, v in (out.get("devices") or {}).items() if k}
        return out

    def client_history(self, client_id):
        rows = [norm_txn(v) for v in _vertices(self.q("client_history", {"cl": client_id}), "txns")]
        return sorted(rows, key=lambda r: (r["ts"], r["id"]))

    def device_fanout(self, device, t0, t1):
        r = self.q("device_fanout", {"d": device.strip(), "t0": t0, "t1": t1})
        dev = _vertices(r, "device")
        rows = sorted((norm_txn(v) for v in _vertices(r, "txns")), key=lambda x: (x["ts"], x["id"]))
        return {"device": {"id": device, "n_txns": int((dev[0] if dev else {}).get("n_txns", len(rows)))},
                "txns": rows}

    def peer_txns(self, txn_id, t0, t1, lo, hi, use_email):
        r = self.q("peer_txns", {"x": str(txn_id), "t0": t0, "t1": t1, "amt_lo": lo, "amt_hi": hi,
                                 "use_email": bool(use_email)})
        return sorted((norm_txn(v) for v in _vertices(r, "peers")), key=lambda x: (x["ts"], x["id"]))

    def prior_cases(self, customer_id):
        r = self.q("prior_cases", {"u": customer_id})
        mine = []
        for v in _vertices(r, "agent_cases"):
            try:
                rec = json.loads(v.get("answer_json") or "{}").get("_memory", {})
                mine.append(rec or {"id": v["id"]})
            except json.JSONDecodeError:
                mine.append({"id": v["id"]})
        return {"closed_cases": [norm_cc(v) for v in _vertices(r, "closed_cases")],
                "connected_closed_cases": [norm_cc(v) for v in _vertices(r, "connected_closed_cases")],
                "agent_cases": mine}

    def device_cases(self, device):
        r = self.q("device_cases", {"d": device.strip()})
        mine = []
        for v in _vertices(r, "agent_cases"):
            try:
                mine.append(json.loads(v.get("answer_json") or "{}").get("_memory", {"id": v["id"]}))
            except json.JSONDecodeError:
                mine.append({"id": v["id"]})
        return {"closed_cases": [norm_cc(v) for v in _vertices(r, "closed_cases")], "agent_cases": mine}

    def amount_band_scan(self, t0, t1, lo, hi):
        return [norm_txn(v) for v in _vertices(self.q("amount_band_scan", {"t0": t0, "t1": t1, "lo": lo, "hi": hi}), "txns")]

    def device_ring_scan(self, t0, t1, min_cards, max_profile_txns):
        r = self.q("device_ring_scan", {"t0": t0, "t1": t1, "min_cards": min_cards, "max_profile_txns": max_profile_txns})
        out = []
        for v in _vertices(r, "rings"):
            g = lambda k, d=None: v.get(k, v.get("Devs." + k, d))  # noqa: E731  (projection keys are prefixed)
            out.append({"id": g("id") or v.get("id"), "n_txns": g("n_txns"), "cards": sorted(g("@cards", []) or []),
                        "n": g("@n", 0) or 0, "n_new": g("@n_new", 0) or 0, "n_proxy": g("@n_proxy", 0) or 0,
                        "amount": g("@amount", 0.0) or 0.0, "model_sum": g("@model_sum", 0.0) or 0.0})
        return out

    def _vector(self, query, vec, k):
        r = self.q(query, {"qv": vec, "k": k})
        hits = _vertices(r, "hits")
        dist = _value(r, "distances", {}) or {}
        out = []
        for h in hits:
            if "@score" in h:
                score = float(h["@score"])
            else:
                d = dist.get(h["id"], dist.get(str(h["id"])))
                score = 1.0 - float(d) if d is not None else 0.0
            out.append((h, score))
        return sorted(out, key=lambda x: -x[1])

    def similar_closed_cases(self, vec, k):
        return [(norm_cc(h), s) for h, s in self._vector("similar_closed_cases", vec, k)]

    def similar_agent_cases(self, vec, k):
        out = []
        for h, s in self._vector("similar_agent_cases", vec, k):
            try:
                rec = json.loads(h.get("answer_json") or "{}").get("_memory", {"id": h["id"]})
            except json.JSONDecodeError:
                rec = {"id": h["id"]}
            out.append((rec, s))
        return out

    def policy_search(self, vec, k):
        return [({kk: h.get(kk) for kk in ("id", "doc", "section", "text")}, s)
                for h, s in self._vector("policy_search", vec, k)]

    def write_case(self, record):
        from agent.case_store import to_upsert
        v, e = to_upsert(record, vector_mode=self.vector_mode)
        self.tg.upsert(vertices=v, edges=e)
        self.transport_log.append("restpp:upsert_case")
        return True

    def graph_stats(self):
        r = self.q("graph_stats", {})
        return r[0] if r else {}


def get_backend(s: Settings = SETTINGS) -> GraphBackend:
    if s.backend == "tigergraph":
        return TigerGraphBackend(s)
    return LocalBackend(s)
