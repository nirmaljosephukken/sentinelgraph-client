"""
SentinelGraph — agentic fraud investigation & next-best action console (Google Cloud AI Builder Cup 2026, BFSI theme).

    python -m streamlit run ui/app.py

Less dashboard, more investigation: the first screen answers four questions (what happened, what the agent
found, how certain it is, what happens next); everything below it is the proof. Nothing in the UI changes a
decision; decisions come from agent/policy.py and are read from the answer file and the investigation trace.
"""
from __future__ import annotations

import html as H
import json
import math
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.backends import get_backend  # noqa: E402
from agent.config import SETTINGS  # noqa: E402
from agent.embeddings import embed  # noqa: E402
from agent.llm import LLM  # noqa: E402
from agent.orchestrator import Investigation  # noqa: E402
from ui import viewmodel as vm  # noqa: E402
from ui import auth  # noqa: E402
from agent import access as A  # noqa: E402
from ui.theme import CSS  # noqa: E402

ASSETS = ROOT / "ui" / "assets"
st.set_page_config(page_title="SentinelGraph · Fraud Intelligence", page_icon=str(ASSETS / "mark.svg"), layout="wide",
                   initial_sidebar_state="expanded")
st.html(CSS)


def me() -> dict:
    return st.session_state.get("user") or {"email": "", "name": "", "role": None}


def allowed(permission: str) -> bool:
    return A.can(me().get("role"), permission)
st.logo(str(ASSETS / "logo.svg"), icon_image=str(ASSETS / "mark.svg"), size="large")

KIND = {"trigger": ("Alert received", "#FDD663"), "case": ("Case opened", "#FDD663"), "tool": ("Graph query", "#34A853"),
        "evidence": ("Evidence", "#BDC1C6"), "assess": ("Assessment", "#FDD663"), "decision": ("Decision", "#FDD663"),
        "execute": ("Executed", "#81C995"), "request": ("Evidence request", "#8AB4F8"),
        "response": ("Reply", "#8AB4F8"), "llm": ("LLM", "#8AB4F8"), "memory": ("Case memory", "#81C995"),
        "guardrail": ("Guardrail", "#F28B82"), "error": ("Error", "#F28B82"), "approval": ("Approval", "#81C995")}
BADGE_CLS = {"TIGERGRAPH": "tg", "SYNTHETIC GRAPH": "tg", "CASE MEMORY": "mem", "MEMORY MODEL": "mem", "GRAPHRAG": "rag", "CUSTOMER": "cust"}
GLYPH = {"BLOCK_ALL_CARDS": ("■", "crit"), "BLOCK_CARD": ("■", "crit"), "DECLINE_TRANSACTION": ("✕", "crit"),
         "STEP_UP_AUTH": ("⇡", "warn"), "VERIFY_WITH_CUSTOMER": ("?", "warn"), "ESCALATE_TO_ANALYST": ("↑", "warn"),
         "ALLOW_TRANSACTION": ("✓", "legit"), "CLOSE_NO_FRAUD": ("✓", "legit"), "MONITOR_CARD": ("◉", "warn")}
TONE = {"crit": ("var(--pink)", "rgba(255,92,147,.16)"), "warn": ("var(--yellow)", "rgba(242,201,76,.16)"),
        "legit": ("var(--legit)", "rgba(121,217,160,.16)")}
STATUS_CHIP = {"awaiting_evidence": ("c-warn", "Waiting · cardholder reply"),
               "closed_fraud": ("c-crit", "Closed · fraud confirmed"), "closed_legitimate": ("c-legit", "Closed · legitimate"),
               "escalated": ("c-warn", "Escalated · analyst review"), "open": ("c-warn", "Open · in progress"),
               "investigating": ("c-warn", "Investigating")}
OUTCOME = {"fraud": ("Fraud", "var(--pink)"), "legitimate": ("Legitimate", "var(--legit)"),
           "uncertain": ("Uncertain", "var(--yellow)")}
SAR_YES = "<span class='badge cust'>file</span>"
SAR_NO = "<span class='muted'>—</span>"


def e(x) -> str:
    return H.escape(str(x), quote=True)


def pct(p: float) -> str:
    """The one formatter for probabilities, so the same number never shows as 3% on one card and 2.6% on another."""
    p *= 100
    return f"{p:.1f}%"


def money(x: float) -> str:
    return f"${x:,.2f}"


def page_head(eyebrow: str, title: str, sub: str = ""):
    st.html(f"<div class='eyebrow'>{e(eyebrow)}</div><h1 class='page'>{e(title)}</h1>"
            + (f"<div class='sub'>{e(sub)}</div>" if sub else ""))


# =========================================================================== data
@st.cache_resource(show_spinner="Connecting to TigerGraph (a suspended Savanna workspace takes 1-3 minutes to wake) …")
def _backend():
    return get_backend(SETTINGS)


CLIENT_DATA = os.getenv("DATA_SOURCE", "both").lower() == "client"
from agent.policy_config import POLICY as _POLICY  # noqa: E402
POLICY_VERSION = _POLICY.version
SHADOW = _POLICY.shadow
POLICY_LABEL = _POLICY.label


def backend():
    try:
        return _backend()
    except Exception as ex:  # noqa: BLE001
        st.error("TigerGraph is not reachable right now. If your Savanna workspace was idle it is suspended: open the "
                 "Savanna console, resume the workspace (or wait 1-3 minutes), then reload this page. "
                 "Saved investigations below still work.")
        with st.expander("Details"):
            st.code(str(ex)[:600])
        st.stop()


@st.cache_data
def case_pack() -> pd.DataFrame:
    return pd.read_csv(SETTINGS.prepared_dir / "case_pack.csv", dtype=str)


@st.cache_data
def closed_cases() -> pd.DataFrame:
    return pd.read_csv(SETTINGS.prepared_dir / "closed_cases.csv", dtype={"case_id": str})


def load_saved(case_id: str):
    a = SETTINGS.cases_dir / f"{case_id}.json"
    t = SETTINGS.traces_dir / f"{case_id}.json"
    if a.exists() and t.exists():
        return json.loads(a.read_text()), json.loads(t.read_text())["events"]
    return None, None


def all_answers() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(SETTINGS.cases_dir.glob("ALR-*.json"))]


def all_traces() -> dict[str, list]:
    out = {}
    for p in sorted(SETTINGS.traces_dir.glob("ALR-*.json")):
        try:
            out[p.stem] = json.loads(p.read_text())["events"]
        except (json.JSONDecodeError, KeyError):
            pass
    return out


@st.cache_data(ttl=120, show_spinner=False)
def status_info():
    """Connection facts for the sidebar; never raises."""
    try:
        g = _backend()
        stats = g.graph_stats()
        via = "MCP" if getattr(g, "mcp", None) is not None else ("RESTPP" if g.name == "tigergraph" else "local")
        ntools = len(g.mcp.tools) if getattr(g, "mcp", None) is not None else 0
        return {"ok": True, "name": g.name, "via": via, "tools": ntools, **stats}
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "err": str(ex)}


# =========================================================================== shared pieces
def merged_evidence(answer: dict, events: list[dict]) -> list[dict]:
    raw = []
    for ev in events:
        if ev["kind"] == "evidence" and isinstance(ev.get("detail"), list):
            raw = ev["detail"]
    by_claim = {r["claim"]: r for r in raw}
    out = []
    for item in answer["case"]["evidence"]:
        r = by_claim.get(item["claim"], {})
        direction = r.get("direction")
        if not direction:
            cl = item["claim"].lower()
            direction = "neutral"
            if item["source"] == "customer" and "simulated reply" in cl:
                direction = "fraud" if "denied" in cl else ("legit" if "confirmed" in cl else "neutral")
        out.append({**item, "direction": direction, "lr": r.get("lr"), "family": r.get("family", "context")})
    return out


def trigger_facts(events: list[dict], answer: dict) -> dict:
    trig = next((x for x in events if x["kind"] == "trigger"), {})
    title = trig.get("title", "")
    # the trigger text carries the transaction amount (e.g. "($77.07, in billing region 444.0)" or "$49.00 online");
    # exposure is 0 for legitimate cases, so it is only a last resort
    m = re.search(r"\$([\d,]+\.\d\d)", title)
    amount = float(m.group(1).replace(",", "")) if m else answer["case"]["exposure_usd"]
    ch = re.search(r"\(\$[\d,]+\.\d\d,\s*([a-z ]+)\)", title)
    channel = ch.group(1).strip() if ch else ""
    return {"title": title, "amount": amount, "channel": channel, **(trig.get("detail") or {})}


def graph_html(answer: dict, events: list[dict], height: int = 520) -> tuple[str, Counter]:
    c = answer["case"]
    det = trigger_facts(events, answer)
    card, cust, flag = det.get("card_id"), det.get("customer_id"), det.get("flagged_txn_id")
    gid = c["graph_case_id"]
    nodes, edges, cnt = [], [], Counter()

    def n(i, label, group, title=""):
        if i and all(x["id"] != i for x in nodes):
            nodes.append({"id": i, "label": label, "group": group, "title": title or label})
            cnt[group] += 1

    n(cust, cust, "customer", "customer")
    n(card, card, "card", "card under investigation")
    edges.append({"from": cust, "to": card})
    n(gid, gid, "case", f"agent case · {c['verdict']}")
    edges.append({"from": gid, "to": card, "dashes": True})
    for t in c["affected_txn_ids"] or [flag]:
        n(t, t, "flagged" if t == flag else "txn", "flagged transaction" if t == flag else "transaction in the episode")
        edges.append({"from": card, "to": t})
    for d in c["connected_device_profiles"]:
        n(d, d[:24] + ("…" if len(d) > 24 else ""), "device", d)
        for t in (c["affected_txn_ids"] or [flag]):
            edges.append({"from": t, "to": d})
        for k in c["connected_card_ids"][:40]:
            n(k, k, "ccard", "connected card")
            edges.append({"from": d, "to": k})
    if not c["connected_device_profiles"]:
        for k in c["connected_card_ids"][:40]:
            n(k, k, "ccard", "connected card")
            edges.append({"from": flag, "to": k, "dashes": True})
    for m in c["similar_prior_cases"]:
        n(m, m, "memory", "closed case used as memory")
        edges.append({"from": gid, "to": m, "dashes": True})
    data = json.dumps({"nodes": nodes, "edges": edges})
    doc = f"""<!doctype html><html><head><style>
body{{margin:0;background:#121318;font-family:'IBM Plex Sans',system-ui,sans-serif;border-radius:14px;overflow:hidden}}
#g{{width:100%;height:{height}px}}
.lg{{position:absolute;left:14px;bottom:10px;display:flex;flex-wrap:wrap;gap:14px;font:12px system-ui;color:#BDC1C6}}
.lg i{{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:-1px}}
</style></head><body>
<div id="g"></div>
<div class="lg"><span><i style="background:#F28B82"></i>flagged transaction</span><span><i style="background:#FDD663"></i>episode transaction</span>
<span><i style="background:#E8EAED"></i>card</span><span><i style="background:#81C995"></i>customer</span>
<span><i style="background:#8AB4F8"></i>shared device</span><span><i style="background:#3C4043"></i>connected card</span>
<span><i style="background:#34A853"></i>closed case (memory)</span><span><i style="background:#FDD663;border-radius:50%"></i>agent case</span></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.9/standalone/umd/vis-network.min.js"></script>
<script>
const d = {data};
const F = {{color:'#BDC1C6', size:11, face:'IBM Plex Mono, monospace'}};
const groups = {{
 customer:{{shape:'dot',size:11,color:{{background:'#81C995',border:'#81C995'}},font:F}},
 card:{{shape:'box',color:{{background:'#E8EAED',border:'#E8EAED'}},font:{{color:'#15171C',face:'IBM Plex Mono'}},margin:7}},
 flagged:{{shape:'diamond',size:16,color:{{background:'#F28B82',border:'#F28B82'}},font:{{color:'#F6AEA9',size:12,face:'IBM Plex Mono'}}}},
 txn:{{shape:'dot',size:8,color:{{background:'#FDD663',border:'#FDD663'}},font:F}},
 device:{{shape:'hexagon',size:17,color:{{background:'#8AB4F8',border:'#8AB4F8'}},font:F}},
 ccard:{{shape:'box',color:{{background:'#1A1B21',border:'#3C4043'}},font:{{color:'#BDC1C6',size:10,face:'IBM Plex Mono'}},margin:4}},
 memory:{{shape:'triangle',size:9,color:{{background:'#34A853',border:'#34A853'}},font:F}},
 case:{{shape:'star',size:16,color:{{background:'#FDD663',border:'#FDD663'}},font:{{color:'#FDD663',size:12,face:'IBM Plex Mono'}}}}}};
const net = new vis.Network(document.getElementById('g'), {{nodes:new vis.DataSet(d.nodes), edges:new vis.DataSet(d.edges)}},
 {{groups, interaction:{{hover:true, zoomView:true}}, physics:{{stabilization:{{iterations:300}}, barnesHut:{{springLength:115, gravitationalConstant:-5200}}}},
   edges:{{color:{{color:'#3C4043',highlight:'#FDD663',hover:'#FDD663'}}, width:1.2, smooth:{{type:'continuous'}}}}}});
net.once('stabilizationIterationsDone', () => {{ net.fit({{animation:false}}); net.setOptions({{physics:false}}); }});
setTimeout(() => net.fit(), 1200);
new ResizeObserver(() => {{ net.redraw(); net.fit(); }}).observe(document.getElementById('g'));
</script></body></html>"""
    return doc, cnt


def show_graph(answer, events, height=520):
    doc, cnt = graph_html(answer, events, height)
    c = answer["case"]
    ndev = cnt["device"]
    chips = [f"<span class='chip'>{cnt['flagged'] + cnt['txn']} transaction(s)</span>",
             f"<span class='chip'>{1 + cnt['ccard']} card(s)</span>",
             f"<span class='chip'>{ndev} shared device{'' if ndev == 1 else 's'}</span>",
             f"<span class='chip'>{cnt['memory']} memory case(s)</span>",
             f"<span class='chip c-legit'><span class='d' style='background:var(--legit)'></span>"
             f"{'written to TigerGraph' if c['written_to_graph'] else ('sandbox only, not written' if _sandbox() else 'not written')} · {e(c['graph_case_id'])}</span>"]
    st.html(f"<div style='display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px'>{''.join(chips)}</div>")
    if hasattr(st, "iframe"):
        st.iframe(doc, height=height + 8)
    else:
        components.html(doc, height=height + 8)


def meter(label: str, value: float, text: str, color: str) -> str:
    return (f"<div class='meter'><div class='top'><span>{e(label)}</span><b>{e(text)}</b></div>"
            f"<div class='track'><div class='fill' style='width:{max(2, min(100, value * 100)):.0f}%;background:{color}'></div></div></div>")


def family_bars(fam: dict) -> str:
    if not fam:
        return "<div class='muted'>No evidence families recorded.</div>"
    rows = sorted(fam.items(), key=lambda kv: -abs(kv[1]))
    m = max(3.0, max(abs(v) for _, v in rows))
    out = []
    for k, v in rows:
        w = max(1.0, 50 * abs(v) / m)
        lr = math.exp(v)
        txt = f"×{lr:.1f}" if lr >= 1 else f"÷{1 / lr:.1f}"
        clr = "var(--pink)" if v >= 0 else "var(--legit)"
        left = 50 if v >= 0 else 50 - w
        out.append(f"<div class='assess'><span class='ink2'>{e(vm.FAMILY.get(k, k))}</span>"
                   f"<div style='position:relative;height:10px;background:#16171C;border-radius:3px'>"
                   f"<div style='position:absolute;left:50%;top:-3px;bottom:-3px;width:1px;background:var(--line2)'></div>"
                   f"<div class='bar' style='position:absolute;left:{left:.1f}%;width:{w:.1f}%;background:{clr}'></div></div>"
                   f"<span class='x'>{txt}</span></div>")
    return ("".join(out) + "<div style='display:grid;grid-template-columns:170px 1fr 64px;gap:10px;font:11px var(--mono);"
            "color:var(--mute);margin-top:6px'><span></span><div style='display:flex;justify-content:space-between'>"
            "<span>← legitimate</span><span>fraud →</span></div><span></span></div>")


