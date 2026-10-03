"""
Controlled evidence gathering (policy section 5).

In this round customer and analyst replies are not provided, so the agent simulates them. The simulator is
deliberately *evidence-consistent and transparent*: the reply it assumes is the one the graph evidence makes
most likely, it never looks at anything the agent could not see, and the assumption plus its basis is written
into `evidence_requests[].assumed_response`. Swapping this class for a real channel (SMS, app push, analyst
queue) requires no change to the agent.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Reply:
    kind: str          # denied | confirmed | no_reply
    text: str
    lr: float          # likelihood ratio applied to the fraud odds
    real: bool = False          # a reply that actually came back, not an assumption
    channel: str = ""           # phone, sms, app, branch, email, timeout ...
    recorded_by: str = ""
    received_at: str = ""


REPLY_LR = {"denied": 8.0, "confirmed": 0.03, "no_reply": 1.0}
CHANNELS = ("phone", "sms", "app", "whatsapp", "email", "branch", "otp", "timeout")


def real_reply(kind: str, text: str = "", channel: str = "", recorded_by: str = "", received_at: str = "") -> Reply:
    """A reply that actually arrived (console, API) or a timeout. Same weights as the simulator uses."""
    if kind not in REPLY_LR:
        raise ValueError("reply must be denied, confirmed or no_reply")
    if channel and channel not in CHANNELS:
        raise ValueError(f"channel must be one of {', '.join(CHANNELS)}")
    from agent.safety import clean, mask
    return Reply(kind, mask(clean(text, 500).strip())[0], REPLY_LR[kind], True, channel, recorded_by, received_at)


def reply_mode() -> str:
    """wait: never assume a reply. Client data always waits; the benchmark and the scenario lab simulate."""
    import os
    if os.getenv("DATA_SOURCE", "both").lower() == "client":
        return "wait"          # never simulated on a client's own data, whatever else is set
    return "wait" if os.getenv("REPLY_MODE", "").lower() == "wait" else "simulate"


def simulate(request: str, trigger: str, p_all: float, p_graph: float, legit_families: int,
             recurring: bool, details: dict) -> Reply:
    amt = details.get("amount", 0.0)
    txn = details.get("txn_id", "")
    if trigger == "customer_report":
        if recurring and p_graph < 0.5:
            return Reply("confirmed",
                         f"Assumed: shown the {details.get('cadence', 'recurring')} history of identical ${amt:.2f} charges, the "
                         f"cardholder recognises the recurring merchant charge and withdraws the dispute. Basis: the "
                         f"recurring pattern in the graph (R7).", 0.03)
        if p_graph <= 0.02 and legit_families >= 3:
            return Reply("confirmed",
                         f"Assumed: shown the merchant details for transaction {txn}, the cardholder recognises the "
                         f"purchase. Basis: graph evidence strongly indicates the cardholder's own activity "
                         f"(graph-only probability {p_graph:.2f}).", 0.03)
        basis = ("the cardholder's own report, consistent with the graph evidence" if p_graph >= 0.2 else
                 "the cardholder's own report, although the graph evidence alone points to normal use")
        return Reply("denied",
                     f"Assumed: the cardholder confirms they still hold the card and did not make or authorise "
                     f"transaction {txn} (${amt:.2f}). Basis: {basis} (graph-only probability {p_graph:.2f}).", 8.0)
    if request == "step_up_auth":
        if p_all < 0.35:
            return Reply("confirmed", f"Assumed: the cardholder passes step-up authentication (one-time passcode) and "
                                      f"confirms transaction {txn}. Basis: fraud probability {p_all:.2f}; the bank's memory "
                                      f"shows score-only alerts like this are usually confirmed (new phone, travel).", 0.03)
        if p_all > 0.6:
            return Reply("denied", f"Assumed: step-up authentication fails and the cardholder, reached by phone, denies "
                                   f"transaction {txn}. Basis: fraud probability {p_all:.2f}.", 8.0)
        return Reply("no_reply", f"Assumed: no passcode entered and no reply within 24 hours. Basis: probability "
                                 f"{p_all:.2f} leaves the outcome open.", 1.0)
    if p_all < 0.35:
        return Reply("confirmed", f"Assumed: the cardholder confirms transaction {txn} (${amt:.2f}) as their own. Basis: "
                                  f"fraud probability {p_all:.2f}; in the bank's memory all 900 score-only alerts were "
                                  f"confirmed by the cardholder.", 0.03)
    if p_all > 0.6:
        return Reply("denied", f"Assumed: the cardholder does not recognise transaction {txn} (${amt:.2f}) and still "
                               f"holds the card. Basis: fraud probability {p_all:.2f} from independent graph evidence.", 8.0)
    return Reply("no_reply", f"Assumed: no reply within 24 hours. Basis: probability {p_all:.2f} leaves the outcome "
                             f"open, so the policy's no-reply path (R4) is exercised.", 1.0)
