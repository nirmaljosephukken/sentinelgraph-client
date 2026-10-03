"""
Real-world outcomes and how the agent compares with them.

An outcome is what actually happened to an alert: confirmed fraud (chargeback, cardholder confirmed, police report)
or legitimate (cardholder confirmed, false alarm). Outcomes come from the console, a file upload or the API.
metrics() compares them with the agent's verdicts and checks the go-live criteria in config/policy.yaml.
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone

from agent import access as A
from agent.policy_config import POLICY

OUTCOMES = ("fraud", "legitimate")
SYNONYMS = {"fraud": "fraud", "confirmed_fraud": "fraud", "chargeback": "fraud", "fraudulent": "fraud", "f": "fraud",
            "1": "fraud", "true": "fraud", "yes": "fraud",
            "legitimate": "legitimate", "legit": "legitimate", "genuine": "legitimate", "cleared": "legitimate",
            "false_positive": "legitimate", "not_fraud": "legitimate", "no_fraud": "legitimate", "l": "legitimate",
            "0": "legitimate", "false": "legitimate", "no": "legitimate"}
BUCKETS = [(0.0, 0.15), (0.15, 0.30), (0.30, 0.70), (0.70, 0.85), (0.85, 1.0001)]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalise(value) -> str | None:
    return SYNONYMS.get(str(value or "").strip().lower().replace(" ", "_").replace("-", "_"))


def record(alert_id: str, outcome: str, actor: dict, source: str = "console", note: str = "") -> dict:
    from agent import live
    o = normalise(outcome)
    if o is None:
        raise ValueError(f"outcome must be fraud or legitimate, got {outcome!r}")
    doc = live.store().get(alert_id)
    if not doc:
        raise ValueError(f"no alert {alert_id}")
    prev = A.get_doc("outcomes", alert_id)
    from agent.safety import clean, mask
    rec = {"alert_id": alert_id, "outcome": o, "note": mask(clean(note, 300))[0], "source": source,
           "recorded_by": actor.get("email", "?"), "recorded_at": _now(),
           "agent_verdict": doc.get("verdict"), "agent_probability": doc.get("fraud_probability")}
    if prev and prev.get("outcome") != o:
        rec["previous"] = f"{prev.get('outcome')} by {prev.get('recorded_by')} at {prev.get('recorded_at')}"
    A.put_doc("outcomes", alert_id, rec)
    A.audit(actor.get("email", "?"), actor.get("role", "?"), "outcome_recorded", alert_id,
            {"outcome": o, "source": source, "agent_verdict": doc.get("verdict"), **({"changed_from": prev.get("outcome")}
                                                                                     if prev else {})})
    return rec


def bulk(rows: list[dict], actor: dict, source: str) -> dict:
    """rows: [{alert_id, outcome, note?}] (column names are matched loosely). Returns counts and row errors."""
    ok, errors = 0, []
    for n, r in enumerate(rows, start=2):
        low = {str(k).strip().lower(): v for k, v in r.items()}
        aid = str(low.get("alert_id") or low.get("alert") or low.get("case_id") or "").strip()
        out = low.get("outcome") or low.get("result") or low.get("label")
        try:
            if not aid:
                raise ValueError("alert_id missing")
            record(aid, out, actor, source, str(low.get("note") or ""))
            ok += 1
        except ValueError as ex:
            errors.append({"row": n, "alert_id": aid, "problem": str(ex)})
    return {"recorded": ok, "rejected": len(errors), "errors": errors[:500]}


def outcome_map() -> dict[str, dict]:
    return {d["id"]: d for d in A.list_docs("outcomes")}


def _median(xs):
    return round(statistics.median(xs), 1) if xs else None


def _secs(a: str, b: str) -> float | None:
    try:
        f = "%Y-%m-%dT%H:%M:%SZ"
        return (datetime.strptime(b, f) - datetime.strptime(a, f)).total_seconds()
    except (TypeError, ValueError):
        return None


def metrics(cases: list[dict] | None = None, outcomes: dict | None = None) -> dict:
    from agent import live
    cases = cases if cases is not None else live.store().recent(100000)
    outcomes = outcomes if outcomes is not None else outcome_map()
    done = [c for c in cases if c.get("status") == "done"]
    labelled = [(c, outcomes[c["alert_id"]]["outcome"]) for c in done if c.get("alert_id") in outcomes]
    verdicts = ("fraud", "uncertain", "legitimate")
    matrix = {v: {o: 0 for o in OUTCOMES} for v in verdicts}
    for c, o in labelled:
        if c.get("verdict") in matrix:
            matrix[c["verdict"]][o] += 1
    called_fraud = sum(matrix["fraud"].values())
    called_legit = sum(matrix["legitimate"].values())
    real_fraud = sum(matrix[v]["fraud"] for v in verdicts)
    precision = matrix["fraud"]["fraud"] / called_fraud if called_fraud else None
    missed = matrix["legitimate"]["fraud"] / called_legit if called_legit else None
    recall = matrix["fraud"]["fraud"] / real_fraud if real_fraud else None
    handed = sum(matrix["uncertain"].values()) / len(labelled) if labelled else None
    calib = []
    for lo, hi in BUCKETS:
        xs = [o for c, o in labelled if lo <= float(c.get("fraud_probability") or 0) < hi]
        calib.append({"range": f"{lo:.2f} to {min(hi, 1):.2f}", "cases": len(xs),
                      "observed_fraud": (sum(1 for o in xs if o == "fraud") / len(xs)) if xs else None})
    by_pattern: dict[str, dict] = {}
    for c, o in labelled:
        p = by_pattern.setdefault(c.get("pattern") or "none", {"cases": 0, "fraud": 0, "agent_fraud": 0, "agent_right": 0})
        p["cases"] += 1
        p["fraud"] += o == "fraud"
        p["agent_fraud"] += c.get("verdict") == "fraud"
        p["agent_right"] += (c.get("verdict") == "fraud" and o == "fraud") or (c.get("verdict") == "legitimate" and o == "legitimate")
    stopped = sum(float(c.get("exposure_usd") or 0) for c, o in labelled if c.get("verdict") == "fraud" and o == "fraud")
    waits = []
    for c in done:
        if c.get("asked_at") and c.get("reply"):
            try:
                waits.append(_secs(c["asked_at"], json.loads(c["reply"]).get("received_at")) or 0)
            except ValueError:
                pass
    decisions = A.list_docs("decisions")
    agreed = sum(1 for d in decisions if d.get("decision") == "agreed")
    disagreed = sum(1 for d in decisions if d.get("decision") == "disagreed")
    gl = POLICY.go_live
    checks = [
        {"check": "Cases with a confirmed outcome", "value": len(labelled), "target": f"at least {gl['min_outcomes']}",
         "ok": len(labelled) >= gl["min_outcomes"]},
        {"check": "Fraud verdicts that were fraud (precision)", "value": precision,
         "target": f"at least {gl['min_fraud_precision']:.0%}", "ok": precision is not None and precision >= gl["min_fraud_precision"]},
        {"check": "Legitimate verdicts that were fraud (missed)", "value": missed,
         "target": f"at most {gl['max_missed_fraud']:.0%}", "ok": missed is not None and missed <= gl["max_missed_fraud"]},
    ]
    return {"cases": len(done), "awaiting_reply": sum(1 for c in cases if c.get("status") == "awaiting_reply"),
            "with_outcome": len(labelled), "matrix": matrix, "precision": precision, "recall": recall,
            "missed_fraud": missed, "handed_to_people": handed, "calibration": calib, "by_pattern": by_pattern,
            "exposure_stopped": round(stopped, 2), "median_seconds": _median([float(c["seconds"]) for c in done if c.get("seconds")]),
            "median_reply_hours": _median([w / 3600 for w in waits]), "agreed": agreed, "disagreed": disagreed,
            "agreement": agreed / (agreed + disagreed) if agreed + disagreed else None,
            "checks": checks, "ready": all(c["ok"] for c in checks), "mode": POLICY.mode}