def action_rows(v: vm.View) -> str:
    """Action hierarchy: executed by the agent / waiting for a human / not permitted without more evidence."""
    a = v.answer
    final = a["next_best_actions"]["final"]
    executed = set()
    for ev in v.events:
        if ev["kind"] == "execute" and isinstance(ev.get("detail"), dict):
            executed |= set(ev["detail"].get("executed", []))
    names = {x["action"] for x in final}
    rows = []
    rank = lambda x: vm.ORDER_HEADLINE.index(x["action"]) if x["action"] in vm.ORDER_HEADLINE else 99  # noqa: E731
    for x in sorted(final, key=lambda x: (x["route"] == "auto", rank(x))):
        if x["route"] == "auto":
            icon, col = "✓", "var(--legit)"
            state = "executed" if (x["action"] in executed or not executed) else "queued"
        else:
            icon, col, state = "◆", "var(--yellow)", f"awaiting {x['route']}"
        rows.append(f"<div class='arow'><span style='color:{col};font:700 13px var(--mono)'>{icon}</span>"
                    f"<div><div class='t'>{e(vm.ACTION_TEXT.get(x['action'], x['action']))} "
                    f"<span class='mono muted' style='font-size:11px'>{e(x['action'])}</span></div>"
                    f"<div class='r'>{e(vm.short(x['reason'], 150))}</div></div><span class='s'>{e(state)}</span></div>")
    verdict = a["case"]["verdict"]
    restricted = []
    if not names & {"BLOCK_CARD", "BLOCK_ALL_CARDS"}:
        restricted.append(("BLOCK_CARD", "R1: a block needs fraud probability ≥85% on two independent lines of evidence"
                           if verdict != "uncertain" else "R8: conflicting evidence, the analyst decides before any block"))
    if verdict == "fraud" and "BLOCK_ALL_CARDS" not in names:
        restricted.append(("BLOCK_ALL_CARDS", "R10: needs confirmed fraud on two or more of the customer's cards"))
    for act, why in restricted:
        rows.append(f"<div class='arow' style='opacity:.6'><span style='color:var(--mute);font:700 13px var(--mono)'>⊘</span>"
                    f"<div><div class='t'>{e(vm.ACTION_TEXT[act])} <span class='mono muted' style='font-size:11px'>{e(act)}</span></div>"
                    f"<div class='r'>{e(why)}</div></div><span class='s'>needs more evidence</span></div>")
    return "<div class='alist'>" + "".join(rows) + "</div>"


def governance(v: vm.View) -> str:
    a = v.answer
    final = a["next_best_actions"]["final"]
    auto = [x for x in final if x["route"] == "auto"]
    human = [x for x in final if x["route"] != "auto"]
    top = "L2" if any(x["route"] == "L2" for x in human) else ("L1" if human else "auto")
    rules = sorted({r for x in final for r in re.findall(r"\b(R\d+|3a)\b", x["reason"])},
                   key=lambda r: (r == "3a", int(r[1:]) if r[1:].isdigit() else 0))
    n_audit = sum(1 for ev in v.events if ev["kind"] in ("decision", "execute", "memory", "request", "response"))
    cells = [("Execution", f"{len(auto)} auto · {len(human)} queued for approval"),
             ("Approval route", vm.ROUTE_WHO[top]),
             ("Policy", "Fraud Policy v1.0 · " + (", ".join(rules) if rules else "section 6")),
             ("Audit", f"{n_audit} logged decisions · {a['case']['graph_case_id']}")]
    return "<div class='gov'>" + "".join(f"<div><div class='eyebrow'>{e(k)}</div><div class='v'>{e(val)}</div></div>"
                                         for k, val in cells) + "</div>"


def record_approval(answer, action, decision, card_id: str = ""):
    from agent import actions as ACT
    u = me()
    try:
        entry = A.decide(u, answer["case_id"], answer["case"]["graph_case_id"], action, decision)
    except (PermissionError, ValueError) as ex:
        st.error(str(ex))
        return
    sent = ACT.after_decision(answer["case_id"], answer, action, decision, u, card_id)
    if sent and sent.get("status") in ("failed", "not_sent"):
        st.warning(f"{action['action']} approved, but not delivered to the bank's systems: {sent.get('detail')}")
    title = f"{action['action']} {decision} by {u['email']} ({A.ROLE_LABEL.get(u['role'], u['role'])})"
    detail = {"action": action, "decision": decision, "actor": u["email"], "role": u["role"]}
    g = backend()
    try:
        if hasattr(g, "tg"):
            cid = answer["case"]["graph_case_id"]
            eid = f"{cid}#A{int(time.time())}"
            g.tg.upsert(vertices={"CaseEvent": {eid: {"case_id": {"value": cid}, "step": {"value": 99},
                                                     "ts": {"value": entry["ts"]}, "kind": {"value": "approval"},
                                                     "title": {"value": title},
                                                     "detail": {"value": json.dumps(detail)}}}},
                        edges={"FraudCase": {cid: {"FC_EVENT": {"CaseEvent": {eid: {}}}}}})
        st.toast(f"{title} · recorded in the audit log")
    except Exception as ex:  # noqa: BLE001  (the audit log entry is already written)
        st.warning(f"Recorded in the audit log, but the graph copy failed: {ex}")


# =========================================================================== investigate: sections
def sec(title: str, right: str = ""):
    st.html(f"<div class='sec'><h3>{e(title)}</h3><span class='eyebrow'>{e(right)}</span></div>")


def case_header(case: dict, v: vm.View | None):
    if v is None:
        st.html(f"<div class='eyebrow'>Alert · {e(case['trigger_type'].replace('_', ' '))}</div>"
                f"<h1 class='page'>{e(case['case_id'])}</h1><div class='sub'>{e(case['trigger_text'])}</div>")
        return
    c = v.answer["case"]
    tf = trigger_facts(v.events, v.answer)
    chip_cls, chip_txt = STATUS_CHIP.get(c["status"], ("", c["status"]))
    line = [f"txn {tf.get('flagged_txn_id', case.get('flagged_txn_id', ''))}", money(tf["amount"])]
    if tf["channel"]:
        line.append(tf["channel"])
    line.append(f"card {tf.get('card_id') or case.get('card_id', '')}")
    if v.bank_score is not None:
        line.append(f"bank risk score {v.bank_score:.2f}")
    st.html(f"<div class='head'><div><div class='eyebrow'>Case {e(case['case_id'])} · {e(c['graph_case_id'])} · "
            f"{e(case['trigger_type'].replace('_', ' '))}</div>"
            f"<h1 class='page'>{e(c['pattern'].replace('_', ' ').capitalize())}</h1>"
            f"<div class='line'>{' · '.join(e(x) for x in line)}</div></div>"
            f"<span class='chip {chip_cls}' style='margin-top:8px'><span class='d' style='background:currentColor'></span>"
            f"{e(chip_txt)}</span></div>")


def four_questions(case: dict, v: vm.View, ev_items: list[dict]):
    c = v.answer["case"]
    tf = trigger_facts(v.events, v.answer)
    trig = case["trigger_type"]
    chan = (tf["channel"] + " ") if tf["channel"] else ""
    what = {"risk_score": f"Bank model flagged a {money(tf['amount'])} {chan}payment",
            "customer_report": f"Cardholder disputes a {money(tf['amount'])} charge",
            "analyst_request": "An analyst asked for a review"}.get(trig, "Alert raised")
    find = vm.finding_sentence(v, ev_items)
    f1, _, f2 = find.partition(". ")
    outcome, ocol = OUTCOME.get(c["verdict"], (c["verdict"], "var(--ink)"))
    h = v.headline
    who = vm.ROUTE_WHO.get(h.get("route", "auto"), "")
    n_other = len(v.answer["next_best_actions"]["final"]) - 1
    more = f" · plus {n_other} supporting action{'' if n_other == 1 else 's'}" if n_other > 0 else ""
    thr = " · decision threshold met" if v.final.threshold_met else " · threshold not met"
    st.html(
        "<div class='q4'>"
        f"<div><div class='eyebrow'>What happened?</div><div class='a'>{e(what)}</div>"
        f"<div class='b'>{e(vm.short(case['trigger_text'], 140))}</div></div>"
        f"<div><div class='eyebrow'>What did the agent find?</div><div class='a'>{e(f1.rstrip('.'))}</div>"
        f"<div class='b'>{e(vm.short(f2, 150))}</div></div>"
        f"<div><div class='eyebrow'>How certain is it?</div><div class='a'><span style='color:{ocol}'>{e(outcome)}</span> · "
        f"{e(vm.conf_level(v.final.confidence))} confidence</div>"
        f"<div class='b'>Fraud probability {pct(v.p_final)} · {ev_text(v)[2]}{thr}</div></div>"
        f"<div class='next'><div class='eyebrow'>What happens next?</div>"
        f"<div class='a'>{e(vm.ACTION_TEXT.get(h.get('action'), h.get('action', '—')))}</div>"
        f"<div class='b'>Approval: {e(who)}{e(more)}</div></div>"
        "</div>")


def ev_text(v: vm.View) -> tuple[str, str, str]:
    """(value, label, sentence) for the evidence count; conflicting cases show both sides."""
    f = v.final
    if v.answer["case"]["verdict"] == "uncertain":
        return (f"{f.support} vs {f.contra}", "for fraud vs legitimate · conflicting",
                f"evidence conflicts ({f.support} line(s) for fraud, {f.contra} for legitimate)")
    return (f"{min(f.support, 2)}/2", "independent lines · policy §6",
            f"{min(f.support, 2)}/2 independent lines of evidence agree")


def kpis(v: vm.View):
    c = v.answer["case"]
    risk = vm.risk_level(v.p_final)
    rcol = {"High": "var(--pink)", "Elevated": "var(--pink)", "Moderate": "var(--yellow)", "Low": "var(--legit)"}[risk]
    chip_txt = STATUS_CHIP.get(c["status"], ("", c["status"]))[1]
    head, _, tail = chip_txt.partition(" · ")
    cells = [("Fraud probability", f"<span style='color:{rcol}'>{pct(v.p_final)}</span>", f"{risk} risk · agent posterior"),
             ("Confidence", f"{v.final.confidence * 100:.0f}%", f"{vm.conf_level(v.final.confidence)} · decisive + sufficient"),
             ("Evidence", ev_text(v)[0], ev_text(v)[1]),
             ("Pattern", f"<span style='font:700 16px/1.25 var(--display);white-space:normal'>{e(c['pattern'].replace('_', ' '))}</span>",
              "fraud pattern"),
             ("Exposure", money(c["exposure_usd"]), f"{len(c['affected_txn_ids'])} transaction(s)"),
             ("Case", e(head), e(tail or c["status"]))]
    st.html("<div class='kpis'>" + "".join(
        f"<div class='kpi'><div class='eyebrow'>{k}</div><div class='n' title='{e(re.sub('<[^>]+>', '', n))}'>{n}</div>"
        f"<div class='l'>{lab}</div></div>" for k, n, lab in cells) + "</div>")


def nba_panel(v: vm.View):
    h = v.headline
    act = h.get("action", "")
    glyph, tone = GLYPH.get(act, ("→", "warn"))
    fg, bg = TONE[tone]
    st.html(f"<div class='nba'><div class='eyebrow' style='color:var(--yellow)'>Next best action · recommended</div>"
            f"<div class='big'><span class='ic' style='background:{bg};color:{fg}'>{glyph}</span>"
            f"{e(vm.ACTION_TEXT.get(act, act))}</div>"
            f"<div class='why'>{e(h.get('reason', ''))}</div>"
            f"<div class='eyebrow' style='margin-top:18px'>Action hierarchy</div>{action_rows(v)}"
            f"<div class='eyebrow' style='margin-top:18px'>Action governance</div>{governance(v)}</div>")
    pending = [x for x in v.answer["next_best_actions"]["final"] if x["route"] != "auto"]
    if v.answer["case"].get("status") == "awaiting_evidence":
        pending = []   # nothing to approve until the cardholder's reply is in
    shadow = SHADOW
    yes, no = ("Agree", "Disagree") if shadow else ("Approve", "Reject")
    if pending and shadow:
        st.html("<div class='muted' style='font-size:12.5px;margin:6px 0'>Shadow mode: nothing is acted on. Record whether "
                "you agree with each recommendation; the Outcomes page compares the agent with what really happened.</div>")
    for x in pending:
        k1, k2, k3 = st.columns([5, 1.2, 1.2], vertical_alignment="center")
        prev = A.decision_for(v.answer["case_id"], x["action"])
        verb = "Would you approve" if shadow else "Approve"
        k1.html(f"<div class='muted' style='font-size:13px'>{verb} <b class='mono' style='color:var(--ink)'>{e(x['action'])}</b> "
                f"as {e(vm.ROUTE_WHO[x['route']])}{'?' if shadow else ''}"
                + (f"<br><span style='color:var(--ink)'>{e(prev['decision'])} by {e(prev.get('actor', ''))} · "
                   f"{e(str(prev.get('ts', ''))[:16].replace('T', ' '))}</span>" if prev else "") + "</div>")
        key = f"{v.answer['case_id']}-{x['action']}"
        perm = A.route_permission(x["route"])
        ok = bool(perm) and allowed(perm) and not prev
        tip = (f"Already {prev['decision']}" if prev else
               None if (perm and allowed(perm)) else f"Needs {A.ROLE_LABEL[A.PERMISSIONS[perm]]} or above" if perm else None)
        card = v.case.get("card_id", "")
        if k2.button(yes, key="ap" + key, type="primary", use_container_width=True, disabled=not ok, help=tip):
            record_approval(v.answer, x, "agreed" if shadow else "approved", card)
        if k3.button(no, key="rj" + key, use_container_width=True, disabled=not ok, help=tip):
            record_approval(v.answer, x, "disagreed" if shadow else "rejected", card)


def sufficiency_panel(v: vm.View):
    f = v.final
    title, bullets, closing = vm.why_stopped(v)
    bic, bcol = ("!", "var(--yellow)") if "handed" in title else ("✓", "var(--legit)")
    gaps = "".join(f"<div class='tick'><span class='i' style='color:var(--legit)'>✓</span>{e(g)}</div>" for g in v.gaps_resolved)
    gcol = "var(--mute)" if f.threshold_met else "var(--pink)"
    gaps += "".join(f"<div class='tick'><span class='i' style='color:{gcol}'>?</span>{e(g)}</div>" for g in v.gaps_open[:4])
    thr = ("<span class='chip c-legit'>threshold met</span>" if f.threshold_met
           else "<span class='chip c-warn'>threshold not met</span>")
    st.html(
        f"<div class='panel'><div style='display:flex;justify-content:space-between;align-items:center'>"
        f"<div class='eyebrow'>Evidence sufficiency &amp; uncertainty</div>{thr}</div>"
        + meter("Independent lines of evidence", f.sufficiency,
                f"{f.support} vs {f.contra} · conflicting" if v.answer["case"]["verdict"] == "uncertain" else f"{min(f.support, 2)} of 2 required",
                "var(--legit)" if f.sufficiency >= 1 else "var(--yellow)")
        + meter("Decision confidence", f.confidence, f"{f.confidence * 100:.0f}% · {vm.conf_level(f.confidence)}", "var(--yellow)")
        + meter("Fraud probability", v.p_final, f"{pct(v.p_final)} · stop at ≤15% or ≥85%",
                "var(--pink)" if v.p_final >= 0.5 else "var(--legit)")
        + (f"<div class='eyebrow' style='margin-top:12px'>Open questions at first look</div>{gaps}" if gaps else "")
        + f"<div class='stopbox'><h4>{e(title)}?</h4>"
        + "".join(f"<div class='tick'><span class='i' style='color:{bcol}'>{bic}</span>{e(b)}</div>" for b in bullets)
        + f"<div class='muted' style='font-size:12.5px;margin-top:8px'>{e(closing)}</div></div></div>")


