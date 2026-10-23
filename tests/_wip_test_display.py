"""Wording on the case view: shadow approvals, unsettled evidence, markdown-safe data."""
from ui import viewmodel as vm


def test_shadow_auto_route_does_not_claim_execution():
    assert "executes" in vm.route_who("auto", shadow=False)
    txt = vm.route_who("auto", shadow=True)
    assert "not executed" in txt and "executes" not in txt
    assert vm.route_who("L2", shadow=True) == vm.ROUTE_WHO["L2"]


def test_evidence_split_wording():
    assert "conflict" not in vm.evidence_split(2, 0)
    assert "none to legitimate" in vm.evidence_split(2, 0)
    assert "conflicts" in vm.evidence_split(2, 1)
