"""config/policy.yaml loading and the deterministic policy engine."""
import copy

import pytest
import yaml

from agent import policy as P
from agent import policy_config as C
from tests.conftest import ROOT

BASE = yaml.safe_load((ROOT / "config/policy.yaml").read_text(encoding="utf-8"))


def test_default_policy_loads():
    p = C.parse(copy.deepcopy(BASE))
    assert p.t.fraud_stop == 0.85 and p.t.legit_stop == 0.15 and p.money.block_card_team_lead_max == 2500
    assert p.mode in ("shadow", "assist") and len(p.fingerprint) == 12


@pytest.mark.parametrize("mutate, message", [
    (lambda r: r["thresholds"].update(fraud_stop=0.6), "fraud_stop"),
    (lambda r: r["thresholds"].update(legit_stop=1.4), "between 0 and 1"),
    (lambda r: r["thresholds"].pop("open_case"), "missing"),
    (lambda r: r["routes"]["auto"].append("FILE_REPORT"), "both"),
    (lambda r: (r["routes"]["L2"].remove("FILE_REPORT"), r["routes"]["auto"].append("FILE_REPORT")), "automatically"),
    (lambda r: r["routes"]["L1"].append("BLOCK_CARD"), "exposure"),
    (lambda r: r["money"].update(report_over="lots"), "number"),
    (lambda r: r["routes"]["auto"].remove("WARN_CUSTOMER"), "no route"),
    (lambda r: r["operations"].update(mode="autopilot"), "shadow or assist"),
    (lambda r: r["go_live"].update(min_fraud_precision=2), "go_live"),
])
def test_bad_policies_are_refused(mutate, message):
    r = copy.deepcopy(BASE)
    mutate(r)
    with pytest.raises(C.PolicyError, match=message):
        C.parse(r)


def test_fingerprint_follows_settings_not_wording():
    a = C.parse(copy.deepcopy(BASE))
    r = copy.deepcopy(BASE)
    r["policy"]["approved_by"] = "Someone"
    assert C.parse(r).fingerprint == a.fingerprint          # metadata does not change the fingerprint
    r["money"]["block_card_team_lead_max"] = 1000
    assert C.parse(r).fingerprint != a.fingerprint


@pytest.mark.parametrize("action, exposure, route", [
    ("BLOCK_CARD", 2500, "L1"), ("BLOCK_CARD", 2500.01, "L2"), ("DECLINE_TRANSACTION", 10, "L1"),
    ("FILE_REPORT", 10, "L2"), ("BLOCK_ALL_CARDS", 10, "L2"), ("MONITOR_CARD", 99999, "auto"),
])
def test_routes(action, exposure, route):
    assert P.route(action, exposure) == route


def _sit(**kw):
    base = dict(trigger="risk_score", p=0.5, fraud_families=1, legit_families=0, exposure=300.0, pattern="card_not_present_fraud")
    base.update(kw)
    return P.Situation(**base)


def test_strong_fraud_blocks_and_stops():
    d = P.decide(_sit(p=0.92, fraud_families=2, exposure=1906.07))
    names = [a["action"] for a in d.actions]
    assert d.verdict == "fraud" and d.stop and "BLOCK_CARD" in names and "FILE_REPORT" in names
    assert not P.check(d.actions, _sit(p=0.92, fraud_families=2, exposure=1906.07))


def test_single_weak_signal_never_blocks():
    s = _sit(p=0.6, fraud_families=1)
    d = P.decide(s)
    assert "BLOCK_CARD" not in [a["action"] for a in d.actions] and d.request
    assert P.check([{"action": "BLOCK_CARD", "route": "L1", "reason": ""}], s)   # the guard catches an R1 breach


def test_strong_legit_closes():
    d = P.decide(_sit(p=0.03, fraud_families=0, legit_families=2))
    assert d.verdict == "legitimate" and d.stop and "CLOSE_NO_FRAUD" in [a["action"] for a in d.actions]


def test_recurring_dispute_verifies_instead_of_blocking():
    d = P.decide(_sit(trigger="customer_report", p=0.4, recurring_dispute=True))
    names = [a["action"] for a in d.actions]
    assert "BLOCK_CARD" not in names and "VERIFY_WITH_CUSTOMER" in names and d.request == "customer_validation"


def test_reply_paths():
    deny = P.decide(_sit(p=0.9, fraud_families=2, response="denied", trigger="customer_report"))
    ok = P.decide(_sit(p=0.05, response="confirmed"))
    none = P.decide(_sit(p=0.5, response="no_reply", exposure=800))
    assert deny.verdict == "fraud"
    assert ok.verdict == "legitimate" and "ALLOW_TRANSACTION" in [a["action"] for a in ok.actions]
    assert none.status == "escalated" and "ESCALATE_TO_ANALYST" in [a["action"] for a in none.actions]