def evolution(v: vm.View):
    a = v.answer
    req = a["evidence_requests"]
    nba = a["next_best_actions"]
    rank = lambda x: vm.ORDER_HEADLINE.index(x["action"]) if x["action"] in vm.ORDER_HEADLINE else 99  # noqa: E731
    i_head = sorted(nba["initial"], key=rank)
    i_act = vm.ACTION_TEXT.get(i_head[0]["action"], i_head[0]["action"]) if i_head else "—"
    f_act = vm.ACTION_TEXT.get(v.headline.get("action"), v.headline.get("action", "—"))

    def stage(eyebrow, p, conf, act, extra="", mid=False):
        return (f"<div class='st{' mid' if mid else ''}'><div class='eyebrow'>{e(eyebrow)}</div>"
                + (f"<div class='row'><span>Fraud probability</span><b>{pct(p)}</b></div>" if p is not None else "")
                + (f"<div class='row'><span>Confidence</span><b>{conf * 100:.0f}%</b></div>" if conf is not None else "")
                + (f"<div class='sact'>{e(act)}</div>" if act else "") + extra + "</div>")

    if req:
        r = req[0]
        if r.get("status") == "pending":
            said, final_stage = (f"Waiting for the cardholder's real reply (up to {r.get('wait_hours', 24):g} hours).",
                                 stage("3 · Final decision", None, None, "after the reply"))
        elif r.get("status") == "answered":
            said = (f"Real reply: {r.get('response', '').replace('_', ' ')}"
                    + (f" via {r['channel']}" if r.get("channel") and r["channel"] != "timeout" else "")
                    + (f" · {r['response_text']}" if r.get("response_text") else "")
                    + (f" · recorded by {r['recorded_by']}" if r.get("recorded_by") else ""))
            final_stage = stage("3 · Final decision", v.p_final, v.final.confidence, f_act)
        else:
            said = r.get("assumed_response", "")
            final_stage = stage("3 · Final decision", v.p_final, v.final.confidence, f_act)
        sec("Decision evolution", "initial → evidence gathered → final")
        body = (stage("1 · Initial assessment", v.p_initial, v.initial.confidence, i_act)
                + "<div class='arr'>→</div>"
                + stage(f"2 · Evidence gathered · {r['type'].replace('_', ' ')}", None, None, "",
                        f"<div class='reply'>{e(vm.short(said, 260))}</div>", mid=True)
                + "<div class='arr'>→</div>"
                + final_stage)
        st.html(f"<div class='evo'>{body}</div>"
                f"<div class='decision' style='margin-top:10px'><b style='color:var(--ink)'>What changed · </b>{e(nba['what_changed'])}</div>")
    else:
        sec("Decision evolution", "memory prior → graph evidence → decision")
        body = (stage("1 · Case-memory prior", v.prior, None, "Model score only, before the graph")
                + "<div class='arr'>→</div>"
                + stage("2 · Graph evidence", None, None, "",
                        f"<div class='reply'>{a['tool_calls']} {'queries on the synthetic graph' if _sandbox() else 'TigerGraph queries via ' + e(via_text(v.events))} · {v.final.support} independent "
                        f"line(s) agree · no customer contact needed</div>", mid=True)
                + "<div class='arr'>→</div>"
                + stage("3 · Decision", v.p_final, v.final.confidence, f_act))
        st.html(f"<div class='evo'>{body}</div>"
                "<div class='decision' style='margin-top:10px'><b style='color:var(--ink)'>No evidence request · </b>"
                "the graph evidence alone met the policy's stop rule, so the initial and final recommendations in the "
                "answer file are the same action set.</div>")


def evidence_col(items: list[dict]):
    st.html("<div class='layer' style='color:var(--yellow)'><span class='n'>1</span>Evidence · what the graph shows</div>")
    order = {"fraud": 0, "legit": 1}
    out = []
    for x in sorted(items, key=lambda x: (order.get(x["direction"], 2), -abs(math.log(x.get("lr") or 1)))):
        d = x["direction"] if x["direction"] in ("fraud", "legit") else "neutral"
        badge = vm.source_badge(x["ref"], x["source"])
        if badge == "TIGERGRAPH" and _sandbox():
            badge = "SYNTHETIC GRAPH"
        lr = x.get("lr")
        lrt = ""
        if lr and d != "neutral":
            lrt = f"<span class='lr'>{'×' + format(lr, '.1f') if lr >= 1 else '÷' + format(1 / lr, '.1f')} odds</span>"
        ids = ", ".join(x["entity_ids"][:4]) + ("…" if len(x["entity_ids"]) > 4 else "")
        dirl = {"fraud": "points to fraud", "legit": "points to legitimate", "neutral": "context"}[d]
        out.append(f"<div class='ev {d}'><div class='c'>{e(x['claim'])}</div><div class='m'>"
                   f"<span class='badge {BADGE_CLS.get(badge, '')}'>{e(badge)}</span>"
                   f"<span class='ref'>{e(x['ref'])}{' · ' + e(ids) if ids else ''} · {dirl}</span>{lrt}</div></div>")
    st.html("<div class='evgrid'>" + "".join(out) + "</div>")


def assessment_col(v: vm.View, events: list[dict]):
    af = [x["detail"] for x in events if x["kind"] == "assess" and isinstance(x.get("detail"), dict)]
    fam = (af[-1] if af else {}).get("log_lr_by_family") or {}
    c = v.answer["case"]
    bank = "" if v.bank_score is None else f"; the bank risk score ({v.bank_score:.2f}) is an input, not a verdict"
    st.html("<div class='layer' style='color:var(--legit)'><span class='n'>2</span>Assessment · how the agent weighs it</div>"
            f"<div class='panel' style='height:auto'>{family_bars(fam)}"
            f"<div class='muted' style='font-size:12.5px;margin-top:12px;line-height:1.55'>Each bar is one evidence family's "
            f"likelihood ratio, capped so no single family decides alone. Memory-model prior {pct(v.prior)} → "
            f"fraud probability {pct(v.p_final)}{bank}.</div></div>")
    oname, ocol = OUTCOME.get(c["verdict"], (c["verdict"], "var(--ink)"))
    st.html("<div class='layer' style='color:var(--pink)'><span class='n'>3</span>Decision · investigation outcome</div>"
            f"<div class='decision'><div style='font:700 17px var(--display);color:{ocol}'>"
            f"{e(oname)} · {e(c['status'].replace('_', ' '))}</div>"
            f"<div style='margin-top:6px'>{e(c['summary'])}</div>"
            f"<div style='margin-top:8px' class='muted'><b>Stop reason · </b>{e(v.answer['stop_reason'])}</div></div>")


def activity(events: list[dict]):
    rows = []
    for ev in events:
        label, color = KIND.get(ev["kind"], (ev["kind"], "#9AA0A6"))
        d = ev.get("detail") or {}
        x = ""
        if ev["kind"] == "tool" and isinstance(d, dict):
            x = f"{str(d.get('transport', '')).upper()} · {float(d.get('latency_s', 0) or 0):.2f}s"
        elif ev["kind"] == "execute" and isinstance(d, dict):
            x = "executed: " + ", ".join(d.get("executed", []))
            if d.get("awaiting"):
                x += "  ·  awaiting: " + ", ".join(d["awaiting"])
        elif ev["kind"] == "decision" and isinstance(d, dict) and d.get("why_request"):
            x = "why ask: " + d["why_request"]
        tm = f"+{float(ev.get('elapsed_s', 0) or 0):.1f}s"
        sub = f"<div class='xx'>{e(vm.short(x, 180))}</div>" if x else ""
        rows.append(f"<div class='act'><div class='tm'>{tm}</div><div class='dt' style='background:{color}'></div>"
                    f"<div><div class='tt'><span class='mono' style='font-size:11px;color:{color};text-transform:uppercase;"
                    f"letter-spacing:.06em'>{e(label)}</span> &nbsp;{e(vm.short(ev['title'], 170))}</div>{sub}</div></div>")
    st.html("<div class='panel' style='height:auto;padding:10px 18px'>" + "".join(rows) + "</div>")


def tab_sar(answer: dict):
    s = answer["sar"]
    if not s["file"]:
        st.html(f"<div class='panel' style='height:auto'><div class='eyebrow'>Suspicious activity report</div>"
                f"<div style='margin-top:6px;font-size:15px;color:var(--ink)'>Not required.</div>"
                f"<div class='muted' style='margin-top:6px;font-size:13.5px'>{e(s['reason'])}</div></div>")
        return
    subj = e(", ".join(s["subjects"][:10])) + (" …" if len(s["subjects"]) > 10 else "")
    st.html(f"<div class='sar'><div class='hd'><div><div class='eyebrow'>Suspicious activity report · draft for L2 approval</div>"
            f"<div style='font:700 18px var(--display);margin-top:2px'>{e(answer['case']['graph_case_id'])}</div></div>"
            f"<span class='badge' style='background:#FCE8E6;color:#A50E0E;border-color:#F6AEA9'>FILE_REPORT · L2</span></div>"
            f"<div class='grid'><div><div class='eyebrow'>Amount</div><div class='mono'>{money(s['total_amount_usd'])}</div></div>"
            f"<div><div class='eyebrow'>Activity dates</div><div class='mono'>{e(' → '.join(s['activity_dates']))}</div></div>"
            f"<div><div class='eyebrow'>Subjects</div><div class='mono' style='font-size:12px'>{subj}</div></div></div>"
            f"<div class='body'>{e(s['narrative'])}</div>"
            f"<div class='body' style='border-top:1px solid var(--cream-line);font-size:13px;color:var(--cream-mute)'>"
            f"<b>Basis · </b>{e(s['reason'])}</div></div>")


def memory_card(cid: str, r: dict | None, score: float | None = None) -> str:
    if r is None:
        return f"<div class='ev neutral'><div class='c mono'>{e(cid)}</div></div>"
    oc = str(r.get("outcome", ""))
    exp = r.get("exposure_usd")
    exp_t = f" · {money(float(exp))}" if exp not in (None, "") and not (isinstance(exp, float) and math.isnan(exp)) else ""
    sim = f"<span class='lr'>similarity {score:.2f}</span>" if score is not None else ""
    tgb = "<span class='badge tg'>TIGERGRAPH VECTOR</span>" if score is not None else ""
    return (f"<div class='ev {'fraud' if 'fraud' in oc else 'legit'}'><div class='c'><b class='mono'>{e(cid)}</b> · "
            f"{e(str(r.get('pattern', '')).replace('_', ' '))} · {e(oc.replace('_', ' '))}{exp_t}</div>"
            f"<div class='m'><span class='badge mem'>CASE MEMORY</span>{tgb}"
            f"<span class='ref'>{e(vm.short(str(r.get('analyst_notes') or r.get('notes') or ''), 220))}</span>{sim}</div></div>")


def tab_memory(answer: dict, items: list[dict]):
    c = answer["case"]
    mem = next((x for x in items if x["claim"].startswith("Case memory")), None)
    cc = closed_cases().set_index("case_id")
    cards = [memory_card(m, cc.loc[m].to_dict() if m in cc.index else None) for m in c["similar_prior_cases"]]
    st.html("<div class='eyebrow' style='margin-bottom:8px'>Closed cases retrieved as memory · graph adjacency + TigerGraph vector search</div>"
            + (f"<div class='decision' style='margin-bottom:10px'>{e(mem['claim'])}</div>" if mem else "")
            + "<div class='evgrid'>" + "".join(cards) + "</div>")
    if c["pattern_description"]:
        st.html(f"<div class='eyebrow' style='margin-top:18px'>Undocumented pattern</div>"
                f"<div class='decision' style='margin-top:8px'>{e(c['pattern_description'])}</div>")


def _sandbox() -> bool:
    """True while rendering a Scenario lab case: its graph is a generated in-memory world, not TigerGraph."""
    return bool(st.session_state.get("_render_sandbox"))


def show_investigation(case: dict, answer: dict, events: list[dict]):
    st.session_state["_render_sandbox"] = str(case.get("case_id", "")).startswith("LAB-")
    v = vm.build(case, answer, events)
    items = merged_evidence(answer, events)
    case_header(case, v)
    four_questions(case, v, items)
    kpis(v)
    left, right = st.columns([1.55, 1], gap="medium")
    with left:
        nba_panel(v)
    with right:
        sufficiency_panel(v)
    evolution(v)
    sec("Investigation graph", ("Synthetic world · in-memory sandbox" if _sandbox() else "TigerGraph · FraudGraph")
        + " · drag, zoom, hover")
    show_graph(answer, events)
    sec("Evidence → Assessment → Decision", "kept separate on purpose")
    c1, c2 = st.columns([1.2, 1], gap="medium")
    with c1:
        evidence_col(items)
    with c2:
        assessment_col(v, events)
    secs = answer['latency_s']
    sec("Agent activity", f"{answer['tool_calls']} graph calls · {'under 0.1' if secs < 0.1 else secs}s · {len(events)} steps")
    activity(events)
    st.html("<div style='height:8px'></div>")
    tabs = st.tabs(["Suspicious activity report", "Case memory", "Answer file", "Raw trace"])
    with tabs[0]:
        tab_sar(answer)
    with tabs[1]:
        tab_memory(answer, items)
    with tabs[2]:
        st.download_button("Download answer file", json.dumps(answer, indent=2), file_name=f"{answer['case_id']}.json")
        st.json(answer, expanded=1)
    with tabs[3]:
        st.json(events, expanded=False)
    st.html("<div class='muted' style='font-size:12px;margin-top:22px;line-height:1.6;border-top:1px solid var(--line);padding-top:12px'>"
            "<b>Definitions.</b> <b>Fraud probability</b>: the agent's posterior after fusing all evidence (the answer file "
            "rounds it to two decimals). <b>Memory-model prior</b>: the case-memory model's calibrated fraud rate before "
            "graph evidence. <b>Bank risk score</b>: the bank's alerting score, an input only. <b>Confidence</b>: half how "
            "decisive the probability is, half whether two independent lines of evidence agree.</div>")


# =========================================================================== pages
def saved_time(case_id: str) -> str:
    p = SETTINGS.cases_dir / f"{case_id}.json"
    return datetime.fromtimestamp(p.stat().st_mtime).strftime("%d %b %Y, %H:%M") if p.exists() else ""


def via_text(events: list | None) -> str:
    ts = Counter(str((ev.get("detail") or {}).get("transport", "")).upper() for ev in (events or [])
                 if ev.get("kind") == "tool" and isinstance(ev.get("detail"), dict))
    ts.pop("", None)
    return " + ".join(ts) if ts else "TigerGraph"


def run_banner(kind: str, answer: dict, when: str, events: list | None = None):
    if kind == "live":
        st.html(f"<div class='runbar live'><span class='chip c-legit'><span class='d' style='background:var(--legit)'></span>"
                f"Live run</span><span>{e(when)} · {answer['tool_calls']} TigerGraph calls via {e(via_text(events))} · {answer['latency_s']}s · "
                f"written to the graph as {e(answer['case']['graph_case_id'])}</span></div>")
    else:
        st.html(f"<div class='runbar'><span class='chip'>Saved investigation</span><span>last run {e(when)} · "
                f"{answer['tool_calls']} TigerGraph calls · {answer['latency_s']}s · opened from the case portfolio</span></div>")


