"""
View model for the SentinelGraph console: turns an answer file + its investigation trace into the handful of
numbers and sentences an analyst needs on the first screen. Nothing here changes a decision; it only explains
the decision the agent already recorded.

Definitions shown in the UI (kept identical everywhere):
  fraud probability   the agent's posterior after fusing all evidence (answer file `fraud_probability`)
  prior               the case-memory model's calibrated fraud rate for this transaction, before graph evidence
  bank risk score     the bank's own model score that raised the alert (an input, never a verdict)
  evidence            independent lines of evidence that agree with the outcome (policy section 6 needs two)
  confidence          how settled the outcome is: half from how decisive the probability is (1 - binary
                      entropy), half from whether the two-independent-lines requirement is met
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from agent.policy_config import POLICY

STOP_LO, STOP_HI = POLICY.t.legit_stop, POLICY.t.fraud_stop
NEED = POLICY.t.min_independent_evidence
ORDER_HEADLINE = ["BLOCK_ALL_CARDS", "BLOCK_CARD", "DECLINE_TRANSACTION", "STEP_UP_AUTH", "VERIFY_WITH_CUSTOMER",
                  "ALLOW_TRANSACTION", "CLOSE_NO_FRAUD", "ESCALATE_TO_ANALYST", "MONITOR_CARD", "CREATE_CASE",
                  "FILE_REPORT", "MONITOR_CONNECTED_CARDS", "WARN_CUSTOMER", "GENERATE_REPORT"]
ROUTE_WHO = {"auto": "Not required · agent executes", "L1": "Team lead (L1)", "L2": "Fraud manager (L2)"}
FAMILY = {"ml": "Case-memory model", "trigger": "Alert trigger", "sequence": "Transaction sequence",
          "network": "Other cards in the graph", "device": "Device & connection", "behaviour": "Cardholder behaviour",
          "history": "Case history", "customer": "Customer reply"}
ACTION_TEXT = {
    "ALLOW_TRANSACTION": "Allow the transaction", "DECLINE_TRANSACTION": "Decline the pending authorisation",
    "MONITOR_CARD": "Monitor the card for 72h", "MONITOR_CONNECTED_CARDS": "Monitor the connected cards",
    "WARN_CUSTOMER": "Send the customer a notice", "VERIFY_WITH_CUSTOMER": "Verify with the customer",
    "STEP_UP_AUTH": "Request step-up authentication", "BLOCK_CARD": "Block and reissue the card",
    "BLOCK_ALL_CARDS": "Block all the customer's cards", "GENERATE_REPORT": "Write an internal report",
    "CREATE_CASE": "Open a fraud case", "FILE_REPORT": "File a suspicious activity report",
    "ESCALATE_TO_ANALYST": "Escalate to a fraud analyst", "CLOSE_NO_FRAUD": "Close as no fraud"}
SOURCE_OF_REF = [("txn.model_score", "MEMORY MODEL"), ("similar_closed", "CASE MEMORY"), ("prior_cases", "CASE MEMORY"),
                 ("device_cases", "CASE MEMORY"), ("similar_agent", "CASE MEMORY"), ("policy", "GRAPHRAG"),
                 ("trigger", "TRIGGER"), ("evidence_request", "CUSTOMER"), ("llm", "LLM"), ("", "TIGERGRAPH")]


def entropy(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return -(p * math.log2(p) + (1 - p) * math.log2(1 - p))


def source_badge(ref: str, source: str) -> str:
    if source == "customer" and "trigger" not in ref:
        return "CUSTOMER"
    for key, label in SOURCE_OF_REF:
        if key in ref:
            return label
    return "TIGERGRAPH"


def risk_level(p: float) -> str:
    if p <= STOP_LO:
        return "Low"
    if p < 0.5:
        return "Moderate"
    if p < STOP_HI:
        return "Elevated"
    return "High"


def conf_level(c: float) -> str:
    return "High" if c >= 0.8 else ("Medium" if c >= 0.55 else "Low")


@dataclass
class Assessment:
    p: float
    support: int
    direction: str
    sufficiency: float
    confidence: float
    threshold_met: bool
    contra: int = 0


def assess(p: float, families: dict, prior: float, direction: str | None = None, conflicted: bool = False) -> Assessment:
    direction = direction or ("fraud" if p >= 0.5 else "legit")
    thr = math.log(1.5)
    n = sum(1 for v in families.values() if (v >= thr if direction == "fraud" else v <= -thr))
    contra = sum(1 for v in families.values() if (v <= -thr if direction == "fraud" else v >= thr))
    if (direction == "legit" and prior <= 0.03) or (direction == "fraud" and prior >= 0.30):
        n += 1  # the memory model counts as an independent line when it already agrees
    suff = min(n, NEED) / NEED
    if conflicted:  # lines that point the other way cancel agreeing ones
        suff = max(0, min(n, NEED) - min(contra, NEED)) / NEED
    conf = 0.5 * (1 - entropy(p)) + 0.5 * suff
    met = (p <= STOP_LO or p >= STOP_HI) and n >= NEED and not conflicted
    return Assessment(p, n, direction, suff, conf, met, contra)


@dataclass
class View:
    case: dict
    answer: dict
    events: list
    prior: float = 0.0
    p_initial: float = 0.0
    p_final: float = 0.0
    bank_score: float | None = None
    initial: Assessment | None = None
    final: Assessment | None = None
    headline: dict = field(default_factory=dict)
    gaps_open: list = field(default_factory=list)
    gaps_resolved: list = field(default_factory=list)
    flagged: dict = field(default_factory=dict)


def build(case: dict, answer: dict, events: list) -> View:
    v = View(case, answer, events)
    assessments = [e["detail"] for e in events if e["kind"] == "assess" and isinstance(e.get("detail"), dict)]
    a0 = assessments[0] if assessments else {}
    af = assessments[-1] if assessments else {}
    v.prior = float(a0.get("prior", 0.0))
    v.p_initial = float(a0.get("posterior", answer["case"]["fraud_probability"]))
    v.p_final = float(af.get("posterior", v.p_initial)) if len(assessments) > 1 else v.p_initial
    trig = next((e for e in events if e["kind"] == "trigger"), {})
    rs = (trig.get("detail") or {}).get("risk_score")
    try:
        v.bank_score = float(rs) if rs not in (None, "") else None
        if v.bank_score is not None and math.isnan(v.bank_score):
            v.bank_score = None
    except (TypeError, ValueError):
        v.bank_score = None
    verdict = answer["case"]["verdict"]
    fdir = "fraud" if verdict == "fraud" else ("legit" if verdict == "legitimate" else None)
    v.initial = assess(v.p_initial, a0.get("log_lr_by_family") or {}, v.prior)
    v.final = assess(v.p_final, af.get("log_lr_by_family") or {}, v.prior, fdir, conflicted=verdict == "uncertain")
    acts = answer["next_best_actions"]["final"]
    v.headline = sorted(acts, key=lambda a: ORDER_HEADLINE.index(a["action"]) if a["action"] in ORDER_HEADLINE else 99)[0] \
        if acts else {}
    gaps = a0.get("uncertainty") or []
    asked = bool(answer["evidence_requests"])
    for g in gaps:
        if asked and "Cardholder has not confirmed" in g:
            v.gaps_resolved.append(g)
        else:
            v.gaps_open.append(g)
    ctx = next((e for e in events if e["kind"] == "tool" and e["title"].startswith("txn_context")), None)
    v.flagged = {"id": case.get("flagged_txn_id")}
    return v


def finding_sentence(v: View, evidence: list[dict]) -> str:
    c = v.answer["case"]
    if c["verdict"] == "legitimate":
        legit = [x for x in evidence if x.get("direction") == "legit"]
        lead = legit[0]["claim"] if legit else "Behaviour consistent with the cardholder's history."
        return "No material fraud indicators. " + short(lead, 110)
    pat = c["pattern"].replace("_", " ")
    fr = sorted([x for x in evidence if x.get("direction") == "fraud"], key=lambda x: -(x.get("lr") or 0))
    lead = fr[0]["claim"] if fr else ""
    extra = f" Linked to {len(c['connected_card_ids'])} other cards." if c["connected_card_ids"] else ""
    return (f"{pat.capitalize()}." if c["verdict"] == "fraud" else f"Conflicting evidence ({pat}).") + \
        (" " + short(lead, 100) if lead else "") + extra


def short(s: str, n: int) -> str:
    s = s.strip()
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0]
    return cut.rstrip(",;:") + "…"


def why_stopped(v: View) -> tuple[str, list[str], str]:
    """Title, bullet list, closing line for the 'why did the agent stop / continue' panel."""
    a = v.answer
    c = a["case"]
    req = a["evidence_requests"]
    f = v.final
    pct = f"{v.p_final * 100:.1f}%"
    if c["status"] in ("escalated", "open") and c["verdict"] == "uncertain":
        items = [f"Fraud probability {pct} sits between the stop thresholds (≤15% / ≥85%)",
                 f"Evidence points in different directions ({f.support} line(s) for fraud, {f.contra} for legitimate use)",
                 "More automated steps would not resolve the conflict; it needs human judgement"]
        return ("Why the agent handed over", items,
                "Policy R8: uncertain and conflicting, so a human analyst decides. Protective actions are already queued.")
    items = []
    if req:
        items.append(f"The requested evidence ({req[0]['type'].replace('_', ' ')}) came back and settled the question")
    if f.threshold_met:
        side = "below the legitimate threshold (≤15%)" if v.p_final <= STOP_LO else "above the fraud threshold (≥85%)"
        items.append(f"Fraud probability {pct} is {side}")
        items.append(f"{f.support} independent lines of evidence agree (policy needs 2)")
    else:
        items.append(f"Fraud probability {pct}; the policy path for the reply is deterministic")
    items.append("No open question left that would change the permitted action" if not v.gaps_open or f.threshold_met
                 else "Open questions remain but would not change the permitted action")
    return ("Why the agent stopped", items, "Policy section 6 · further investigation is unlikely to change the decision.")
