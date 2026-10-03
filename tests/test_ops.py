"""Structured operational logs (agent/obs.py) and the Cloud Monitoring files that depend on them."""
import json
import re

from agent import access as A
from agent import obs
from tests.conftest import ROOT

MON = ROOT / "deploy" / "monitoring"
FILL = {"{{PROJECT}}": "p", "{{SERVICE}}": "sentinelgraph-client", "{{PREFIX}}": "sentinelgraph_client",
        "{{SUBSCRIPTION}}": "s", "{{CONSOLE_URL}}": "https://x.run.app", "{{CHANNEL}}": "projects/p/notificationChannels/1",
        "{{CONSOLE_CHECK}}": "c", "{{API_CHECK}}": "a"}


def _lines(capsys):
    return [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]


def test_events_are_one_json_object_per_line(capsys):
    obs.event("investigation_error", "ERROR", alert_id="A-1", error="boom")
    rec = _lines(capsys)[-1]
    assert rec["severity"] == "ERROR" and rec["event"] == "investigation_error" and rec["alert_id"] == "A-1"


def test_audit_entries_and_flags_become_events(capsys):
    A.audit("x@test.bank", "analyst", "input_flagged", "C-9", {"source": "test", "reasons": ["asks to ignore instructions"]})
    evs = {r["event"]: r for r in _lines(capsys)}
    assert evs["input_flagged"]["severity"] == "WARNING" and evs["audit"]["action"] == "input_flagged"
    assert "detail" not in evs["audit"]                      # audit details (which may hold text) are not logged


def test_monitoring_files_fill_and_parse():
    for f in MON.rglob("*.json"):
        t = f.read_text(encoding="utf-8")
        for k, v in FILL.items():
            t = t.replace(k, v)
        assert not re.findall(r"\{\{[A-Z_]+\}\}", t), f
        json.loads(t)


def test_every_alerted_event_is_emitted_by_the_code():
    code = "\n".join(p.read_text(encoding="utf-8") for d in ("agent", "api", "worker", "ui") for p in (ROOT / d).rglob("*.py"))
    for f in (MON / "metrics").glob("*.json"):
        ev = re.search(r'jsonPayload.event="(\w+)"', json.loads(f.read_text())["filter"]).group(1)
        assert re.search(rf'event\(\s*"{ev}"', code), f"{f.name} counts '{ev}' but nothing logs it"


def test_policies_point_at_defined_metrics():
    names = {json.loads(f.read_text())["name"].replace("{{PREFIX}}_", "") for f in (MON / "metrics").glob("*.json")}
    for f in (MON / "policies").glob("*.json"):
        for m in re.findall(r"user/\{\{PREFIX\}\}_(\w+)", f.read_text()):
            assert m in names, f"{f.name} uses undefined metric {m}"
