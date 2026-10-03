"""
Decision policy loaded from config/policy.yaml (or the file named by POLICY_FILE).

The file is validated when it loads. A bad policy stops the app from starting rather than letting the agent run
with numbers nobody approved. `POLICY.fingerprint` is a short hash of the effective settings, recorded on every
case so an auditor can tell exactly which policy produced a decision.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FILE = ROOT / "config" / "policy.yaml"

ACTIONS = ["DECLINE_TRANSACTION", "BLOCK_CARD", "BLOCK_ALL_CARDS", "ALLOW_TRANSACTION", "CREATE_CASE",
           "STEP_UP_AUTH", "VERIFY_WITH_CUSTOMER", "MONITOR_CARD", "FILE_REPORT", "MONITOR_CONNECTED_CARDS",
           "ESCALATE_TO_ANALYST", "WARN_CUSTOMER", "GENERATE_REPORT", "CLOSE_NO_FRAUD"]

THRESHOLD_KEYS = ["fraud_stop", "legit_stop", "min_independent_evidence", "hold_authorisation", "open_case",
                  "monitor_connected_cards", "step_up_below"]
MONEY_KEYS = ["block_card_team_lead_max", "report_over", "escalate_over", "card_testing_large_purchase"]


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Thresholds:
    fraud_stop: float
    legit_stop: float
    min_independent_evidence: int
    hold_authorisation: float
    open_case: float
    monitor_connected_cards: float
    step_up_below: float


@dataclass(frozen=True)
class Money:
    block_card_team_lead_max: float
    report_over: float
    escalate_over: float
    card_testing_large_purchase: float


@dataclass(frozen=True)
class Policy:
    name: str
    version: str
    effective_from: str
    approved_by: str
    currency: str
    symbol: str
    t: Thresholds
    money: Money
    auto: frozenset
    l1: frozenset
    l2: frozenset
    source: str
    fingerprint: str
    unscored_prior: float = 0.035
    mode: str = "shadow"
    reply_wait_hours: float = 24.0
    go_live: dict = None

    @property
    def shadow(self) -> bool:
        return self.mode == "shadow"

    def fmt(self, amount: float, cents: bool = False) -> str:
        return f"{self.symbol}{amount:,.2f}" if cents else f"{self.symbol}{amount:,.0f}"

    @property
    def label(self) -> str:
        return f"{self.name} v{self.version}"

    def summary(self) -> dict:
        return {"name": self.name, "version": self.version, "effective_from": self.effective_from,
                "approved_by": self.approved_by, "fingerprint": self.fingerprint, "mode": self.mode}


def _num(section: dict, key: str, where: str) -> float:
    if key not in section:
        raise PolicyError(f"{where}.{key} is missing")
    v = section[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise PolicyError(f"{where}.{key} must be a number, got {v!r}")
    return float(v)


def parse(raw: dict, source: str = "<memory>") -> Policy:
    if not isinstance(raw, dict):
        raise PolicyError("policy file is empty or not a mapping")
    meta, cur = raw.get("policy") or {}, raw.get("currency") or {}
    th, mo, ro = raw.get("thresholds") or {}, raw.get("money") or {}, raw.get("routes") or {}

    t = {k: _num(th, k, "thresholds") for k in THRESHOLD_KEYS}
    for k, v in t.items():
        if k != "min_independent_evidence" and not 0 <= v <= 1:
            raise PolicyError(f"thresholds.{k} must be between 0 and 1, got {v}")
    if t["min_independent_evidence"] < 1 or t["min_independent_evidence"] != int(t["min_independent_evidence"]):
        raise PolicyError("thresholds.min_independent_evidence must be a whole number of at least 1")
    order = ["legit_stop", "open_case", "hold_authorisation", "fraud_stop"]
    for a, b in zip(order, order[1:]):
        if not t[a] < t[b]:
            raise PolicyError(f"thresholds must satisfy legit_stop < open_case < hold_authorisation < fraud_stop "
                              f"({a}={t[a]} is not below {b}={t[b]})")
    m = {k: _num(mo, k, "money") for k in MONEY_KEYS}
    for k, v in m.items():
        if v < 0:
            raise PolicyError(f"money.{k} cannot be negative")

    groups = {}
    for name in ("auto", "L1", "L2"):
        vals = ro.get(name) or []
        unknown = [a for a in vals if a not in ACTIONS]
        if unknown:
            raise PolicyError(f"routes.{name} lists unknown action(s): {', '.join(map(str, unknown))}")
        groups[name] = frozenset(vals)
    if "BLOCK_CARD" in groups["auto"] | groups["L1"] | groups["L2"]:
        raise PolicyError("BLOCK_CARD is routed by exposure; leave it out of routes")
    seen = {}
    for name, vals in groups.items():
        for a in vals:
            if a in seen:
                raise PolicyError(f"{a} appears in both routes.{seen[a]} and routes.{name}")
            seen[a] = name
    missing = [a for a in ACTIONS if a != "BLOCK_CARD" and a not in seen]
    if missing:
        raise PolicyError(f"no route for: {', '.join(missing)}")
    for a in ("BLOCK_ALL_CARDS", "FILE_REPORT", "DECLINE_TRANSACTION"):
        if a in groups["auto"]:
            raise PolicyError(f"{a} cannot be executed automatically; it needs human approval")

    model = raw.get("model") or {}
    prior = _num(model, "unscored_prior", "model") if "unscored_prior" in model else 0.035
    if not 0 < prior < t["open_case"]:
        raise PolicyError(f"model.unscored_prior must be above 0 and below thresholds.open_case, got {prior}")
    ops = raw.get("operations") or {}
    mode = str(ops.get("mode", "shadow"))
    if mode not in ("shadow", "assist"):
        raise PolicyError(f"operations.mode must be shadow or assist, got {mode!r}")
    wait = _num(ops, "reply_wait_hours", "operations") if "reply_wait_hours" in ops else 24.0
    if not 0 < wait <= 24 * 14:
        raise PolicyError("operations.reply_wait_hours must be between 0 and 336")
    gl_raw = raw.get("go_live") or {}
    gl = {"min_outcomes": int(_num(gl_raw, "min_outcomes", "go_live")) if "min_outcomes" in gl_raw else 200,
          "min_fraud_precision": _num(gl_raw, "min_fraud_precision", "go_live") if "min_fraud_precision" in gl_raw else 0.9,
          "max_missed_fraud": _num(gl_raw, "max_missed_fraud", "go_live") if "max_missed_fraud" in gl_raw else 0.02}
    for k in ("min_fraud_precision", "max_missed_fraud"):
        if not 0 <= gl[k] <= 1:
            raise PolicyError(f"go_live.{k} must be between 0 and 1")
    t["min_independent_evidence"] = int(t["min_independent_evidence"])
    effective = {"t": t, "m": m, "routes": {k: sorted(v) for k, v in groups.items()}, "prior": prior,
                 "ops": {"mode": mode, "wait": wait}, "go_live": gl}
    fp = hashlib.sha256(json.dumps(effective, sort_keys=True).encode()).hexdigest()[:12]
    return Policy(name=str(meta.get("name", "Fraud Policy")), version=str(meta.get("version", "0")),
                  effective_from=str(meta.get("effective_from", "")), approved_by=str(meta.get("approved_by") or ""),
                  currency=str(cur.get("code", "USD")), symbol=str(cur.get("symbol", "$")),
                  t=Thresholds(**t), money=Money(**m), auto=groups["auto"], l1=groups["L1"], l2=groups["L2"],
                  source=source, fingerprint=fp, unscored_prior=prior, mode=mode, reply_wait_hours=wait, go_live=gl)


def load(path: str | os.PathLike | None = None) -> Policy:
    p = Path(path or os.environ.get("POLICY_FILE") or DEFAULT_FILE)
    if not p.is_absolute():
        p = ROOT / p
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except FileNotFoundError as ex:
        raise PolicyError(f"policy file not found: {p}") from ex
    except yaml.YAMLError as ex:
        raise PolicyError(f"policy file {p} is not valid YAML: {ex}") from ex
    return parse(raw, str(p))


POLICY = load()
