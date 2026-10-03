"""
Case memory writer. A finished investigation becomes a FraudCase vertex connected to everything it names:

  FraudCase -FC_TXN(role)-> Txn               flagged / affected transactions
  FraudCase -FC_CARD(role)-> Card             subject card / connected cards
  FraudCase -FC_DEVICE-> DeviceProfile        devices linking the case to other cards
  FraudCase -FC_PATTERN-> Pattern             typology (incl. `undocumented`)
  FraudCase -FC_SIMILAR_CC(score)-> ClosedCase   memory the agent retrieved and used
  FraudCase -FC_SIMILAR_FC(score)-> FraudCase    earlier agent cases it resembles
  FraudCase -FC_EVENT-> CaseEvent             the full audit trail (every step, request, decision)

The FraudCase also carries a vector embedding, so the next investigation finds it by similarity as well as
by graph adjacency (a case that names a device becomes evidence for the next analyst).
"""
from __future__ import annotations

import json


def memory_digest(record: dict) -> dict:
    keys = ["id", "source_case_id", "card_ids", "device_profiles", "pattern", "verdict", "fraud_probability",
            "exposure_usd", "summary", "final_actions", "opened_at", "status", "sar_filed"]
    return {k: record.get(k) for k in keys}


def to_upsert(record: dict, vector_mode: str = "native") -> tuple[dict, dict]:
    cid = record["id"]
    answer = dict(record.get("answer", {}))
    answer["_memory"] = memory_digest(record)
    fc_attrs = {
        "source_case_id": record["source_case_id"], "trigger_type": record["trigger_type"],
        "opened_at": record["opened_at"], "updated_at": record["updated_at"], "status": record["status"],
        "verdict": record["verdict"], "fraud_probability": float(record["fraud_probability"]),
        "pattern": record["pattern"], "exposure_usd": float(record["exposure_usd"]), "summary": record["summary"],
        "sar_filed": bool(record["sar_filed"]), "final_actions": "|".join(record["final_actions"]),
        "answer_json": json.dumps(answer, default=str),
    }
    v = {"FraudCase": {cid: {k: {"value": val} for k, val in fc_attrs.items()}}, "CaseEvent": {}, "Pattern": {}}
    v["FraudCase"][cid]["emb" if vector_mode == "native" else "emb_list"] = {"value": record["emb"]}
    e: dict = {"FraudCase": {cid: {}}}
    out = e["FraudCase"][cid]

    def link(etype, ttype, tid, attrs=None):
        out.setdefault(etype, {}).setdefault(ttype, {})[str(tid)] = {k: {"value": x} for k, x in (attrs or {}).items()}

    link("FC_TXN", "Txn", record["flagged_txn_id"], {"role": "flagged"})
    for t in record.get("affected_txn_ids", []):
        if t != record["flagged_txn_id"]:
            link("FC_TXN", "Txn", t, {"role": "affected"})
    link("FC_CARD", "Card", record["card_id"], {"role": "subject"})
    for c in record.get("connected_card_ids", []):
        link("FC_CARD", "Card", c, {"role": "connected"})
    for d in record.get("device_profiles", []):
        if d.strip():
            link("FC_DEVICE", "DeviceProfile", d.strip())
    v["Pattern"][record["pattern"]] = {}
    link("FC_PATTERN", "Pattern", record["pattern"])
    for cc, score in record.get("similar_closed", []):
        link("FC_SIMILAR_CC", "ClosedCase", cc, {"score": float(score)})
    for fc, score in record.get("similar_agent", []):
        if fc != cid:
            link("FC_SIMILAR_FC", "FraudCase", fc, {"score": float(score)})
    for ev in record.get("events", []):
        eid = f"{cid}#{ev['step']:02d}"
        v["CaseEvent"][eid] = {"case_id": {"value": cid}, "step": {"value": int(ev["step"])},
                               "ts": {"value": ev["ts"]}, "kind": {"value": ev["kind"]},
                               "title": {"value": ev["title"][:500]},
                               "detail": {"value": json.dumps(ev.get("detail", ""), default=str)[:20000]}}
        link("FC_EVENT", "CaseEvent", eid)
    return v, e