def compare_strip(prev: dict | None, new: dict):
    """What changed between the previous saved run and this live run, so an identical outcome still reads as re-run."""
    if not prev:
        st.html("<div class='cmp'><b>First run for this alert.</b> No earlier investigation to compare with.</div>")
        return
    a, b = prev["case"], new["case"]
    pa = {x["action"] for x in prev["next_best_actions"]["final"]}
    pb = {x["action"] for x in new["next_best_actions"]["final"]}
    items = []
    same = a["verdict"] == b["verdict"]
    items.append(("Outcome", f"unchanged ({b['verdict']})" if same else f"{a['verdict']} → {b['verdict']}", same))
    dp = abs(a["fraud_probability"] - b["fraud_probability"]) < 0.005
    items.append(("Fraud probability", f"{pct(a['fraud_probability'])} → {pct(b['fraud_probability'])}", dp))
    if pa == pb:
        items.append(("Actions", "unchanged", True))
    else:
        ch = [f"+{x}" for x in sorted(pb - pa)] + [f"−{x}" for x in sorted(pa - pb)]
        items.append(("Actions", ", ".join(ch), False))
    ev_a, ev_b = len(a["evidence"]), len(b["evidence"])
    items.append(("Evidence", f"{ev_a} → {ev_b} findings", ev_a == ev_b))
    items.append(("Narrative", "rewritten" if a["summary"] != b["summary"] else "identical", True))
    items.append(("Graph calls", f"{prev['tool_calls']} → {new['tool_calls']}", True))
    cells = "".join(f"<div><div class='eyebrow'>{e(k)}</div><div class='v' style='color:{'var(--ink)' if ok else 'var(--yellow)'}'>"
                    f"{e(v)}</div></div>" for k, v, ok in items)
    note = ("Same evidence in the graph, same policy, so the same decision. The agent is deterministic by design; "
            "only the LLM's wording and the timings move.") if same and pa == pb else \
        "The decision moved. Highlighted cells show what changed since the previous run."
    st.html(f"<div class='cmp'><div class='eyebrow' style='color:var(--yellow);margin-bottom:8px'>Compared with the previous run</div>"
            f"<div class='cmpgrid'>{cells}</div><div class='muted' style='font-size:12.5px;margin-top:8px'>{e(note)}</div></div>")


def alert_preview(case: dict):
    trig = {"risk_score": "Bank risk score", "customer_report": "Customer report",
            "analyst_request": "Analyst request"}.get(case["trigger_type"], case["trigger_type"])
    rs = case.get("risk_score") or ""
    cells = [("Trigger", trig), ("Opened", case.get("opened_at", "")), ("Transaction", case.get("flagged_txn_id", "")),
             ("Card", case.get("card_id") or "resolved by the agent")]
    if rs:
        cells.append(("Bank score", rs))
    st.html(f"<div class='eyebrow'>Incoming alert · not yet investigated in this session</div>"
            f"<h1 class='page'>{e(case['case_id'])}</h1>"
            f"<div class='alertcard'><div class='quote'>“{e(case['trigger_text'])}”</div>"
            f"<div class='gov' style='grid-template-columns:repeat({len(cells)},1fr);margin-top:14px'>"
            + "".join(f"<div><div class='eyebrow'>{e(k)}</div><div class='v mono'>{e(v)}</div></div>" for k, v in cells)
            + "</div></div>"
            "<div class='empty' style='margin-top:18px'><div class='gtext' style='font:600 30px var(--display);"
            "letter-spacing:-.02em;display:inline-block'>Ready to investigate</div><div style='margin-top:6px'>Press <b>Run investigation</b>. "
            "The agent will query TigerGraph through MCP, weigh the evidence, decide the next best action and write "
            "the case to the graph. Past results live in the <a href='./portfolio' target='_self' style='color:var(--blue)'>"
            "Case Portfolio</a>.</div></div>")


def _investigate_client():
    from agent import live
    page_head("Investigations", "Investigate",
              "Send any transaction to the agent. It runs on the live queue, like alerts from the bank's systems, so a case "
              "that needs the cardholder waits for their real reply.")
    live.use_backend(backend())
    with st.form("adhoc"):
        c1, c2 = st.columns([1.2, 1])
        txn = c1.text_input("Transaction id", placeholder="as loaded through Data intake")
        trig = c2.selectbox("Why", ["analyst_request", "risk_score", "customer_report"],
                            format_func={"analyst_request": "Analyst review", "risk_score": "Rule or model hit",
                                         "customer_report": "Customer says it was not them"}.get)
        text = st.text_input("Note for the case", "Analyst request: review this transaction and related activity.")
        go = st.form_submit_button("Investigate", type="primary", disabled=not allowed("investigate"))
    if go and txn.strip():
        try:
            x = backend().txn_context(txn.strip())["txn"]
        except Exception:  # noqa: BLE001
            st.error(f"Transaction {txn} is not in the graph. Load it through Data intake first.")
            return
        aid = f"ADHOC-{datetime.now():%m%d%H%M%S}"
        live.publish({"case_id": aid, "opened_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), "trigger_type": trig,
                      "trigger_text": text, "flagged_txn_id": x["id"], "card_id": x["card_id"],
                      "customer_id": x.get("customer_id", ""), "risk_score": ""}, f"console · {me()['email']}")
        A.audit(me()["email"], me()["role"], "investigation_run", aid, {"txn": x["id"]})
        st.success(f"{aid} is on the queue. Open it on the Live queue page when it is done.")
    _live_table()


def page_investigate():
    if CLIENT_DATA:
        return _investigate_client()
    pack = case_pack()
    qp = st.query_params.get("case", "")
    saved_view = st.query_params.get("view", "") == "saved"
    icon = {"risk_score": "bank score", "customer_report": "customer dispute", "analyst_request": "analyst request"}
    opts = [f"{r.case_id}  ·  {icon.get(r.trigger_type, r.trigger_type)}  ·  txn {r.flagged_txn_id}" for r in pack.itertuples()]
    idx = next((i for i, o in enumerate(opts) if o.startswith(qp)), 0) if qp else 0
    llm_probe = LLM(SETTINGS)
    t1, t2, t3, t4 = st.columns([3.2, 1.3, 1.4, 1.2], vertical_alignment="center")
    choice = t1.selectbox("Alert", opts, index=idx, label_visibility="collapsed")
    use_llm = t2.toggle("LLM investigator", value=llm_probe.provider != "none",
                        help="Off = deterministic narrative templates. Graph work and decisions are identical.")
    run = t3.button("Re-run live" if saved_view else "Run investigation", type="primary", use_container_width=True,
                    icon=":material/play_arrow:", disabled=not allowed("investigate"),
                    help=None if allowed("investigate") else "Needs the Analyst role or above")
    with t4.popover("Custom alert", use_container_width=True):
        c_txn = st.text_input("Transaction ID", "3503988")
        c_trig = st.selectbox("Trigger", ["risk_score", "customer_report", "analyst_request"])
        c_text = st.text_area("Trigger text", "Analyst request: review this transaction and related activity.")
        run_custom = st.button("Investigate custom alert", use_container_width=True, disabled=not allowed("investigate"))
    cid = choice.split()[0]
    case = pack[pack.case_id == cid].iloc[0].to_dict()
    if cid != qp:  # a different alert was picked: leave the saved view
        st.query_params.clear()
        st.query_params["case"] = cid
        saved_view = False
    if run_custom:
        ctx = backend().txn_context(c_txn)["txn"]
        case = {"case_id": f"ADHOC-{c_txn[-3:]}", "opened_at": ctx["ts"][:10] + " 23:59:00", "trigger_type": c_trig,
                "trigger_text": c_text, "flagged_txn_id": c_txn, "card_id": ctx["card_id"],
                "customer_id": ctx["customer_id"], "risk_score": ""}
        run = True
    if run:
        if not use_llm:
            SETTINGS.llm_provider = "none"
        prev, _ = load_saved(case["case_id"])
        g = backend()
        llm = LLM(SETTINGS)
        box = st.status("Agent investigating on TigerGraph …", expanded=True)

        def emit(ev):
            label, color = KIND.get(ev["kind"], (ev["kind"], "#9AA0A6"))
            d = ev.get("detail") or {}
            x = f"{str(d.get('transport', '')).upper()} · {float(d.get('latency_s', 0) or 0):.2f}s" \
                if ev["kind"] == "tool" and isinstance(d, dict) else ""
            box.html(f"<div class='lv'><div class='k' style='color:{color}'>{e(label)}</div>"
                     f"<div>{e(vm.short(ev['title'], 150))}</div><div class='x'>{x}</div></div>")

        A.audit(me()["email"], me()["role"], "investigation_run", case["case_id"], {"txn": case["flagged_txn_id"]})
        inv = Investigation(g, llm, case, emit=emit, use_llm_investigator=use_llm)
        answer = inv.run()
        box.update(label=f"Investigation complete · {answer['tool_calls']} graph calls · {answer['latency_s']}s",
                   state="complete", expanded=False)
        when = datetime.now().strftime("%d %b %Y, %H:%M:%S")
        st.session_state["result"] = (case, answer, inv.events)
        st.session_state["live"] = {"case_id": case["case_id"], "when": when, "prev": prev}
        if case["case_id"].startswith("ALR-"):
            (SETTINGS.cases_dir / f"{case['case_id']}.json").write_text(json.dumps(answer, indent=2))
            (SETTINGS.traces_dir / f"{case['case_id']}.json").write_text(json.dumps(
                {"case": case, "events": inv.events, "llm": {"provider": llm.provider, "model": llm.model}},
                indent=1, default=str))
        if saved_view:
            st.query_params.pop("view", None)
        run_banner("live", answer, when, inv.events)
        compare_strip(prev, answer)
        show_investigation(case, answer, inv.events)
        return
    live = st.session_state.get("live")
    res = st.session_state.get("result")
    if live and res and live["case_id"] == case["case_id"] and res[1]["case_id"] == case["case_id"] and not saved_view:
        # a live run from this session (e.g. after pressing Approve): keep showing it
        run_banner("live", res[1], live["when"], res[2])
        compare_strip(live["prev"], res[1])
        show_investigation(*res)
        return
    if saved_view:
        a, ev = load_saved(case["case_id"])
        if a:
            st.session_state["result"] = (case, a, ev)
            run_banner("saved", a, saved_time(case["case_id"]))
            show_investigation(case, a, ev)
            return
    alert_preview(case)


def page_portfolio():
    data = all_answers()
    page_head("Investigations", "Case portfolio", "Twenty benchmark alerts, each investigated end to end and written to TigerGraph.")
    if not data:
        st.html("<div class='empty'>No answer files yet. Run the agent on a case.</div>")
        return
    cnt = Counter(a["case"]["verdict"] for a in data)
    sars = sum(1 for a in data if a["sar"]["file"])
    exp = sum(a["case"]["exposure_usd"] for a in data if a["case"]["verdict"] == "fraud")
    pend = sum(1 for a in data for x in a["next_best_actions"]["final"] if x["route"] != "auto")
    cells = [("Cases", len(data), ""), ("Fraud", cnt["fraud"], "var(--pink)"), ("Legitimate", cnt["legitimate"], "var(--legit)"),
             ("Uncertain", cnt["uncertain"], "var(--yellow)"), ("SARs drafted", sars, ""), ("Awaiting approval", pend, "var(--yellow)")]
    st.html("<div class='kpis'>" + "".join(f"<div class='kpi'><div class='eyebrow'>{k}</div><div class='n' style='color:{c or 'var(--ink)'}'>"
                                           f"{n}</div></div>" for k, n, c in cells) + "</div>"
            f"<div class='muted' style='font-size:13px;margin:-4px 0 12px'>Fraud exposure identified: "
            f"<b class='mono' style='color:var(--ink)'>{money(exp)}</b></div>")
    rows = []
    traces = all_traces()
    pack = case_pack().set_index("case_id")
    for a in data:
        c = a["case"]
        ev = traces.get(a["case_id"], [])
        v = vm.build(pack.loc[a["case_id"]].to_dict() if a["case_id"] in pack.index else {}, a, ev)
        chip_cls = STATUS_CHIP.get(c["status"], ("", ""))[0]
        head = vm.ACTION_TEXT.get(v.headline.get("action"), "—")
        sar = SAR_YES if a["sar"]["file"] else SAR_NO
        rows.append(f"<tr><td><a href='./?case={e(a['case_id'])}&view=saved' target='_self'>{e(a['case_id'])}</a></td>"
                    f"<td><span class='chip {chip_cls}'>{e(OUTCOME.get(c['verdict'], (c['verdict'],))[0])}</span></td>"
                    f"<td class='num'>{pct(v.p_final)}</td><td class='num'>{v.final.confidence * 100:.0f}%</td>"
                    f"<td>{e(c['pattern'].replace('_', ' '))}</td><td class='num'>{money(c['exposure_usd'])}</td>"
                    f"<td class='num'>{len(c['connected_card_ids'])}</td><td>{sar}</td>"
                    f"<td style='color:var(--ink)'>{e(head)}<div class='acts'>{e(v.headline.get('route', ''))}</div></td></tr>")
    st.html("<div class='tbl'><table><thead><tr><th>Case</th><th>Outcome</th><th style='text-align:right'>Fraud prob.</th>"
            "<th style='text-align:right'>Confidence</th><th>Pattern</th><th style='text-align:right'>Exposure</th>"
            "<th style='text-align:right'>Linked cards</th><th>SAR</th><th>Next best action</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table></div>")


def page_alerts():
    page_head("Investigations", "Alert queue", "Inbound alerts from the case pack, plus alerts the agent raised itself by monitoring the graph.")
    pack = case_pack()
    done = {p.stem for p in SETTINGS.cases_dir.glob("ALR-*.json")}
    rows = []
    for r in pack.itertuples():
        state = "<span class='chip c-legit'>investigated</span>" if r.case_id in done else "<span class='chip c-warn'>new</span>"
        rows.append(f"<tr><td><a href='./?case={e(r.case_id)}&view=saved' target='_self'>{e(r.case_id)}</a></td>"
                    f"<td class='mono' style='white-space:nowrap'>{e(r.opened_at)}</td><td>{e(r.trigger_type.replace('_', ' '))}</td>"
                    f"<td class='mono'>{e(r.flagged_txn_id)}</td><td>{e(vm.short(r.trigger_text, 120))}</td><td>{state}</td></tr>")
    t1, t2 = st.tabs([f"Inbound alerts · {len(pack)}", "Proactive · raised by the agent"])
    with t1:
        st.html("<div class='tbl'><table><thead><tr><th>Alert</th><th>Opened</th><th>Trigger</th><th>Txn</th><th>Alert text</th>"
                "<th>Status</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>")
    with t2:
        p = ROOT / "proactive" / "alerts.json"
        if not p.exists():
            st.html("<div class='empty'>Run <span class='mono'>python monitor.py</span> to scan the graph for rings and structuring.</div>")
            return
        al = json.loads(p.read_text())
        rings = sorted(al.get("device_rings", []), key=lambda r: -float(r.get("score", 0)))
        struct = al.get("structuring", [])
        st.html("<div class='kpis' style='grid-template-columns:repeat(3,1fr)'>"
                f"<div class='kpi'><div class='eyebrow'>Device rings</div><div class='n'>{len(rings)}</div><div class='l'>one device profile, many cards</div></div>"
                f"<div class='kpi'><div class='eyebrow'>Structuring bursts</div><div class='n'>{len(struct)}</div><div class='l'>just under $500, within an hour</div></div>"
                f"<div class='kpi'><div class='eyebrow'>Connected components</div><div class='n'>{len(al.get('wcc_components', []))}</div>"
                f"<div class='l'>WCC over client ↔ device</div></div></div>")
        pro = []
        for f in sorted((ROOT / "proactive" / "cases").glob("PRO-*.json")):
            a = json.loads(f.read_text())
            c = a["case"]
            chip_cls = STATUS_CHIP.get(c["status"], ("", ""))[0]
            pro.append(f"<tr><td class='mono'>{e(a['case_id'])}</td><td><span class='chip {chip_cls}'>{e(c['verdict'])}</span></td>"
                       f"<td class='num'>{pct(c['fraud_probability'])}</td><td>{e(c['pattern'].replace('_', ' '))}</td>"
                       f"<td class='num'>{money(c['exposure_usd'])}</td><td class='num'>{len(c['connected_card_ids'])}</td>"
                       f"<td>{e(vm.short(c['summary'], 140))}</td></tr>")
        if pro:
            st.html("<div class='eyebrow' style='margin:6px 0 8px'>Cases the agent opened on its own</div><div class='tbl'><table><thead><tr>"
                    "<th>Case</th><th>Outcome</th><th style='text-align:right'>Fraud prob.</th><th>Pattern</th>"
                    "<th style='text-align:right'>Exposure</th><th style='text-align:right'>Linked cards</th><th>Summary</th>"
                    "</tr></thead><tbody>" + "".join(pro) + "</tbody></table></div>")
        rr = "".join(f"<tr><td class='mono' style='font-size:12px'>{e(vm.short(r['device'], 60))}</td>"
                     f"<td class='num'>{len(r['cards'])}</td><td class='num'>{e(r['txns'])}</td>"
                     f"<td class='num'>{float(r['new_share']) * 100:.0f}%</td><td class='num'>{float(r['proxy_share']) * 100:.0f}%</td>"
                     f"<td class='num'>{float(r['score']):.1f}</td></tr>" for r in rings[:12])
        st.html("<div class='eyebrow' style='margin:18px 0 8px'>Top device rings</div><div class='tbl'><table><thead><tr><th>Device profile</th>"
                "<th style='text-align:right'>Cards</th><th style='text-align:right'>Txns</th><th style='text-align:right'>New device</th>"
                "<th style='text-align:right'>Proxy</th><th style='text-align:right'>Ring score</th></tr></thead><tbody>" + rr + "</tbody></table></div>")


