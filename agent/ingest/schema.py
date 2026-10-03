"""
SentinelGraph's own field names. A client file is mapped onto these (config/mappings/*.yaml), then checked and loaded.

Only four transaction fields are required. Every other field improves an investigation when present and is simply
skipped by the detectors when absent: no device column means no device-sharing evidence, no email column means no
email-link evidence, and so on. The data-quality report says which detectors a file can feed.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    name: str
    kind: str            # text | number | time | choice
    required: bool = False
    label: str = ""
    feeds: str = ""      # which evidence the field enables
    choices: tuple = ()


TXN_FIELDS = [
    Field("id", "text", True, "Transaction id", "every query (must be unique)"),
    Field("ts", "time", True, "Transaction time", "velocity, sequences, timelines"),
    Field("amount", "number", True, "Amount", "exposure, card testing, structuring, recurring charges"),
    Field("card_id", "text", True, "Card id or token", "card history and linked cards"),
    Field("customer_id", "text", False, "Customer id", "prior cases, block-all-cards rule (defaults to the card id)"),
    Field("channel", "choice", False, "Channel", "online vs in-person rules", ("online", "in_person")),
    Field("product", "text", False, "Product or merchant category", "recurring charges"),
    Field("risk_score", "number", False, "Bank risk score (0 to 1)", "alert context; never a verdict"),
    Field("model_score", "number", False, "Case-memory model score (0 to 1)", "starting probability"),
    Field("addr1", "text", False, "Billing region", "out-of-region use, home region"),
    Field("addr2", "text", False, "Country", "context"),
    Field("dist1", "number", False, "Distance from home", "out-of-region use"),
    Field("p_email", "text", False, "Purchaser email domain", "email links between cards"),
    Field("r_email", "text", False, "Recipient email domain", "email links between cards"),
    Field("device_status", "choice", False, "Device seen before", "new-device evidence", ("New", "Found", "")),
    Field("proxy", "text", False, "Proxy or anonymiser flag", "anonymous-proxy evidence"),
    Field("device_profile", "text", False, "Device fingerprint", "device sharing and device rings"),
    Field("device_type", "choice", False, "Device type", "context", ("mobile", "desktop", "")),
    Field("client_id", "text", False, "Cardholder identity key", "identity history (defaults to card|region|0)"),
    Field("network", "text", False, "Card network", "context"),
    Field("card_type", "text", False, "Card type", "context"),
    Field("m4", "text", False, "Match flag M4", "IEEE-CIS style datasets only"),
    Field("m6", "text", False, "Match flag M6", "IEEE-CIS style datasets only"),
    Field("d1", "number", False, "Days since card first seen", "card age"),
]
TXN = {f.name: f for f in TXN_FIELDS}

ALERT_FIELDS = [
    Field("case_id", "text", True, "Alert id"),
    Field("flagged_txn_id", "text", True, "Flagged transaction id"),
    Field("trigger_type", "choice", False, "Why the alert was raised",
          choices=("risk_score", "customer_report", "analyst_request")),
    Field("trigger_text", "text", False, "Alert text"),
    Field("opened_at", "time", False, "Alert time (defaults to now)"),
    Field("card_id", "text", False, "Card id (defaults to the transaction's card)"),
    Field("customer_id", "text", False, "Customer id (defaults to the transaction's customer)"),
    Field("risk_score", "number", False, "Bank risk score (defaults to the transaction's)"),
]
ALERT = {f.name: f for f in ALERT_FIELDS}

TS_FMT = "%Y-%m-%d %H:%M:%S"
