"""Roles, approvals and API keys (agent/access.py)."""
import pytest

from agent import access as A
from agent.policy_config import POLICY
from tests.conftest import ADMIN, ANALYST, LEAD, MANAGER, VIEWER

BLOCK = {"action": "BLOCK_CARD", "route": "L1", "reason": "r"}
REPORT = {"action": "FILE_REPORT", "route": "L2", "reason": "r"}
YES = "agreed" if POLICY.shadow else "approved"
NO = "disagreed" if POLICY.shadow else "rejected"


@pytest.mark.parametrize("role, perm, allowed", [
    ("viewer", "view", True), ("viewer", "investigate", False), ("analyst", "investigate", True),
    ("analyst", "approve_L1", False), ("team_lead", "approve_L1", True), ("team_lead", "approve_L2", False),
    ("fraud_manager", "approve_L2", True), ("fraud_manager", "manage_users", False), ("admin", "ingest", True),
    (None, "view", False), ("intruder", "view", False),
])
def test_permissions(role, perm, allowed):
    assert A.can(role, perm) is allowed


def test_first_sign_in_and_admin_emails(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "Boss@Test.bank; other@test.bank")
    assert A.resolve_user("boss@test.bank")["role"] == "admin"
    assert A.resolve_user("new.person@test.bank")["role"] == "viewer"


def test_role_changes():
    A.resolve_user("someone@test.bank")
    with pytest.raises(PermissionError):
        A.set_role(LEAD, "someone@test.bank", "admin")
    A.set_role(ADMIN, "someone@test.bank", "analyst")
    assert A.resolve_user("someone@test.bank")["role"] == "analyst"
    with pytest.raises(PermissionError):
        A.set_role(ADMIN, ADMIN["email"], "viewer")      # an admin cannot demote themselves


def test_approval_routes_are_enforced():
    with pytest.raises(PermissionError):
        A.decide(ANALYST, "T-1", "g", BLOCK, YES)
    with pytest.raises(PermissionError):
        A.decide(LEAD, "T-1", "g", REPORT, YES)
    with pytest.raises(PermissionError):
        A.decide(LEAD, "T-1", "g", {"action": "MONITOR_CARD", "route": "auto"}, YES)
    A.decide(LEAD, "T-1", "g", BLOCK, YES)
    A.decide(MANAGER, "T-1", "g", REPORT, NO)
    assert A.decision_for("T-1", "BLOCK_CARD")["decision"] == YES


def test_a_decision_cannot_be_repeated_or_use_the_wrong_mode():
    A.decide(LEAD, "T-2", "g", BLOCK, YES)
    with pytest.raises(ValueError, match="already"):
        A.decide(MANAGER, "T-2", "g", BLOCK, NO)
    with pytest.raises(ValueError, match="mode"):
        A.decide(LEAD, "T-3", "g", BLOCK, "approved" if POLICY.shadow else "agreed")


def test_api_keys():
    with pytest.raises(PermissionError):
        A.create_api_key(VIEWER, "x", "sentinelgraph", ["read"])
    key = A.create_api_key(ADMIN, "core banking", "example_bank", ["transactions", "read"])
    assert A.check_api_key(key, "transactions")["mapping"] == "example_bank"
    assert A.check_api_key(key, "alerts") is None                       # scope not granted
    assert A.check_api_key(key[:-2] + "xx", "read") is None             # tampered
    assert A.check_api_key("not-a-key", "read") is None
    assert all("hash" not in k for k in A.list_api_keys())
    A.revoke_api_key(ADMIN, key.split("_")[1])
    assert A.check_api_key(key, "read") is None


def test_audit_is_append_only_and_records_who():
    n = len(A.recent_audit(10000))
    A.audit("x@test.bank", "analyst", "investigation_run", "C-1", {"txn": "1"})
    last = A.recent_audit(1)[0]
    assert len(A.recent_audit(10000)) == n + 1 and last["actor"] == "x@test.bank" and last["case_id"] == "C-1"