def case_picker(key: str):
    ids = [p.stem for p in sorted(SETTINGS.cases_dir.glob("ALR-*.json"))]
    if not ids:
        st.html("<div class='empty'>No investigated cases yet.</div>")
        return None
    res = st.session_state.get("result")
    default = ids.index(res[1]["case_id"]) if res and res[1]["case_id"] in ids else 0
    cid = st.selectbox("Case", ids, index=default, key=key)
    a, ev = load_saved(cid)
    if not a:
        return None
    pack = case_pack().set_index("case_id")
    return (pack.loc[cid].to_dict() if cid in pack.index else {}), a, ev


def page_graph():
    page_head("Intelligence", "Investigation graph", "The TigerGraph neighbourhood the agent traversed for a case: "
              "transactions, cards, shared devices, connected cards, and the closed cases used as memory.")
    got = case_picker("g_case")
    if got:
        _, a, ev = got
        show_graph(a, ev, height=640)


def page_patterns():
    page_head("Intelligence", "Fraud patterns", "What the agent found across the benchmark, next to what the bank's closed cases record.")
    data = all_answers()
    cc = closed_cases()
    agent = Counter(a["case"]["pattern"] for a in data if a["case"]["verdict"] != "legitimate")
    fr = cc[cc.outcome == "confirmed_fraud"]
    mem_n = fr.groupby("pattern").case_id.count().to_dict()
    mem_x = fr.groupby("pattern").exposure_usd.mean().to_dict()
    pats = sorted(set(agent) | set(mem_n), key=lambda p: (-agent.get(p, 0), -mem_n.get(p, 0)))
    cards = []
    for p in pats:
        n_mem = int(mem_n.get(p, 0))
        undocumented = n_mem == 0
        avg = f" · avg {money(float(mem_x[p]))}" if n_mem else ""
        cards.append(f"<div class='kpi'><div class='eyebrow'>{'undocumented · new' if undocumented else 'documented pattern'}</div>"
                     f"<div style='font:700 17px var(--display);color:{'var(--pink)' if undocumented else 'var(--ink)'};margin-top:6px'>"
                     f"{e(p.replace('_', ' '))}</div>"
                     f"<div class='l' style='margin-top:8px'><b class='mono' style='color:var(--yellow)'>{agent.get(p, 0)}</b> agent case(s) · "
                     f"<b class='mono' style='color:var(--ink)'>{n_mem}</b> closed case(s){avg}</div></div>")
    st.html("<div style='display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px'>" + "".join(cards) + "</div>")
    doc = SETTINGS.knowledge_dir / "fraud_patterns.md"
    if doc.exists():
        with st.expander("Pattern playbook (knowledge base used by GraphRAG)"):
            st.markdown(doc.read_text(encoding="utf-8").replace("$", "\\$"))


def page_memory():
    page_head("Intelligence", "Case memory", "Search the bank's closed cases by meaning: TigerGraph native vector search over case embeddings.")
    q = st.text_input("Describe a situation", "new device online purchase, several cards share the same device profile, amounts nearly identical")
    k = st.slider("Results", 3, 15, 8)
    try:
        hits = backend().similar_closed_cases(embed(q), k)
    except Exception as ex:  # noqa: BLE001
        st.error(f"Vector search failed: {ex}")
        return
    st.html("<div class='evgrid'>" + "".join(memory_card(str(h.get("case_id") or h.get("id")), h, s) for h, s in hits) + "</div>")


def _inline(t: str) -> str:
    t = e(t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"`([^`]+)`", r"<code class='pc'>\1</code>", t)
    return t


def md_html(text: str, drop_heading: bool = True) -> str:
    """Tiny markdown renderer for policy chunks: headings, paragraphs, bullet lists, pipe tables, bold, code."""
    lines = text.strip().splitlines()
    if drop_heading and lines and lines[0].lstrip().startswith("#"):
        lines = lines[1:]  # the section name is already shown in the card header
    out, para, table, items = [], [], [], []

    def flush():
        nonlocal para, table, items
        if para:
            out.append(f"<p>{_inline(' '.join(para))}</p>")
        if items:
            out.append("<ul>" + "".join(f"<li>{_inline(x)}</li>" for x in items) + "</ul>")
        if table:
            rows = [[c.strip() for c in r.strip().strip("|").split("|")] for r in table
                    if not re.fullmatch(r"\|?[\s:|-]+\|?", r.strip())]
            if rows:
                head, body = rows[0], rows[1:]
                out.append("<table class='pt'><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head)
                           + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r)
                                                              + "</tr>" for r in body) + "</tbody></table>")
        para, table, items = [], [], []

    for ln in lines:
        t = ln.strip()
        if not t:
            flush()
        elif t.startswith("|"):
            if para or items:
                flush()
            table.append(t)
        elif t.startswith("#"):
            flush()
            out.append(f"<div class='ph'>{_inline(t.lstrip('#').strip())}</div>")
        elif re.match(r"^[-*] ", t):
            if para or table:
                flush()
            items.append(t[2:])
        else:
            if table or items:
                flush()
            para.append(t)
    flush()
    return "<div class='md'>" + "".join(out) + "</div>"


def page_policies():
    from agent.policy_config import POLICY as PC
    t, m, f = PC.t, PC.money, PC.fmt
    page_head("Intelligence", "Policies",
              f"{PC.label} decides every action. The numbers below are read from the policy file; the language model "
              f"cannot change them. GraphRAG retrieves the clause the agent cites.")
    signed = PC.approved_by or "not recorded"
    st.html("<div class='gov' style='grid-template-columns:repeat(4,1fr)'>"
            f"<div><div class='eyebrow'>Active policy</div><div class='v'>{e(PC.label)}</div></div>"
            f"<div><div class='eyebrow'>Effective from</div><div class='v'>{e(PC.effective_from or 'not set')}</div></div>"
            f"<div><div class='eyebrow'>Approved by</div><div class='v'>{e(signed)}</div></div>"
            f"<div><div class='eyebrow'>Fingerprint</div><div class='v'><code>{e(PC.fingerprint)}</code></div></div></div>")
    if not PC.approved_by:
        st.warning("This policy version has no approver recorded. Set policy.approved_by in the policy file before go-live.")

    rows = [
        ("Act as fraud", f"probability >= {t.fraud_stop:.2f} on {t.min_independent_evidence}+ independent lines of evidence", "Policy 6"),
        ("Close as legitimate", f"probability <= {t.legit_stop:.2f} on {t.min_independent_evidence}+ independent lines of evidence", "Policy 6"),
        ("Hold pending authorisation", f"probability >= {t.hold_authorisation:.2f}; never block on one weak signal below this", "R1"),
        ("Open a case", f"probability >= {t.open_case:.2f}, evidence requested, or any customer dispute", "3a"),
        ("Monitor linked cards", f"probability >= {t.monitor_connected_cards:.2f} while verification is pending", "R6"),
        ("Step-up instead of a call", f"online risk-score alerts below {t.step_up_below:.2f}", "R1"),
        ("Block card: team lead", f"exposure up to {f(m.block_card_team_lead_max)}; above it a fraud manager", "Routing"),
        ("Suspicious activity report", f"confirmed fraud with exposure over {f(m.report_over)}, or linked to other cards", "3a, R6, R9"),
        ("Escalate to an analyst", f"unresolved or conflicting cases over {f(m.escalate_over)}", "R4, R8"),
        ("Card testing: block, not decline", f"a purchase over {f(m.card_testing_large_purchase)} already cleared", "R5"),
    ]
    st.html("<div class='eyebrow' style='margin:22px 0 8px'>Decision thresholds</div>"
            "<div class='tbl'><table style='min-width:0'><tr><th>Decision</th><th>When</th><th>Rule</th></tr>"
            + "".join(f"<tr><td>{e(a)}</td><td>{e(b)}</td><td>{e(c)}</td></tr>" for a, b, c in rows) + "</table></div>")

    def names(xs):
        return ", ".join(x.replace("_", " ").lower() for x in sorted(xs))
    st.html("<div class='eyebrow' style='margin:22px 0 8px'>Approval routes</div><div class='gov' style='grid-template-columns:repeat(3,1fr)'>"
            f"<div><div class='eyebrow'>auto · agent executes</div><div class='v'>{e(names(PC.auto)).capitalize()}</div></div>"
            f"<div><div class='eyebrow'>L1 · team lead</div><div class='v'>{e(names(PC.l1)).capitalize()}; block card up to "
            f"{e(f(m.block_card_team_lead_max))} exposure</div></div>"
            f"<div><div class='eyebrow'>L2 · fraud manager</div><div class='v'>Block card above {e(f(m.block_card_team_lead_max))}; "
            f"{e(names(PC.l2))}</div></div></div>")
    st.caption("To change a threshold: edit config/policy.yaml, bump policy.version, record the approver and redeploy. "
               "The app refuses to start if the file is inconsistent. Every case records the policy version and fingerprint "
               "that decided it.")

    st.html("<div class='eyebrow' style='margin:26px 0 8px'>Ask the policy</div>")
    q = st.text_input("Ask the policy", "when may the agent block a card without approval?", label_visibility="collapsed")
    try:
        hits = backend().policy_search(embed(q), 5)
    except Exception as ex:  # noqa: BLE001
        st.error(f"GraphRAG search failed: {ex}")
        hits = []
    out = []
    for h, s in hits:
        out.append(f"<div class='ev neutral'><div class='m' style='margin:0 0 6px'><span class='badge rag'>GRAPHRAG</span>"
                   f"<span class='ref'>{e(h.get('doc'))} · {e(h.get('section'))}</span><span class='lr'>relevance {s:.2f}</span></div>"
                   f"{md_html(str(h.get('text', '')))}</div>")
    st.html("<div class='evgrid'>" + "".join(out) + "</div>")
    doc = SETTINGS.knowledge_dir / "fraud_policy.md"
    if doc.exists():
        with st.expander("Full policy text"):
            st.markdown(doc.read_text(encoding="utf-8").replace("$", "\\$"))
    with st.expander("Policy file (read-only)"):
        try:
            st.code(open(PC.source, encoding="utf-8").read(), language="yaml")
        except OSError:
            st.caption(PC.source)


