"""
Proactive monitoring of the exam period (Nov-Dec 2016): the agent looks for trouble on its own.

  1. device_ring_scan   - specific device profiles used by many cards, scored by proxy / New-device share and
                          memory-model scores on the cards involved
  2. ring_components    - WCC over the client<->device co-usage graph (GSQL graph algorithm)
  3. amount_band_scan   - threshold structuring (>=3 varied just-under-$500 online purchases within an hour)

Each alert is then investigated end to end by the same agent (trigger = analyst_request) and written to the
graph. Results go to proactive/alerts.json and proactive/cases/*.json. These are *beyond* the 20 benchmark
cases and are reported separately, as the task README asks.

    python monitor.py --investigate 8
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict

from agent.backends import get_backend
from agent.config import ROOT, SETTINGS
from agent.llm import LLM
from agent.orchestrator import Investigation
from agent import signals as S

OUT = ROOT / "proactive"
T0, T1 = "2016-11-01 00:00:00", "2016-12-31 23:59:59"


def ring_alerts(g) -> list[dict]:
    rings = g.device_ring_scan(T0, T1, 3, 300)
    out = []
    for r in rings:
        n = max(1, r["n"])
        proxy, new, model = r["n_proxy"] / n, r["n_new"] / n, r["model_sum"] / n
        score = len(r["cards"]) * (proxy + 0.5 * new + 2 * model)
        if (proxy >= 0.5 and new >= 0.6) or (model >= 0.5 and len(r["cards"]) >= 3):
            out.append({"type": "device_ring", "device": r["id"], "cards": r["cards"], "txns": r["n"],
                        "proxy_share": round(proxy, 2), "new_share": round(new, 2), "mean_model_score": round(model, 3),
                        "score": round(score, 2)})
    return sorted(out, key=lambda a: -a["score"])


def structuring_alerts(g) -> list[dict]:
    rows = g.amount_band_scan(T0, T1, 440.0, 500.0)
    by_card = defaultdict(list)
    for r in rows:
        by_card[r["card_id"]].append(r)
    out = []
    for card, rs in by_card.items():
        rs.sort(key=lambda r: r["ts"])
        for i in range(len(rs)):
            cl = [r for r in rs if 0 <= (S.ts(r["ts"]) - S.ts(rs[i]["ts"])).total_seconds() <= 3600]
            amts = [r["amount"] for r in cl]
            if len(cl) >= 3 and sum(amts) > 1300 and max(amts) - min(amts) >= 5:
                out.append({"type": "structuring", "card": card, "txns": [r["id"] for r in cl],
                            "total": round(sum(amts), 2), "start": cl[0]["ts"]})
                break
    return sorted(out, key=lambda a: a["start"])


def wcc_alerts(g) -> list[dict]:
    if not hasattr(g, "q"):
        return []
    r = g.q("ring_components", {"t0": T0, "t1": T1, "max_profile_txns": 300, "min_cards": 4})
    comps = r[0].get("comp_cards", {}) if r else {}
    devs = r[0].get("comp_devices", {}) if r else {}
    out = []
    for k, cards in comps.items():
        if 4 <= len(set(cards)) <= 200 and len(devs.get(k, [])) <= 3:
            out.append({"type": "wcc_component", "component": k, "cards": sorted(set(cards)), "devices": devs.get(k, [])})
    return sorted(out, key=lambda a: -len(a["cards"]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--investigate", type=int, default=6, help="investigate the top-N alerts end to end")
    ap.add_argument("--only", nargs="*", default=[], help="re-investigate only these case ids (e.g. PRO-005)")
    a = ap.parse_args()
    g = get_backend(SETTINGS)
    (OUT / "cases").mkdir(parents=True, exist_ok=True)
    alerts = {"window": [T0, T1], "device_rings": ring_alerts(g), "structuring": structuring_alerts(g),
              "wcc_components": wcc_alerts(g)}
    (OUT / "alerts.json").write_text(json.dumps(alerts, indent=1))
    print(f"device rings: {len(alerts['device_rings'])}, structuring cards: {len(alerts['structuring'])}, "
          f"WCC components: {len(alerts['wcc_components'])}")
    queue = []
    for r in alerts["device_rings"][: max(1, a.investigate - 2)]:
        fan = g.device_fanout(r["device"], T0, T1)["txns"]
        if fan:
            queue.append((fan[0], f"Monitoring: device profile '{r['device']}' used on {len(r['cards'])} cards in Nov-Dec "
                                  f"({int(r['proxy_share'] * 100)}% via proxy, {int(r['new_share'] * 100)}% New)."))
    for s in alerts["structuring"][:2]:
        queue.append(({"id": s["txns"][-1]}, f"Monitoring: {len(s['txns'])} just-under-$500 online purchases on card "
                                              f"{s['card']} totalling ${s['total']:,.2f} within an hour."))
    done = set()
    for i, (row, text) in enumerate(queue[: a.investigate], 1):
        ctx = g.txn_context(row["id"])["txn"]
        if ctx["card_id"] in done:
            continue
        done.add(ctx["card_id"])
        if a.only and f"PRO-{i:03d}" not in a.only:
            continue
        case = {"case_id": f"PRO-{i:03d}", "opened_at": S.fmt(S.ts(ctx["ts"])), "trigger_type": "analyst_request",
                "trigger_text": text, "flagged_txn_id": ctx["id"], "card_id": ctx["card_id"],
                "customer_id": ctx["customer_id"], "risk_score": ""}
        inv = Investigation(g, LLM(SETTINGS), case)
        ans = inv.run()
        (OUT / "cases" / f"{case['case_id']}.json").write_text(json.dumps(ans, indent=2))
        c = ans["case"]
        print(f"{case['case_id']} {ctx['card_id']} {c['verdict']} p={c['fraud_probability']} {c['pattern']} "
              f"conn={len(c['connected_card_ids'])} sar={ans['sar']['file']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