def _health_panel():
    """Operational health from the live queue and the action log (the same signals Cloud Monitoring alerts on)."""
    from agent import actions as ACT
    from agent import live
    docs = live.store().recent(2000)
    now_ = datetime.utcnow()

    def age_min(ts):
        try:
            return (now_ - datetime.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S")).total_seconds() / 60
        except ValueError:
            return 0
    stuck = [d for d in docs if d.get("status") in ("queued", "investigating", "resuming")
             and age_min(d.get("received_at")) > 10]
    errors = [d for d in docs if d.get("status") == "error" and age_min(d.get("finished_at") or d.get("received_at")) < 24 * 60]
    overdue = [d for d in docs if d.get("status") == "awaiting_reply" and d.get("reply_due", "9") < now_.strftime("%Y-%m-%dT%H:%M:%SZ")]
    failed = [x for x in ACT.recent(500) if x.get("status") == "failed"]
    secs = sorted(float(d["seconds"]) for d in docs if d.get("status") == "done" and d.get("seconds"))
    p95 = secs[int(0.95 * (len(secs) - 1))] if secs else None
    cells = [("Stuck over 10 min", len(stuck)), ("Errors, last 24 h", len(errors)), ("Replies overdue", len(overdue)),
             ("Actions not delivered", len(failed)),
             ("Investigation time p95", f"{p95:.1f} s" if p95 is not None else "—")]
    st.html("<div class='kpis'>" + "".join(
        f"<div class='kpi'><div class='eyebrow'>{k}</div><div class='n' style='color:"
        f"{'var(--pink)' if isinstance(v, int) and v else 'var(--ink)'}'>{v}</div><div class='l'>live queue</div></div>"
        for k, v in cells) + "</div>")
    for d in (stuck + errors)[:5]:
        st.caption(f"{d['alert_id']}: {d.get('status')} {e(d.get('error', '') or '')[:160]}")
    for x in failed[:5]:
        st.caption(f"{x.get('event_id')}: not delivered ({x.get('detail', '')[:120]})")


def page_activity():
    page_head("System", "Agent activity", "Every graph call the agent made, how it was made, and how long it took.")
    sec("Health", "same signals as the Cloud Monitoring alerts")
    _health_panel()
    traces = all_traces()
    calls = []
    for cid, evs in traces.items():
        for ev in evs:
            if ev["kind"] == "tool" and isinstance(ev.get("detail"), dict):
                calls.append({"case": cid, "query": ev["title"].split("(")[0], "transport": str(ev["detail"].get("transport", "")),
                              "latency": float(ev["detail"].get("latency_s", 0) or 0)})
    if not calls:
        st.html("<div class='empty'>No traces yet.</div>")
        return
    df = pd.DataFrame(calls)
    mcp = (df.transport == "mcp").mean()
    llm = sum(1 for evs in traces.values() for ev in evs if ev["kind"] == "llm")
    st.html("<div class='kpis' style='grid-template-columns:repeat(4,1fr)'>"
            f"<div class='kpi'><div class='eyebrow'>Graph calls</div><div class='n'>{len(df)}</div><div class='l'>{len(traces)} cases</div></div>"
            f"<div class='kpi'><div class='eyebrow'>Via TigerGraph MCP</div><div class='n'>{mcp * 100:.0f}%</div><div class='l'>run_installed_query</div></div>"
            f"<div class='kpi'><div class='eyebrow'>Median latency</div><div class='n'>{df.latency.median():.2f}s</div><div class='l'>per graph call</div></div>"
            f"<div class='kpi'><div class='eyebrow'>LLM steps</div><div class='n'>{llm}</div><div class='l'>bounded investigator + writer</div></div></div>")
    g = df.groupby("query").agg(calls=("case", "count"), cases=("case", "nunique"), p50=("latency", "median"),
                                maxl=("latency", "max")).sort_values("calls", ascending=False)
    m = g.calls.max()
    rows = "".join(f"<tr><td class='mono' style='color:var(--ink)'>{e(q)}</td><td style='width:38%'><div style='height:8px;border-radius:4px;"
                   f"background:var(--green);width:{r.calls / m * 100:.0f}%'></div></td><td class='num'>{int(r.calls)}</td>"
                   f"<td class='num'>{int(r.cases)}</td><td class='num'>{r.p50:.2f}s</td><td class='num'>{r.maxl:.2f}s</td></tr>"
                   for q, r in g.iterrows())
    st.html("<div class='tbl'><table><thead><tr><th>Installed query</th><th></th><th style='text-align:right'>Calls</th>"
            "<th style='text-align:right'>Cases</th><th style='text-align:right'>p50</th><th style='text-align:right'>Max</th>"
            "</tr></thead><tbody>" + rows + "</tbody></table></div>")


def _audit_text(action: str, d: dict) -> str:
    t = {"action_approved": lambda: f"{d.get('action')} approved ({d.get('route')})",
         "action_rejected": lambda: f"{d.get('action')} rejected ({d.get('route')})",
         "action_agreed": lambda: f"shadow: agrees with {d.get('action')} ({d.get('route')})",
         "action_disagreed": lambda: f"shadow: disagrees with {d.get('action')} ({d.get('route')})",
         "action_send_failed": lambda: f"action not delivered to the bank's systems: {d.get('detail')}",
         "role_changed": lambda: f"role of {d.get('user')}: {d.get('from')} → {d.get('to')}",
         "user_created": lambda: f"user created as {d.get('role')} ({d.get('how')})",
         "dispute_submitted": lambda: f"customer dispute sent to the agent ({d.get('language')})",
         "investigation_run": lambda: f"investigation run on txn {d.get('txn')}",
         "customer_reply_recorded": lambda: f"cardholder reply recorded: {d.get('reply')} via {d.get('channel') or 'unknown'}",
         "outcome_recorded": lambda: f"real outcome recorded: {d.get('outcome')} ({d.get('source')})",
         "api_key_created": lambda: f"API key {d.get('key_id')} created ({d.get('name')}, {', '.join(d.get('scopes') or [])})",
         "api_key_revoked": lambda: f"API key {d.get('key_id')} revoked",
         "input_flagged": lambda: f"input flagged ({d.get('source')}): {'; '.join((d.get('reasons') or [])[:2])}"}.get(action)
    if t:
        return t()
    if action.startswith("data_"):
        return (f"{action[5:].replace('_', ' ')}: {d.get('rows_ok')} of {d.get('rows_in')} rows "
                f"(batch {d.get('batch')}, {d.get('source')})")
    return action.replace("_", " ")


def page_audit():
    page_head("System", "Audit log", "What the agent executed on its own, what it queued for a human, and what humans decided.")
    traces = all_traces()
    rows = []
    for a_ in A.recent_audit(500):
        d_ = json.loads(a_.get("detail") or "{}")
        what = _audit_text(a_["action"], d_)
        kind = "approval" if a_["action"].startswith("action_") and "send_failed" not in a_["action"] else (
            "guardrail" if ("failed" in a_["action"] or "flagged" in a_["action"]) else "human")
        rows.append((a_["ts"].replace("T", " ").rstrip("Z"), a_.get("case_id") or "·", kind, what,
                     f"{a_['actor']} · {A.ROLE_LABEL.get(a_['role'], a_['role'])}"))
    for cid, evs in traces.items():
        for ev in evs:
            d = ev.get("detail") or {}
            if ev["kind"] == "execute" and isinstance(d, dict):
                for x in d.get("executed", []):
                    rows.append((ev["ts"], cid, "executed", x, "agent · auto"))
                for x in d.get("awaiting", []):
                    rows.append((ev["ts"], cid, "queued", x, "awaiting human"))
            elif ev["kind"] in ("memory", "guardrail", "approval"):
                rows.append((ev["ts"], cid, ev["kind"], vm.short(ev["title"], 120), "agent"))
    kind_chip = {"executed": "c-legit", "queued": "c-warn", "approval": "c-legit", "guardrail": "c-crit", "memory": "",
                 "human": ""}
    body = "".join(f"<tr><td class='mono' style='white-space:nowrap'>{e(t)}</td>"
                   f"<td><a href='./?case={e(c)}&view=saved' target='_self'>{e(c)}</a></td>"
                   f"<td><span class='chip {kind_chip.get(k, '')}'>{e(k)}</span></td><td style='color:var(--ink)'>{e(x)}</td>"
                   f"<td class='muted'>{e(who)}</td></tr>" for t, c, k, x, who in rows)
    st.html(f"<div class='muted' style='font-size:13px;margin-bottom:10px'>{len(rows)} entries · every case's events are also stored "
            "in TigerGraph as <span class='mono'>CaseEvent</span> vertices linked to its <span class='mono'>FraudCase</span>.</div>"
            "<div class='tbl'><table><thead><tr><th>Time</th><th>Case</th><th>Type</th><th>Action / event</th><th>By</th></tr></thead>"
            "<tbody>" + body + "</tbody></table></div>")


def page_agent():
    if not allowed("agent"):
        auth.denied("agent")
        return
    page_head("Investigations", "Ask the agent",
              "A Gemini agent built with Google's Agent Development Kit. It picks the TigerGraph tools, investigates and "
              "explains. Verdicts, probabilities and actions still come from the policy engine, and a guardrail removes "
              "claims the graph evidence does not support.")
    try:
        from sentinel_adk import agent as adk_agent
        from sentinel_adk.chat import Chat
    except Exception as ex:  # noqa: BLE001
        st.error(f"ADK agent unavailable: {ex}. Install it with `pip install google-adk`.")
        return
    adk_agent.use_backend(backend())
    if "adk_chat" not in st.session_state:
        st.session_state["adk_chat"] = Chat()
        st.session_state["adk_log"] = []
    log = st.session_state["adk_log"]
    c1, c2 = st.columns([5, 1], vertical_alignment="center")
    c1.caption(f"Model: {adk_agent.MODEL} · "
               + ("Vertex AI" if str(__import__('os').getenv("GOOGLE_GENAI_USE_VERTEXAI", "")).lower() in ("true", "1")
                  else "Gemini API") + " · tools: " + ", ".join(t.__name__ for t in adk_agent.root_agent.tools))
    if c2.button("New chat", use_container_width=True):
        st.session_state.pop("adk_chat")
        st.rerun()
    if not log:
        st.html("<div class='sub' style='margin:6px 0 14px'>Try: <b>Investigate ALR-003</b> · "
                "<b>How are the cards in ALR-006 linked?</b> · <b>Why does blocking need a team lead?</b> · "
                "<b>Investigate transaction 3475414, the customer says it was not them</b></div>")
    for m in log:
        with st.chat_message(m["role"]):
            if m.get("steps"):
                with st.expander(f"{sum(1 for x in m['steps'] if x['kind'] == 'call')} tool call(s)"):
                    for x in m["steps"]:
                        if x["kind"] == "call":
                            st.code(f"{x['name']}({json.dumps(x['args'])})", language="python")
            st.markdown(m["text"].replace("$", "\\$"))
    q = st.chat_input("Ask about an alert, a case or the policy")
    if q:
        log.append({"role": "user", "text": q})
        with st.chat_message("user"):
            st.markdown(q.replace("$", "\\$"))
        with st.chat_message("assistant"):
            with st.spinner("Agent working on TigerGraph ..."):
                try:
                    a, steps = st.session_state["adk_chat"].ask(q)
                except Exception as ex:  # noqa: BLE001
                    a, steps = f"The agent hit an error: {ex}", []
        log.append({"role": "assistant", "text": a or "(no answer)", "steps": steps})
        st.rerun()


# =========================================================================== customer dispute intake + live queue
DEMO_CUSTOMERS = [("C07297", "C07297-K1", "Hindi"), ("C11923", "C11923-K2", "Malayalam"), ("C08623", "C08623-K2", "English"),
                  ("C02354", "C02354-K2", "English"), ("C09988", "C09988-K1", "English")]
LIVE_CHIP = {"queued": ("c-warn", "queued"), "investigating": ("c-warn", "investigating"), "done": ("c-legit", "done"),
             "error": ("c-crit", "error"), "awaiting_reply": ("c-warn", "awaiting reply"), "resuming": ("c-warn", "resuming")}


def _live_mode_label() -> str:
    from agent import live
    return "Pub/Sub → Cloud Run worker → Firestore" if live.MODE == "gcp" else "local queue (set LIVE_BACKEND=gcp on Cloud Run)"


def page_intake():
    if not allowed("intake"):
        auth.denied("intake")
        return
    from agent import intake, live
    page_head("Investigations", "Customer dispute",
              "A customer describes a problem in their own words, in any language. Gemini reads and translates it, the "
              "graph finds the transaction, and the alert goes onto the live stream for the agent to investigate.")
    live.use_backend(backend())
    c1, c2 = st.columns([1.1, 2], gap="large")
    with c1:
        if CLIENT_DATA:
            card = st.text_input("Card id or token", placeholder="the card the customer is calling about").strip()
            cust = st.text_input("Customer id", placeholder="optional").strip() or card
            lang = "English"
            ex_lang = st.radio("Example message", list(intake.EXAMPLES), horizontal=True)
            st.caption("The agent matches the amount the customer wrote against the card's recent transactions. "
                       "An amount that is not on the card gets a follow-up question instead of a guess.")
        else:
            labels = [f"{cu} · card {ca}" for cu, ca, _ in DEMO_CUSTOMERS]
            who = st.selectbox("Signed-in customer (demo)", labels)
            cust, card, lang = DEMO_CUSTOMERS[labels.index(who)]
            ex_lang = st.radio("Example message", list(intake.EXAMPLES), index=list(intake.EXAMPLES).index(lang), horizontal=True)
            st.caption("The examples quote amounts that exist on these cards. Edit the text freely; an amount that is "
                       "not on the card gets a follow-up question instead of a guess.")
    with c2:
        msg = st.text_area("Customer message", intake.EXAMPLES[ex_lang], height=120, key=f"msg_{card}_{ex_lang}")
        go = st.button("Read and send to the agent", type="primary", icon=":material/send:")
    if go and CLIENT_DATA and not card:
        st.warning("Enter the card id first.")
        go = False
    if go and msg.strip():
        with st.spinner("Gemini is reading the message …"):
            ex = intake.extract(msg, LLM(SETTINGS))
        warn = (f"<div class='sub' style='color:var(--warn)'>Gemini suggested ${ex['amount_rejected']:,.2f}, which the customer did "
                f"not write, so it was discarded.</div>" if ex.get("amount_rejected") is not None else "")
        sf = ex.get("safety") or {}
        if sf.get("pii_masked"):
            warn += (f"<div class='sub'>Personal data hidden before Gemini saw the message: "
                     f"{e(', '.join(f'{v} {k.replace(chr(95), chr(32))}' for k, v in sf['pii_masked'].items()))}.</div>")
        if sf.get("flagged"):
            warn += (f"<div class='sub' style='color:var(--warn)'>This message looks like an attempt to instruct the agent "
                     f"({e('; '.join(sf['reasons'][:3]))}). It will go to an analyst instead of being treated as a "
                     f"customer report.</div>")
            A.audit(me()["email"], me()["role"], "input_flagged", "", {"source": "customer dispute", "reasons": sf["reasons"]})
        st.html("<div class='eyebrow' style='margin:14px 0 8px'>1 · Read by Gemini</div><div class='ev neutral'>"
                f"<div class='m' style='margin:0 0 6px'><span class='badge rag'>{e(ex.get('read_by', ''))}</span>"
                f"<span class='ref'>{e(ex.get('language', ''))} · claim: {e(str(ex.get('claim', '')).replace('_', ' '))} · "
                f"amount: {money(ex['amount']) if ex.get('amount') is not None else 'not stated'}</span></div>"
                f"<b>English:</b> {e(ex.get('english', ''))}<br><span class='muted'>{e(ex.get('summary', ''))}</span>{warn}</div>")
        m = intake.match(backend(), card, ex.get("amount"))
        if m["status"] != "matched":
            cands = "".join(f"<tr><td class='mono'>{e(r['id'])}</td><td class='mono'>{e(r['ts'])}</td><td class='num'>{money(r['amount'])}</td>"
                            f"<td>{e(r['channel'])}</td></tr>" for r in m.get("candidates", []))
            st.html("<div class='eyebrow' style='margin:14px 0 8px'>2 · Match in the graph</div>"
                    f"<div class='ev neutral'><b>Follow-up question to the customer:</b> {e(m['question'])}</div>"
                    + (("<div class='tbl' style='margin-top:8px'><table><thead><tr><th>Txn</th><th>Time</th>"
                        "<th style='text-align:right'>Amount</th><th>Channel</th></tr></thead><tbody>" + cands + "</tbody></table></div>")
                       if cands else ""))
            return
        x = m["txn"]
        more = f" ({m['n_matches']} payments of this amount; the most recent was chosen)" if m["n_matches"] > 1 else ""
        st.html("<div class='eyebrow' style='margin:14px 0 8px'>2 · Matched in the graph</div><div class='ev neutral'>"
                f"Transaction <span class='mono'>{e(x['id'])}</span> · {e(x['ts'])} · {money(x['amount'])} · {e(x['channel'])} "
                f"on card <span class='mono'>{e(card)}</span>{e(more)}</div>")
        alert = intake.build_alert(card, cust, msg, ex, m)
        aid = live.publish(alert, f"customer intake · {me()['email']}")
        A.audit(me()["email"], me()["role"], "dispute_submitted", aid, {"txn": alert["flagged_txn_id"],
                                                                         "language": ex.get("language")})
        st.session_state["intake_last"] = aid
        st.html("<div class='eyebrow' style='margin:14px 0 8px'>3 · On the live stream</div><div class='ev neutral'>"
                f"Alert <span class='mono'>{e(aid)}</span> sent ({e(_live_mode_label())}). Follow it below or on the "
                "<a href='./live' target='_self'>Live queue</a>.</div>")
    aid = st.session_state.get("intake_last")
    if aid:
        _intake_status(aid)


@st.fragment(run_every=3)
def _intake_status(aid: str):
    from agent import live
    d = live.store().get(aid) or {}
    cls, txt = LIVE_CHIP.get(d.get("status", "queued"), ("", d.get("status", "")))
    extra = ""
    if d.get("status") == "done":
        extra = (f" · <b>{e(d.get('verdict', ''))}</b> · fraud probability {pct(d.get('fraud_probability', 0))} · "
                 f"{e(', '.join(d.get('final_actions', [])))} · {d.get('seconds', '')}s")
    elif d.get("status") == "investigating":
        extra = f" · {e(vm.short(d.get('last_step', 'querying TigerGraph'), 110))}"
    elif d.get("status") == "error":
        extra = f" · {e(d.get('error', ''))}"
    st.html(f"<div class='sub'><span class='mono'>{e(aid)}</span> <span class='chip {cls}'>{e(txt)}</span>{extra}</div>")


def page_live():
    from agent import live
    page_head("Investigations", "Live queue",
              f"Alerts arriving on the stream and being investigated as they land. Pipeline: {_live_mode_label()}.")
    live.use_backend(backend())
    b1, b2, _ = st.columns([1.3, 1.3, 3])
    if b1.button("Replay bank feed (3 alerts)", icon=":material/podcasts:", use_container_width=True,
                 disabled=not allowed("investigate")):
        off = st.session_state.get("feed_off", 0)
        for a in live.bank_feed(3, off):
            live.publish(a, "bank feed")
        st.session_state["feed_off"] = (off + 3) % 18
        st.toast("3 alerts published to the stream")
    b2.button("Refresh", icon=":material/refresh:", use_container_width=True)
    _live_table()
    done = [d for d in live.store().recent(30) if d.get("status") in ("done", "awaiting_reply")]
    if done:
        pick = st.selectbox("Open an investigated alert", [d["alert_id"] for d in done],
                            format_func=lambda a: a + ("  · awaiting reply" if next(x for x in done if x["alert_id"] == a)
                                                       .get("status") == "awaiting_reply" else ""))
        d = next(x for x in done if x["alert_id"] == pick)
        if d.get("status") == "awaiting_reply":
            _reply_form(d)
        else:
            _outcome_form(d)
        try:
            case, ans, evs = json.loads(d["alert"]), json.loads(d["answer"]), json.loads(d["events"])
        except Exception as ex:  # noqa: BLE001
            st.error(f"Could not load {pick}: {ex}")
            return
        it = case.get("intake")
        if it:
            st.html("<div class='ev neutral' style='margin:10px 0'><b>Customer wrote</b> "
                    f"({e(it.get('language', ''))}): {e(it.get('original', ''))}<br><b>Gemini read it as:</b> "
                    f"{e(it.get('english', ''))}</div>")
        show_investigation(case, ans, evs)


def _reply_form(d: dict):
    from agent import live
    from agent.simulator import CHANNELS
    st.html(f"<div class='ev neutral' style='margin:10px 0'><b>Waiting for the cardholder.</b> The agent asked for "
            f"<b>{e(str(d.get('request_type', '')).replace('_', ' '))}</b>: {e(d.get('request_why', ''))}<br>"
            f"If no reply is recorded by {e(str(d.get('reply_due', '')).replace('T', ' ').rstrip('Z'))} UTC, the case continues "
            f"as <i>no reply</i> (R4). Replies can also arrive through the intake API.</div>")
    if not allowed("record"):
        return
    with st.form(f"reply_{d['alert_id']}"):
        c1, c2 = st.columns([1.4, 1])
        kind = c1.radio("What did the cardholder say?", ["denied", "confirmed", "no_reply"], horizontal=True,
                        format_func={"denied": "Not me (denies it)", "confirmed": "It was me (confirms it)",
                                     "no_reply": "Could not reach them"}.get)
        ch = c2.selectbox("How", [c for c in CHANNELS if c != "timeout"])
        note = st.text_input("Note (optional)", placeholder="e.g. still has the card, did not share an OTP")
        if st.form_submit_button("Record reply and resume the case", type="primary"):
            try:
                live.record_reply(d["alert_id"], kind, note, ch, me())
                st.toast("Reply recorded; the agent is finishing the case")
                time.sleep(1)
                st.rerun()
            except ValueError as ex:
                st.error(str(ex))


def _outcome_form(d: dict):
    from agent import outcomes as O
    prev = O.outcome_map().get(d["alert_id"])
    if prev:
        st.html(f"<div class='muted' style='font-size:13px;margin:8px 0'>Real outcome: <b style='color:var(--ink)'>"
                f"{e(prev['outcome'])}</b> · recorded by {e(prev.get('recorded_by', ''))} "
                f"({e(prev.get('source', ''))}, {e(str(prev.get('recorded_at', ''))[:16].replace('T', ' '))})</div>")
    if not allowed("record"):
        return
    with st.expander("Record what really happened" if not prev else "Correct the real outcome"):
        c1, c2, c3 = st.columns([1.2, 2, 1], vertical_alignment="bottom")
        o = c1.radio("Outcome", O.OUTCOMES, horizontal=True, key=f"o_{d['alert_id']}")
        note = c2.text_input("Note", placeholder="e.g. chargeback received, cardholder confirmed", key=f"on_{d['alert_id']}")
        if c3.button("Save", key=f"os_{d['alert_id']}", type="primary", use_container_width=True):
            O.record(d["alert_id"], o, me(), "console", note)
            st.toast("Outcome recorded")
            st.rerun()


@st.fragment(run_every=3)
def _live_table():
    from agent import live
    if live.MODE != "gcp" and time.time() - st.session_state.get("_swept", 0) > 60:
        st.session_state["_swept"] = time.time()   # locally there is no Cloud Scheduler: check overdue replies here
        live.sweep_overdue()
    docs = live.store().recent(30)
    if not docs:
        st.html("<div class='empty'>No alerts on the stream yet. Send a customer dispute or replay the bank feed.</div>")
        return
    rows = []
    for d in docs:
        cls, txt = LIVE_CHIP.get(d.get("status", ""), ("", d.get("status", "")))
        res = (f"<b>{e(d.get('verdict', ''))}</b> · {pct(d.get('fraud_probability', 0))}" if d.get("status") == "done"
               else f"asked: {e(str(d.get('request_type', '')).replace('_', ' '))} · due {e(str(d.get('reply_due', ''))[5:16].replace('T', ' '))}"
               if d.get("status") == "awaiting_reply"
               else e(vm.short(d.get("last_step", "") or d.get("error", ""), 70)))
        acts = e(", ".join(d.get("final_actions", [])[:3]))
        rows.append(f"<tr><td class='mono' style='white-space:nowrap'>{e(str(d.get('received_at', ''))[11:19])}</td>"
                    f"<td class='mono'>{e(d.get('alert_id', ''))}</td><td>{e(d.get('source', ''))}</td>"
                    f"<td>{e(str(d.get('trigger', '')).replace('_', ' '))}"
                    f"{' <span class=' + chr(39) + 'chip c-crit' + chr(39) + '>flagged</span>' if d.get('flagged') else ''}</td><td class='mono'>{e(d.get('txn_id', ''))}</td>"
                    f"<td><span class='chip {cls}'>{e(txt)}</span></td><td>{res}</td><td>{acts}</td>"
                    f"<td class='num'>{e(d.get('seconds', ''))}</td></tr>")
    st.html("<div class='tbl'><table><thead><tr><th>Received (UTC)</th><th>Alert</th><th>Source</th><th>Trigger</th><th>Txn</th>"
            "<th>Status</th><th>Result</th><th>Next best actions</th><th style='text-align:right'>Seconds</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table></div>")


# =========================================================================== scenario lab
def _lab_emit_box(label: str):
    box = st.status(label, expanded=True)

    def emit(ev):
        lab, color = KIND.get(ev["kind"], (ev["kind"], "#9AA0A6"))
        d = ev.get("detail") or {}
        x = f"{str(d.get('transport', '')).upper()} · {float(d.get('latency_s', 0) or 0):.2f}s" \
            if ev["kind"] == "tool" and isinstance(d, dict) else ""
        box.html(f"<div class='lv'><div class='k' style='color:{color}'>{e(lab)}</div>"
                 f"<div>{e(vm.short(ev['title'], 150))}</div><div class='x'>{x}</div></div>")
    return box, emit


def page_lab():
    if not allowed("lab"):
        auth.denied("lab")
        return
    from agent import scenarios as SC
    from agent.orchestrator import Investigation
    page_head("Evaluate", "Scenario lab",
              "Bring new data: generate a fresh synthetic bank, plant a fraud pattern or a harmless look-alike, and watch "
              "the agent investigate it. Nothing in the data is labelled; the agent has to find the pattern itself, with "
              "the same detectors, policy and stop rule as on the real alerts.")
    kinds = list(SC.SCENARIOS)
    c1, c2 = st.columns([1.15, 1], gap="large")
    with c1:
        kind = st.radio("Scenario", kinds, format_func=lambda k: SC.SCENARIOS[k]["label"], horizontal=True)
        st.caption(SC.SCENARIOS[kind]["blurb"])
        linked, proxy = 5, True
        if kind in ("device_ring", "structuring"):
            linked = st.slider("Other cards involved", 2, 12, 5)
        elif kind == "traveller":
            linked = st.slider("Days away from home", 3, 7, 4)
        if kind in ("device_ring", "card_testing"):
            proxy = st.toggle("Behind a proxy", value=True)
    with c2:
        trig = st.selectbox("How the bank raises it", ["default", "risk_score", "customer_report"],
                            format_func={"default": "Scenario default", "risk_score": "Bank risk score",
                                         "customer_report": "Customer report"}.get)
        rnd = st.toggle("New random world each time", value=True)
        seed = None if rnd else int(st.number_input("World seed", 1, 9999, 42))
        use_llm = st.toggle("Gemini writes the summary", value=False, help="Off: deterministic templates, faster.")
        go = st.button("Generate world and investigate", type="primary", icon=":material/science:", use_container_width=True)
    if go:
        with st.spinner("Generating a synthetic bank …"):
            sc = SC.generate(kind, seed=seed, linked=linked, proxy=proxy, trigger=None if trig == "default" else trig)
        llm = LLM(SETTINGS)
        if not use_llm:
            llm.provider = "none"
        box, emit = _lab_emit_box("Agent investigating the generated world …")
        inv = Investigation(sc.backend, llm, sc.alert, emit=emit, use_llm_investigator=False, replies="simulate")
        ans = inv.run()
        box.update(label=f"Investigation complete · {ans['tool_calls']} queries on the synthetic graph · "
                         f"{'under 0.1' if ans['latency_s'] < 0.1 else ans['latency_s']}s",
                   state="complete", expanded=False)
        st.session_state["lab"] = {"alert": sc.alert, "answer": ans, "events": inv.events, "planted": sc.planted,
                                   "stats": sc.stats, "expect": sc.expect, "kind": kind}
    lab = st.session_state.get("lab")
    if lab:
        a, pl = lab["answer"], lab["planted"]
        got = a["case"]["verdict"]
        match = got == lab["expect"]
        chip = ("<span class='chip c-legit'>matches what was planted</span>" if match else
                "<span class='chip c-warn'>differs from what was planted</span>")
        st.html("<div class='kpis' style='grid-template-columns:repeat(4,1fr)'>"
                f"<div class='kpi'><div class='eyebrow'>Synthetic world</div><div class='n'>{lab['stats']['transactions']:,}</div>"
                f"<div class='l'>transactions · {lab['stats']['cards']} cardholders · seed {pl['seed']}</div></div>"
                f"<div class='kpi'><div class='eyebrow'>Planted</div><div class='n'>{e(SC.SCENARIOS[lab['kind']]['label'])}</div>"
                f"<div class='l'>expected verdict: {e(lab['expect'])}</div></div>"
                f"<div class='kpi'><div class='eyebrow'>Agent concluded</div><div class='n'>{e(got)}</div>"
                f"<div class='l'>fraud probability {pct(a['case']['fraud_probability'])}</div></div>"
                f"<div class='kpi'><div class='eyebrow'>Result</div><div style='margin-top:10px'>{chip}</div>"
                f"<div class='l' style='margin-top:6px'>{len(a['case']['connected_card_ids'])} linked card(s) found</div></div></div>")
        with st.expander("What was planted (the agent never sees this)"):
            st.html(f"<div class='md'><p>{e(pl.get('description', ''))}</p><p class='mono' style='font-size:12px'>"
                    f"cards: {e(', '.join(pl.get('cards', [])))}<br>transactions: {e(', '.join(pl.get('txn_ids', [])[:12]))}"
                    + (f"<br>device: {e(pl['device'])}" if pl.get('device') else "") + "</p>"
                    "<p class='muted'>Synthetic data. Model scores for lab transactions are drawn for the scenario; the "
                    "bank's historical priors quoted in the evidence come from the main bank's memory.</p></div>")
        show_investigation(lab["alert"], a, lab["events"])
    sec("Stress test", "many random worlds")
    n = st.select_slider("Worlds per scenario", [5, 10, 20], value=10)
    if st.button("Run every scenario on fresh random worlds", icon=":material/repeat:"):
        import random as _r
        rows, bar = [], st.progress(0.0)
        total = n * len(kinds)
        k = 0
        for kd in kinds:
            ok = 0
            for _ in range(n):
                rr = _r.Random()
                sc = SC.generate(kd, linked=rr.randint(2, 10), proxy=rr.random() < 0.5)
                llm = LLM(SETTINGS)
                llm.provider = "none"
                v = Investigation(sc.backend, llm, sc.alert, use_llm_investigator=False, replies="simulate").run()["case"]["verdict"]
                ok += v == sc.expect
                k += 1
                bar.progress(k / total)
            rows.append(f"<tr><td>{e(SC.SCENARIOS[kd]['label'])}</td><td>{e(SC.SCENARIOS[kd]['expect'])}</td>"
                        f"<td class='num'>{ok} / {n}</td></tr>")
        st.html("<div class='tbl'><table style='min-width:600px'><thead><tr><th>Scenario</th><th>Planted</th>"
                "<th style='text-align:right'>Agent matched</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>")


# =========================================================================== team & access (admin)
def page_team():
    page_head("System", "Team & access", "Who can use SentinelGraph and what each role may do. Every change is written to "
                                         "the audit log.")
    if not allowed("manage_users"):
        auth.denied("manage_users")
        return
    users = A.list_users()
    rows = "".join(f"<tr><td class='mono'>{e(u['email'])}</td><td>{e(u.get('name', ''))}</td>"
                   f"<td><span class='chip'>{e(A.ROLE_LABEL.get(u.get('role'), u.get('role')))}</span></td>"
                   f"<td class='mono' style='white-space:nowrap'>{e(str(u.get('last_seen', ''))[:16].replace('T', ' '))}</td></tr>"
                   for u in users)
    st.html("<div class='tbl'><table style='min-width:700px'><thead><tr><th>Email</th><th>Name</th><th>Role</th>"
            "<th>Last seen (UTC)</th></tr></thead><tbody>" + rows + "</tbody></table></div>")
    sec("Change a role", "takes effect on their next page load")
    c1, c2, c3 = st.columns([2.2, 1.4, 1], vertical_alignment="bottom")
    known = [u["email"] for u in users]
    em = c1.text_input("Email", placeholder="name@yourbank.com", help="Existing user, or a new one before they first sign in")
    rl = c2.selectbox("Role", A.ROLES, index=1, format_func=A.ROLE_LABEL.get)
    if c3.button("Save role", type="primary", use_container_width=True) and em.strip():
        try:
            A.set_role(me(), em, rl)
            st.toast(f"{em} is now {A.ROLE_LABEL[rl]}")
            st.rerun()
        except (PermissionError, ValueError) as ex:
            st.error(str(ex))
    if known:
        st.caption(f"{len(known)} user(s). New people get the {A.ROLE_LABEL[A.default_role()]} role on first sign-in.")
    sec("What each role may do", "roles include everything to their left")
    head = "".join(f"<th style='text-align:center'>{e(A.ROLE_LABEL[r])}</th>" for r in A.ROLES)
    body = "".join("<tr><td>" + e(A.PERMISSION_LABEL[p]) + "</td>" +
                   "".join(f"<td style='text-align:center'>{'✓' if A.can(r, p) else '·'}</td>" for r in A.ROLES) + "</tr>"
                   for p in A.PERMISSIONS)
    st.html("<div class='tbl'><table><thead><tr><th>Permission</th>" + head + "</tr></thead><tbody>" + body +
            "</tbody></table></div>")
    _api_keys_section()


def _report_html(b: dict) -> None:
    rep = b.get("report") or {}
    status = b["status"]
    tone = {"loaded": "var(--legit)", "checked": "var(--legit)", "queued": "var(--legit)"}.get(status, "var(--fraud)")
    st.html("<div class='gov' style='grid-template-columns:repeat(4,1fr)'>"
            f"<div><div class='eyebrow'>Result</div><div class='v' style='color:{tone}'>{e(status.title())}</div></div>"
            f"<div><div class='eyebrow'>Rows in file</div><div class='v'>{b.get('rows_in', 0):,}</div></div>"
            f"<div><div class='eyebrow'>Passed checks</div><div class='v'>{b.get('rows_ok', 0):,}</div></div>"
            f"<div><div class='eyebrow'>Rejected</div><div class='v'>{b.get('rows_rejected', 0):,}</div></div></div>")
    if b.get("reason"):
        (st.info if status == "checked" else st.error)(b["reason"])
    if rep.get("detectors"):
        chips = "".join(f"<span class='chip' style='margin:0 6px 6px 0;{'' if on else 'opacity:.45;text-decoration:line-through'}'>"
                        f"{e(k)}</span>" for k, on in rep["detectors"].items())
        st.html("<div class='eyebrow' style='margin:14px 0 6px'>Evidence this data can feed</div><div>" + chips + "</div>")
    if rep.get("problems"):
        st.html("<div class='eyebrow' style='margin:14px 0 6px'>Why rows were rejected</div><div class='tbl'><table style='min-width:0'>"
                "<tr><th>Problem</th><th style='text-align:right'>Rows</th></tr>" +
                "".join(f"<tr><td>{e(p['problem'])}</td><td style='text-align:right'>{p['rows']:,}</td></tr>" for p in rep["problems"])
                + "</table></div>")
        with st.expander(f"Rejected rows ({min(len(rep.get('rejected_rows', [])), 500)} shown)"):
            st.dataframe(pd.DataFrame(rep.get("rejected_rows", [])), hide_index=True, use_container_width=True)
    for w in rep.get("warnings", []):
        st.warning(w)


def page_data():
    from agent import live
    from agent.ingest import load as L, mapping as M
    page_head("System", "Data intake",
              "Load a client's transactions and alerts in their own format. Each file is checked row by row against "
              "the client's mapping before anything reaches the graph; the client's systems can send the same files "
              "through the intake API.")
    if not allowed("ingest"):
        auth.denied("ingest")
        return
    names = M.available()
    if not names:
        st.error("No mappings found in config/mappings.")
        return
    c1, c2 = st.columns([1, 2], gap="large")
    name = c1.selectbox("Client format (mapping)", names, index=names.index("sentinelgraph") if "sentinelgraph" in names else 0)
    try:
        m = M.load(name)
    except M.MappingError as ex:
        st.error(f"This mapping cannot be used: {ex}")
        return
    c2.html(f"<div style='padding-top:26px'><b>{e(m.title)}</b><br><span style='color:var(--mute)'>{e(m.description)}</span></div>")
    with st.expander("Mapping file (read-only)"):
        st.code(open(m.source, encoding="utf-8").read(), language="yaml")
    g = backend()
    if g is None:
        return
    t_tx, t_al, t_hist = st.tabs(["Transactions", "Alerts", "Upload history"])
    with t_tx:
        f = st.file_uploader("Transaction file", type=["csv", "tsv", "gz", "jsonl", "json", "xlsx"], key=f"tx_{name}",
                             help="CSV, TSV, gzip CSV, JSON lines, a JSON array or Excel. Up to 200 MB in the console.")
        if f is not None:
            try:
                raw = M.read_file(f.getvalue(), f.name)
            except Exception as ex:  # noqa: BLE001
                st.error(f"Could not read the file: {ex}")
                raw = None
            if raw is not None:
                try:
                    chk = L.ingest_transactions(g, raw, m, me(), "console", dry_run=True)
                except M.MappingError as ex:
                    st.error(str(ex))
                    chk = None
                if chk:
                    _report_html(chk)
                    if chk["status"] == "checked":
                        ok, _ = M.apply(raw, m, "transactions")
                        with st.expander("Preview: the first 20 rows as SentinelGraph will store them"):
                            st.dataframe(ok.head(20), hide_index=True, use_container_width=True)
                        if st.button(f"Load {chk['rows_ok']:,} transactions", type="primary", icon=":material/upload:"):
                            with st.spinner("Loading into the graph..."):
                                b = L.ingest_transactions(g, raw, m, me(), f"console · {f.name}")
                            if b["status"] == "loaded":
                                st.success(f"Loaded {b['rows_ok']:,} transactions on {b.get('cards', 0):,} cards "
                                           f"({b['first_ts']} to {b['last_ts']}) into {b['result']['target']}. Batch {b['batch_id']}.")
                            else:
                                _report_html(b)
    with t_al:
        if not m.alerts:
            st.info("This mapping has no alerts section.")
        else:
            fa = st.file_uploader("Alert file", type=["csv", "tsv", "jsonl", "json", "xlsx"], key=f"al_{name}",
                                  help="Each alert must name a transaction that is already loaded.")
            if fa is not None:
                try:
                    araw = M.read_file(fa.getvalue(), fa.name)
                    _, arep = M.apply(araw, m, "alerts")
                    st.caption(f"{arep.rows_ok:,} alert(s) pass the format checks; each is also checked against the graph "
                               "when it is queued.")
                    if st.button(f"Queue {arep.rows_ok:,} alert(s) for investigation", type="primary", icon=":material/send:",
                                 disabled=not arep.rows_ok or not allowed("investigate")):
                        live.use_backend(g)
                        b = L.ingest_alerts(g, araw, m, me(), f"console · {fa.name}")
                        _report_html(b)
                        if b.get("alert_ids"):
                            st.success(f"Queued {', '.join(b['alert_ids'][:10])}{' ...' if len(b['alert_ids']) > 10 else ''}. "
                                       "Follow them on the Live queue page.")
                except M.MappingError as ex:
                    st.error(str(ex))
                except Exception as ex:  # noqa: BLE001
                    st.error(f"Could not read the file: {ex}")
    with t_hist:
        bs = L.recent_batches(100)
        if not bs:
            st.html("<div class='empty'>No uploads yet.</div>")
        else:
            st.dataframe(pd.DataFrame([{"Batch": b["batch_id"], "Kind": b["kind"], "Status": b["status"],
                                        "Rows in": b.get("rows_in"), "Accepted": b.get("rows_ok"),
                                        "Rejected": b.get("rows_rejected"), "Mapping": b.get("mapping"),
                                        "From": b.get("source"), "By": b.get("actor"),
                                        "Received (UTC)": str(b.get("received_at", ""))[:19].replace("T", " ")}
                                       for b in bs]), hide_index=True, use_container_width=True)


def _api_keys_section():
    from agent.ingest import mapping as M
    sec("API keys", "for the client's systems to call the intake API")
    keys = A.list_api_keys()
    if keys:
        st.dataframe(pd.DataFrame([{"Key id": k["id"], "Name": k.get("name"), "Mapping": k.get("mapping"),
                                    "Scopes": ", ".join(k.get("scopes") or []),
                                    "Status": "revoked" if k.get("revoked") else "active",
                                    "Created": f"{str(k.get('created_at', ''))[:10]} by {k.get('created_by', '')}",
                                    "Last used (UTC)": str(k.get("last_used") or "never")[:16].replace("T", " ")}
                                   for k in keys]), hide_index=True, use_container_width=True)
    with st.form("new_key", clear_on_submit=True):
        c1, c2, c3 = st.columns([1.6, 1.1, 1.6])
        nm = c1.text_input("Name", placeholder="e.g. core banking nightly export")
        names = M.available()
        mp = c2.selectbox("Mapping", names, index=names.index("sentinelgraph") if "sentinelgraph" in names else 0)
        sc = c3.multiselect("Scopes", list(A.KEY_SCOPES), default=list(A.KEY_SCOPES), format_func=A.KEY_SCOPES.get)
        if st.form_submit_button("Create key", type="primary"):
            try:
                st.session_state["_new_key"] = A.create_api_key(me(), nm, mp, sc)
            except (PermissionError, ValueError) as ex:
                st.error(str(ex))
    if st.session_state.get("_new_key"):
        st.success("Copy this key now and store it in the client system's secret store. It will not be shown again.")
        st.code(st.session_state["_new_key"], language=None)
        if st.button("I have stored it"):
            st.session_state.pop("_new_key")
            st.rerun()
    active = [k["id"] for k in keys if not k.get("revoked")]
    if active:
        c1, c2 = st.columns([2, 1], vertical_alignment="bottom")
        kid = c1.selectbox("Revoke a key", active, format_func=lambda i: f"{i} · {next(k['name'] for k in keys if k['id'] == i)}")
        if c2.button("Revoke", use_container_width=True):
            A.revoke_api_key(me(), kid)
            st.toast(f"Key {kid} revoked")
            st.rerun()


def page_outcomes():
    from agent import outcomes as O
    from agent import live
    from agent.ingest import mapping as M
    mode = "Shadow mode" if SHADOW else "Assist mode"
    page_head("System", "Outcomes",
              f"{mode}. What really happened to each alert, compared with what the agent said. This is the evidence "
              f"for moving from shadow to assist mode, and for tuning the policy.")
    mt = O.metrics()
    f1 = lambda x: "—" if x is None else f"{x:.0%}"  # noqa: E731
    cells = [("Cases investigated", f"{mt['cases']:,}", f"{mt['awaiting_reply']} waiting for a reply"),
             ("With a real outcome", f"{mt['with_outcome']:,}", "confirmed fraud or legitimate"),
             ("Fraud calls that were fraud", f1(mt["precision"]), "precision"),
             ("Fraud caught", f1(mt["recall"]), "of real fraud, called fraud"),
             ("Legitimate calls that were fraud", f1(mt["missed_fraud"]), "missed fraud"),
             ("Handed to people", f1(mt["handed_to_people"]), "agent said uncertain")]
    st.html("<div class='kpis'>" + "".join(f"<div class='kpi'><div class='eyebrow'>{k}</div><div class='n'>{n}</div>"
                                          f"<div class='l'>{e(lab)}</div></div>" for k, n, lab in cells) + "</div>")

    sec("Ready for assist mode?", f"criteria from {POLICY_LABEL}")
    rows = "".join(
        f"<tr><td>{e(c['check'])}</td><td class='num'>{e(c['value'] if isinstance(c['value'], int) else f1(c['value']))}</td>"
        f"<td>{e(c['target'])}</td><td><span class='chip {'c-legit' if c['ok'] else 'c-warn'}'>{'met' if c['ok'] else 'not yet'}</span></td></tr>"
        for c in mt["checks"])
    st.html("<div class='tbl'><table style='min-width:0'><tr><th>Check</th><th style='text-align:right'>Now</th><th>Target</th>"
            "<th></th></tr>" + rows + "</table></div>")
    if SHADOW:
        st.caption(("All criteria are met. To switch, set operations.mode: assist in config/policy.yaml, bump the version, "
                    "record the approver and redeploy." if mt["ready"] else
                    "Keep running in shadow mode and recording outcomes until every criterion is met."))
    if mt["agreement"] is not None:
        st.caption(f"Analysts agreed with {mt['agreed']} and disagreed with {mt['disagreed']} shadow recommendations "
                   f"({f1(mt['agreement'])} agreement).")

    c1, c2 = st.columns(2, gap="large")
    with c1:
        sec("Agent verdict vs real outcome", "cases with an outcome")
        mx = mt["matrix"]
        st.html("<div class='tbl'><table style='min-width:0'><tr><th>Agent said</th><th style='text-align:right'>Really fraud</th>"
                "<th style='text-align:right'>Really legitimate</th></tr>" +
                "".join(f"<tr><td>{v}</td><td class='num'>{mx[v]['fraud']}</td><td class='num'>{mx[v]['legitimate']}</td></tr>"
                        for v in ("fraud", "uncertain", "legitimate")) + "</table></div>")
    with c2:
        sec("Calibration", "does a 90% case turn out fraud 90% of the time?")
        st.html("<div class='tbl'><table style='min-width:0'><tr><th>Fraud probability</th><th style='text-align:right'>Cases</th>"
                "<th style='text-align:right'>Really fraud</th></tr>" +
                "".join(f"<tr><td>{b['range']}</td><td class='num'>{b['cases']}</td><td class='num'>{f1(b['observed_fraud'])}</td></tr>"
                        for b in mt["calibration"]) + "</table></div>")
    if mt["by_pattern"]:
        sec("By pattern", "")
        st.dataframe(pd.DataFrame([{"Pattern": k.replace("_", " "), "Cases": v["cases"], "Really fraud": v["fraud"],
                                    "Agent called fraud": v["agent_fraud"], "Agent right (fraud or legitimate)": v["agent_right"]}
                                   for k, v in sorted(mt["by_pattern"].items(), key=lambda kv: -kv[1]["cases"])]),
                     hide_index=True, use_container_width=True)
    st.caption(f"Exposure on correctly called fraud: {money(mt['exposure_stopped'])} · median investigation time "
               f"{mt['median_seconds'] if mt['median_seconds'] is not None else '—'} s · median wait for a cardholder reply "
               f"{mt['median_reply_hours'] if mt['median_reply_hours'] is not None else '—'} h")

    if allowed("record"):
        sec("Record outcomes", "one at a time on the Live queue page, or many here")
        up = st.file_uploader("Outcome file (CSV, TSV or Excel with columns alert_id and outcome; note optional)",
                              type=["csv", "tsv", "xlsx"], key="outcomes_up")
        if up is not None and st.button("Record outcomes from this file", type="primary"):
            try:
                df = M.read_file(up.getvalue(), up.name)
                r = O.bulk(df.to_dict("records"), me(), f"console · {up.name}")
                st.success(f"Recorded {r['recorded']} outcome(s); {r['rejected']} row(s) rejected.")
                if r["errors"]:
                    st.dataframe(pd.DataFrame(r["errors"]), hide_index=True, use_container_width=True)
            except Exception as ex:  # noqa: BLE001
                st.error(f"Could not read the file: {ex}")
        st.caption("Outcome words understood: fraud, confirmed_fraud, chargeback / legitimate, genuine, cleared, false_positive.")
        pending = [c for c in live.store().recent(500) if c.get("status") == "done" and c["alert_id"] not in O.outcome_map()]
        if pending:
            st.caption(f"{len(pending)} investigated alert(s) still have no outcome, newest first: "
                       + ", ".join(c["alert_id"] for c in pending[:15]) + (" ..." if len(pending) > 15 else ""))


# =========================================================================== navigation
from agent import obs as _obs  # noqa: E402
_obs.started("console")
_user = auth.current_user()
if not _user:
    st.stop()
st.session_state["user"] = _user
pages = {
    "Investigations": [st.Page(page_investigate, title="Investigate", icon=":material/policy:", url_path="investigate", default=True),
                       st.Page(page_agent, title="Ask the agent", icon=":material/smart_toy:", url_path="agent"),
                       st.Page(page_intake, title="Customer dispute", icon=":material/forum:", url_path="dispute"),
                       st.Page(page_live, title="Live queue", icon=":material/stream:", url_path="live"),
                       st.Page(page_portfolio, title="Case Portfolio", icon=":material/folder_open:", url_path="portfolio"),
                       st.Page(page_alerts, title="Alert Queue", icon=":material/notifications_active:", url_path="alerts")],
    "Evaluate": [st.Page(page_lab, title="Scenario lab", icon=":material/science:", url_path="lab")],
    "Intelligence": [st.Page(page_graph, title="Investigation Graph", icon=":material/hub:", url_path="graph"),
                     st.Page(page_patterns, title="Fraud Patterns", icon=":material/pattern:", url_path="patterns"),
                     st.Page(page_memory, title="Case Memory", icon=":material/psychology:", url_path="memory"),
                     st.Page(page_policies, title="Policies", icon=":material/gavel:", url_path="policies")],
    "System": [st.Page(page_outcomes, title="Outcomes", icon=":material/rule:", url_path="outcomes"),
               st.Page(page_activity, title="Agent Activity", icon=":material/timeline:", url_path="activity"),
               st.Page(page_audit, title="Audit Log", icon=":material/fact_check:", url_path="audit")],
}
_hide = {"agent": "agent", "dispute": "intake", "live": "investigate", "lab": "lab"}
for _sec in list(pages):
    pages[_sec] = [p for p in pages[_sec] if not (p.url_path in _hide and not allowed(_hide[p.url_path]))]
    if not pages[_sec]:
        pages.pop(_sec)
if allowed("ingest"):
    pages["System"].append(st.Page(page_data, title="Data intake", icon=":material/upload_file:", url_path="data"))
if allowed("manage_users"):
    pages["System"].append(st.Page(page_team, title="Team & access", icon=":material/admin_panel_settings:",
                                   url_path="team"))
nav = st.navigation(pages)

with st.sidebar:
    st.html(auth.user_card(me()))
    auth.sign_out_button()
    s = status_info()
    if s["ok"]:
        tg = ("Connected" if s["name"] == "tigergraph" else "Local mode") + f" · {s['via']}"
        llm_p = LLM(SETTINGS)
        llm_on = llm_p.provider != "none"
        st.html("<div class='side-status'>"
                f"<div><span class='d' style='background:var(--legit)'></span>TigerGraph<b>{e(tg)}</b></div>"
                "<div><span class='d' style='background:var(--legit)'></span>Agent<b>Online</b></div>"
                f"<div><span class='d' style='background:var(--legit)'></span>Policy engine<b>v{e(POLICY_VERSION)} active</b></div>"
                f"<div><span class='d' style='background:{'var(--yellow)' if SHADOW else 'var(--legit)'}'></span>Mode"
                f"<b>{'Shadow · nothing acted on' if SHADOW else 'Assist'}</b></div>"
                f"<div><span class='d' style='background:{'var(--legit)' if llm_on else 'var(--mute)'}'></span>"
                f"LLM<b>{e(llm_p.provider if llm_on else 'off')}</b></div>"
                f"<div style='margin-top:6px'>Transactions<b>{int(s.get('txns', 0)):,}</b></div>"
                f"<div>Closed cases<b>{int(s.get('closed_cases', 0)):,}</b></div>"
                f"<div>Agent cases<b>{int(s.get('agent_cases', 0)):,}</b></div>"
                "<div class='gtext' style='margin-top:14px;font:600 15px var(--display);display:inline-block'>Less dashboard. More investigation.</div>"
                "<div class='muted' style='font-size:11.5px'>Google ADK · Gemini on Vertex AI · Cloud Run</div></div>")
    else:
        status_info.clear()
        st.html("<div class='side-status'><div><span class='d' style='background:var(--pink)'></span>TigerGraph"
                "<b>unreachable</b></div><div class='muted' style='font-size:11.5px;line-height:1.5;margin-top:4px'>"
                "Savanna workspace may be suspended. Resume it in the Savanna console, then reload. Saved cases "
                "still open.</div></div>")

nav.run()
