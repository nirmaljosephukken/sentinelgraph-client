"""
The investigation agent: a controlled, auditable state machine.

  TRIGGER -> OPEN CASE -> INVESTIGATE (graph tools) -> RECALL MEMORY (GraphRAG) -> GROUND IN POLICY (RAG)
  -> LLM INVESTIGATOR (bounded extra tool calls) -> ASSESS (evidence fusion, uncertainty)
  -> DECIDE (policy engine, initial NBA) -> GATHER EVIDENCE (controlled request, simulated reply)
  -> REASSESS -> DECIDE (final NBA) -> EXPLAIN (LLM writer, validated) -> WRITE CASE MEMORY (graph)

Every step is appended to the case timeline (trace) and emitted to the UI as it happens.
"""
from __future__ import annotations

import json
import time
from datetime import timedelta
from typing import Any, Callable

from agent import policy, signals as S
from agent.backends import GraphBackend
from agent.embeddings import amount_band, embed
from agent.llm import LLM, foreign_ids
from agent.policy_config import POLICY
from agent.safety import DATA_NOTE as S_DATA_NOTE, check_output
from agent.linkage import kinds_of, link_statement, link_summary, strip_unsupported, unsupported_link_claims
from agent.simulator import Reply, reply_mode, simulate

PATTERN_TEXT = {
    "card_testing": "card testing small online authorisations then larger purchase",
    "card_not_present_fraud": "card not present online purchases inconsistent with cardholder usual merchants amounts",
    "card_not_present_new_device": "online purchases from a device not previously seen on this account",
    "out_of_region_use": "card present use in a billing region the cardholder had no history in",
    "account_takeover": "mixed channel activity inconsistent credentials and card data both used",
    "undocumented": "pattern not matched to a documented typology shared device proxy threshold",
    "none": "model scored transaction cardholder confirmed alert cleared",
}


class Investigation:
    def __init__(self, backend: GraphBackend, llm: LLM, case: dict, emit: Callable[[dict], None] | None = None,
                 use_llm_investigator: bool = True, reply: Reply | None = None, replies: str | None = None):
        """reply: a real answer to the evidence request (resuming a case that was waiting for it).
        replies: 'simulate' (benchmark, scenario lab) or 'wait' (client data); default from agent.simulator."""
        self.g = backend
        self.reply_in = reply
        self.replies = replies or reply_mode()
        self.llm = llm
        self.case = case
        self.emit = emit or (lambda e: None)
        self.events: list[dict] = []
        self.tool_calls = 0
        self.step = 0
        self.use_llm_investigator = use_llm_investigator
        self.t_start = time.time()

    # ------------------------------------------------------------------ plumbing
    def _clock(self, minutes: float = 0) -> str:
        return S.fmt(S.ts(self.case["opened_at"]) + timedelta(minutes=minutes))

    def log(self, kind: str, title: str, detail: Any = None, minutes: float | None = None) -> dict:
        self.step += 1
        e = {"step": self.step, "ts": self._clock(minutes if minutes is not None else self.step * 0.5), "kind": kind,
             "title": title, "detail": detail, "elapsed_s": round(time.time() - self.t_start, 2)}
        self.events.append(e)
        self.emit(e)
        return e

    def tool(self, name: str, fn: Callable, **kw) -> Any:
        t0 = time.time()
        n_before = len(getattr(self.g, "transport_log", []))
        out = fn(**kw)
        self.tool_calls += 1
        transport = (self.g.transport_log[n_before:] or [self.g.name])[-1].split(":")[0]
        summary = _summarise(out)
        self.log("tool", f"{name}({', '.join(f'{k}={_fmt_arg(v)}' for k, v in kw.items())})",
                 {"args": {k: _fmt_arg(v) for k, v in kw.items()}, "result": summary,
                  "latency_s": round(time.time() - t0, 3), "transport": transport})
        return out

    # ------------------------------------------------------------------ main
    def run(self) -> dict:
        c = self.case
        trig = c["trigger_type"]
        self.log("trigger", f"{trig.replace('_', ' ').title()}: {c['trigger_text']}",
                 {k: c.get(k) for k in ("case_id", "flagged_txn_id", "card_id", "customer_id", "risk_score")}, 0)
        yr = str(c.get("opened_at", ""))[:4] or "2016"
        gcase = (f"CASE-{yr}-{int(c['case_id'].split('-')[1]):04d}" if c["case_id"].startswith("ALR-")
                 else f"CASE-{yr}-{c['case_id']}")
        self.log("case", f"Case {gcase} opened for alert {c['case_id']} (status: investigating)", {"graph_case_id": gcase})

        # ---------------- investigate
        ctx = self.tool("txn_context", self.g.txn_context, txn_id=str(c["flagged_txn_id"]))
        x = ctx["txn"]
        ft = S.ts(x["ts"])
        W = lambda d: S.fmt(ft + timedelta(days=d))  # noqa: E731
        card_rows = self.tool("card_window", self.g.card_window, card_id=x["card_id"], t0=W(-120), t1=W(7))
        prof = self.tool("card_profile", self.g.card_profile, card_id=x["card_id"], before=x["ts"])
        client_rows = self.tool("client_history", self.g.client_history, client_id=x["client_id"])
        fan, dcases = {"device": {}, "txns": []}, {"closed_cases": [], "agent_cases": []}
        if x["device_profile"].strip():
            fan = self.tool("device_fanout", self.g.device_fanout, device=x["device_profile"], t0=W(-10), t1=W(10))
            if S.specific_device(x["device_profile"], int(fan["device"].get("n_txns") or 0)):
                dcases = self.tool("device_cases", self.g.device_cases, device=x["device_profile"])
        peers = self.tool("peer_txns", self.g.peer_txns, txn_id=x["id"], t0=W(-2), t1=W(2),
                          lo=round(x["amount"] * 0.985, 2), hi=round(x["amount"] * 1.015, 2), use_email=bool(x["p_email"]))
        prior = self.tool("prior_cases", self.g.prior_cases, customer_id=x["customer_id"])

        f = S.Findings()
        evs: list[S.Evidence] = []
        st = self.step
        evs += S.d_trigger(c, x, st)
        p_model, e_model = S.d_model(x, st)
        evs += e_model
        evs += S.d_recurring(x, card_rows, st, f)
        evs += S.d_region(x, client_rows, prof, card_rows, st, f)
        evs += S.d_card_testing(x, card_rows, st, f)
        evs += S.d_structuring(x, card_rows, st, f)
        if "structuring" in f.flags:
            band = self.tool("amount_band_scan", self.g.amount_band_scan, t0=W(-30), t1=W(30), lo=440.0, hi=500.0)
            evs += S.d_structuring_network(x, band, self.step, f)
        evs += S.d_device(x, prof, st, f)
        evs += S.d_device_ring(x, fan, dcases, st, f)
        evs += S.d_peers(x, peers, st, f)
        evs += S.d_episode(x, card_rows, st, f)
        evs += S.d_amount_profile(x, prof, st)
        evs += S.d_history(x, prior, st)
        evs += S.d_ato(x, card_rows, f, st)
        self.log("evidence", f"Graph evidence collected: {len(evs)} findings",
                 [e.to_dict() for e in evs])

        # ---------------- memory (GraphRAG over closed cases + earlier agent cases)
        hyp = self._hypothesis(x, f)
        extra = ""
        if "structuring" in f.flags:
            extra = "four online purchases within forty minutes each just under $500 authorization threshold "
        elif "device_ring" in f.flags:
            extra = "same device profile anonymous proxy never seen on this account other cardholders reported the same device "
        elif "card_testing" in f.flags:
            extra = "run of very small online authorizations followed by a larger purchase testing a stolen card number "
        qtext = extra + f"{PATTERN_TEXT.get(hyp, '')} {c['trigger_text']} {x['channel']} " + \
                ("new device " if x["device_status"] == "New" else "") + ("proxy " if x["proxy"] else "")
        qvec = embed(qtext, [hyp, "confirmed_fraud" if hyp != "none" else "cleared", amount_band(x["amount"])])
        sim_cc = self.tool("similar_closed_cases", self.g.similar_closed_cases, vec=qvec, k=8)
        sim_fc = self.tool("similar_agent_cases", self.g.similar_agent_cases, vec=qvec, k=6)
        # memory integrity: only cases closed before this alert was opened, never the case itself
        sim_fc = [(m, s) for m, s in sim_fc if m and m.get("id") and m.get("id") != gcase
                  and str(m.get("opened_at") or "") < c["opened_at"]][:3]
        memory_ids = self._pick_memory(x, f, prior, dcases, sim_cc, hyp)
        if memory_ids:
            evs.append(S.ev("Case memory: similar prior investigations retrieved - " + "; ".join(
                f"{m['id']} ({m['outcome']}, {m['pattern']}, ${m['exposure_usd']:,.2f}): {m['analyst_notes'][:110]}"
                for m in memory_ids[:3]), "graph", "similar_closed_cases + prior_cases + device_cases",
                [m["id"] for m in memory_ids], 1.0, "context", self.step))
        if sim_fc:
            top = [(m, s) for m, s in sim_fc if m and m.get("id") and s > 0.35]
            if top:
                evs.append(S.ev("Earlier agent cases with a similar profile: " + "; ".join(
                    f"{m['id']} ({m.get('verdict')}, {m.get('pattern')})" for m, _ in top),
                    "graph", "similar_agent_cases", [m["id"] for m, _ in top], 1.0, "context", self.step))

        # ---------------- policy grounding (RAG over policy, typologies, regulatory digest)
        pq = f"{trig} {hyp} {PATTERN_TEXT.get(hyp, '')} verify before block report shared device"
        pol = self.tool("policy_search", self.g.policy_search, vec=embed(pq, [hyp]), k=4)
        grounding = [{"section": h["section"], "doc": h["doc"], "text": h["text"][:600], "score": round(s, 3)}
                     for h, s in pol]

        # ---------------- bounded LLM investigator
        notes = ""
        if self.use_llm_investigator and self.llm.provider != "none":
            notes = self._llm_investigate(x, evs, f, grounding)
            notes, dropped = strip_unsupported(notes, kinds_of(f.links))
            if dropped:
                self.log("guardrail", f"Dropped {len(dropped)} investigator sentence(s) claiming a link the evidence does not show",
                         {"dropped": dropped})
            if notes:
                evs.append(S.ev(f"Investigator note (LLM): {notes[:600]}", "graph", "llm_investigator", [x["id"]],
                                1.0, "context", self.step))

        # ---------------- assess
        p0 = p_model
        p1, contrib = S.fuse(p0, evs)
        p_graph, _ = S.fuse(p0, [e for e in evs if e.family != "trigger"])
        ff, lf = S.families(evs, "fraud"), S.families(evs, "legit")
        scored = x.get("model_scored", True)   # client feeds may carry no case-memory score
        if scored and p_model <= 0.03:
            lf.add("ml")
        if scored and p_model >= 0.30:
            ff.add("ml")
        episode = dict(f.episode)
        episode.setdefault(x["id"], x)
        exposure = round(sum(abs(r["amount"]) for r in episode.values()), 2)
        pattern = self._pattern(x, f, client_rows, p1)
        conf = self._confidence(p1, ff, lf)
        self.log("assess", f"Fraud probability {p1:.2f} (memory-model prior {p0:.2f}); confidence {conf}; "
                           f"hypothesis {pattern}; exposure ${exposure:,.2f}",
                 {"prior": round(p0, 4), "posterior": round(p1, 4), "graph_only": round(p_graph, 4),
                  "log_lr_by_family": contrib, "fraud_families": sorted(ff), "legit_families": sorted(lf),
                  "uncertainty": self._gaps(x, evs, ff, lf, trig)})

        sit = policy.Situation(
            trigger=trig, p=p1, fraud_families=len(ff), legit_families=len(lf), exposure=exposure, pattern=pattern,
            recurring_dispute=(trig == "customer_report" and "recurring" in f.flags),
            card_testing="card_testing" in f.flags,
            testing_large_over_100=bool(f.flags.get("card_testing", {}).get("large_cleared_over_100")),
            shared_origin=bool(f.connected_cards) and ("device_ring" in f.flags or "peer_cluster" in f.flags
                                                       or "structuring_network" in f.flags),
            coordinated_undocumented=(pattern == "undocumented" and bool(f.connected_cards or f.flags.get("device_ring"))),
            connected_cards=len(f.connected_cards), link_summary=link_summary(f.links),
            flagged_pending=(trig == "risk_score"), online=(x["channel"] == "online"))
        initial = policy.decide(sit)
        self.log("decision", f"Initial next best action: {', '.join(a['action'] for a in initial.actions)}",
                 {"actions": initial.actions, "request": initial.request, "why_request": initial.request_reason,
                  "policy_check": policy.check(initial.actions, sit), "policy": POLICY.summary()})
        auto, queued = policy.executable(initial.actions)
        if POLICY.shadow:
            self.log("execute", f"Shadow mode: {len(auto)} auto action(s) and {len(queued)} approval action(s) recommended; "
                                f"nothing is acted on", {"recommended": [a["action"] for a in initial.actions],
                                                         "mode": "shadow"})
        else:
            self.log("execute", f"Executed {len(auto)} auto action(s); {len(queued)} queued for approval",
                     {"executed": [a["action"] for a in auto], "awaiting": [f"{a['action']} ({a['route']})" for a in queued]})

        # ---------------- gather more evidence if needed
        requests_out, final, p_final, reply = [], initial, p1, None
        self.awaiting = None
        if initial.request and self.reply_in is None and self.replies == "wait":
            hours = POLICY.reply_wait_hours
            self.log("request", f"Evidence requested: {initial.request} (policy 5). Waiting for the cardholder's real "
                                f"reply; no reply within {hours:g} hours counts as no reply (R4)",
                     {"why": initial.request_reason, "wait_hours": hours}, minutes=30)
            requests_out.append({"type": initial.request, "asked_after_step": self.step, "status": "pending",
                                 "why": initial.request_reason, "wait_hours": hours})
            self.awaiting = {"type": initial.request, "why": initial.request_reason, "wait_hours": hours}
        elif initial.request:
            real = self.reply_in is not None
            reply = self.reply_in or simulate(initial.request, trig, p1, p_graph, len(lf), sit.recurring_dispute,
                                              {"amount": x["amount"], "txn_id": x["id"],
                                               "cadence": (f.flags.get("recurring") or {}).get("cadence", "recurring")})
            self.log("request", f"Evidence requested: {initial.request} (policy 5, auto)",
                     {"why": initial.request_reason}, minutes=30)
            if real:
                requests_out.append({"type": initial.request, "asked_after_step": self.step, "status": "answered",
                                     "response": reply.kind, "response_text": reply.text, "channel": reply.channel,
                                     "recorded_by": reply.recorded_by, "received_at": reply.received_at})
            else:
                requests_out.append({"type": initial.request, "asked_after_step": self.step, "assumed_response": reply.text})
            hours = POLICY.reply_wait_hours
            said = {"denied": "Cardholder denied the transaction when contacted",
                    "confirmed": "Cardholder confirmed the transaction as their own",
                    "no_reply": f"No reply from the cardholder within {hours:g} hours" if real else
                                "No reply from the cardholder within 24 hours"}[reply.kind]
            if not real:
                how = " (simulated reply; assumption recorded in evidence_requests)"
            else:
                bits = [f"via {reply.channel}"] if reply.channel and reply.channel != "timeout" else []
                bits += [f"recorded by {reply.recorded_by}"] if reply.recorded_by else []
                how = f" ({', '.join(bits)})" if bits else ""
            evs.append(S.ev(said + how, "customer", "evidence_request:1", [x["id"]], reply.lr, "customer", self.step + 1))
            self.log("response", f"Reply ({reply.kind}): {reply.text or said}", {"lr": reply.lr, "real": real},
                     minutes=24 * 60 if reply.kind == "no_reply" else 120)
            p_final, contrib = S.fuse(p0, evs)
            sit.p = p_final
            sit.response = reply.kind
            if reply.kind == "denied":
                sit.fraud_families += 1
            final = policy.decide(sit)
            self.log("assess", f"Reassessed fraud probability {p_final:.2f} (was {p1:.2f})",
                     {"posterior": round(p_final, 4), "log_lr_by_family": contrib})
            self.log("decision", f"Final next best action: {', '.join(a['action'] for a in final.actions)}",
                     {"actions": final.actions, "policy_check": policy.check(final.actions, sit)})
        verdict, status = final.verdict, final.status
        if verdict == "legitimate":
            pattern_out, affected, exposure_out, conn_cards, conn_devs = "none", [], 0.0, [], []
        else:
            pattern_out = pattern
            affected = [r["id"] for r in sorted(episode.values(), key=lambda r: (r["ts"], r["id"]))]
            exposure_out = exposure
            conn_cards = sorted(f.connected_cards)
            conn_devs = sorted(f.connected_devices)
        first = affected[0] if affected else ""
        if verdict != "legitimate" and sit.response is None and not initial.request and not initial.stop:
            status = "open"
        if self.awaiting:
            status = "awaiting_evidence"

        # ---------------- explain
        memory_ids = self._pick_memory(x, f, prior, dcases, sim_cc, pattern_out)  # memory consistent with the verdict
        similar_ids = [m["id"] for m in memory_ids]
        facts = {
            "case_id": c["case_id"], "graph_case_id": gcase, "trigger": trig, "trigger_text": c["trigger_text"],
            "flagged": {k: x[k] for k in ("id", "ts", "amount", "product", "channel", "addr1", "p_email", "r_email",
                                          "device_status", "proxy", "device_profile", "card_id", "customer_id")},
            "verdict": verdict, "status": status, "fraud_probability_initial": round(p1, 2),
            "fraud_probability_final": round(p_final, 2), "pattern": pattern_out, "exposure_usd": exposure_out,
            "affected_txns": [{k: episode[t][k] for k in ("id", "ts", "amount", "channel", "device_profile", "card_id")}
                              for t in affected],
            "connected_cards": {k: f.connected_cards[k] for k in conn_cards},
            "how_cards_are_linked": link_statement({k: f.links.get(k, set()) for k in conn_cards}),
            "link_kinds": sorted(kinds_of({k: f.links.get(k, set()) for k in conn_cards})),
            "connected_devices": {k: f.connected_devices[k] for k in conn_devs},
            "evidence": [e.claim for e in evs], "similar_prior_cases": similar_ids,
            "initial_actions": initial.actions, "final_actions": final.actions,
            "evidence_request": requests_out, "flags": _jsonable(f.flags), "sar_required": final.sar,
            "sar_reason": final.sar_reason, "policy_grounding": [g["section"] for g in grounding],
        }
        text = self._write(facts)
        # what_changed / stop_reason are factual statements about the decision path: generated deterministically
        what_changed = self._changed_template(initial, final, reply, p1, p_final) if initial.request else "nothing"
        if self.awaiting:
            what_changed = "nothing yet: waiting for the cardholder's reply"
        stop_reason = None

        sar = {"file": bool(final.sar), "reason": final.sar_reason, "narrative": "", "subjects": [],
               "total_amount_usd": 0, "activity_dates": []}
        if final.sar:
            dates = sorted(episode[t]["ts"][:10] for t in affected)
            subjects = [x["customer_id"], x["card_id"]] + conn_cards + conn_devs
            narrative = text.get("sar_narrative") or self._sar_template(facts, dates)
            if facts["how_cards_are_linked"] and facts["how_cards_are_linked"] not in narrative:
                narrative = narrative.rstrip() + " " + facts["how_cards_are_linked"]
            sar.update({"narrative": narrative,
                        "subjects": list(dict.fromkeys(subjects)), "total_amount_usd": exposure_out,
                        "activity_dates": [dates[0], dates[-1]] if dates else []})

        answer = {
            "case_id": c["case_id"],
            "case": {
                "status": status, "verdict": verdict, "fraud_probability": round(p_final, 2),
                "pattern": pattern_out,
                "pattern_description": (text.get("pattern_description") or self._pattern_template(f, x))
                if pattern_out == "undocumented" else "",
                "affected_txn_ids": affected, "first_suspicious_txn_id": first,
                "connected_card_ids": conn_cards, "connected_device_profiles": conn_devs,
                "exposure_usd": exposure_out,
                "evidence": [e.to_answer() for e in _select_evidence(evs)],
                "similar_prior_cases": similar_ids,
                "summary": text.get("summary") or self._summary_template(facts),
                "written_to_graph": False, "graph_case_id": gcase,
            },
            "evidence_requests": requests_out,
            "next_best_actions": {"initial": initial.actions, "final": final.actions, "what_changed": what_changed},
            "sar": sar,
            "policy": POLICY.summary(),
            "stop_reason": stop_reason or (
                f"Paused at the evidence request: the cardholder's answer is the highest-value evidence, so the case waits "
                f"for it (policy 5). No reply within {POLICY.reply_wait_hours:g} hours counts as no reply (R4)."
                if self.awaiting else self._stop_template(p_final, reply, initial)),
            "tool_calls": 0, "tokens": 0, "latency_s": 0.0,
        }
        # ---------------- write case memory into the graph
        record = {
            "id": gcase, "source_case_id": c["case_id"], "trigger_type": trig, "opened_at": c["opened_at"],
            "updated_at": self._clock(24 * 60 + 5 if reply and reply.kind == "no_reply" else 180), "status": status,
            "verdict": verdict, "fraud_probability": round(p_final, 4), "pattern": pattern_out,
            "exposure_usd": exposure_out, "summary": answer["case"]["summary"], "sar_filed": bool(final.sar),
            "final_actions": [a["action"] for a in final.actions], "flagged_txn_id": x["id"],
            "affected_txn_ids": affected, "card_id": x["card_id"], "connected_card_ids": conn_cards,
            "card_ids": [x["card_id"]] + conn_cards, "device_profiles": conn_devs or ([x["device_profile"]] if x["device_profile"].strip() else []),
            "similar_closed": [(m["id"], m.get("_score", 0.0)) for m in memory_ids],
            "similar_agent": [(m["id"], s) for m, s in sim_fc if m and m.get("id")],
            "emb": embed(answer["case"]["summary"] + " " + PATTERN_TEXT.get(pattern_out, ""),
                         [pattern_out, "confirmed_fraud" if verdict == "fraud" else "cleared", amount_band(x["amount"])]),
        }
        self.log("memory", f"Case {gcase} written to case memory ({status}, {verdict}, {pattern_out})",
                 {"edges": {"txns": len(affected) or 1, "cards": 1 + len(conn_cards), "devices": len(conn_devs),
                            "similar_closed_cases": similar_ids}})
        record["events"] = self.events
        record["answer"] = answer
        written = False
        try:
            written = bool(self.g.write_case(record))
            self.tool_calls += 1
        except Exception as e:  # keep the answer even if the write fails; the trace records why
            self.log("error", f"Graph write failed: {e}")
        answer["case"]["written_to_graph"] = written
        answer["tool_calls"] = self.tool_calls
        answer["tokens"] = self.llm.tokens
        answer["latency_s"] = round(time.time() - self.t_start, 1)
        self.answer, self.record = answer, record
        return answer

    # ------------------------------------------------------------------ helpers
    def _hypothesis(self, x, f) -> str:
        v = f.pattern_votes
        for p in ("undocumented", "card_testing", "account_takeover", "out_of_region_use"):
            if p in v:
                return p
        if "recurring" in f.flags or "home_region" in f.flags or \
                (x.get("model_scored", True) and (x["model_score"] or 0) < 0.03 and not f.connected_cards and self.case["trigger_type"] != "customer_report"):
            return "none"
        if x["channel"] == "online":
            return "card_not_present_new_device" if x["device_status"] == "New" else "card_not_present_fraud"
        return "out_of_region_use"

    def _pattern(self, x, f, client_rows, p) -> str:
        v = f.pattern_votes
        for pat in ("undocumented", "card_testing", "account_takeover", "out_of_region_use"):
            if pat in v:
                return pat
        if x["channel"] == "online":
            if x["device_status"] == "New" or "device_ring" in f.flags:
                return "card_not_present_new_device"
            return "card_not_present_fraud"
        prior_here = [r for r in client_rows if r["addr1"] == x["addr1"] and
                      (S.ts(x["ts"]) - S.ts(r["ts"])).days >= 7]
        return "out_of_region_use" if len(prior_here) < 3 else "account_takeover"

    def _pick_memory(self, x, f, prior, dcases, sim_cc, hyp) -> list[dict]:
        picked: dict[str, dict] = {}

        def add(c, score):
            if c["id"] not in picked:
                c = dict(c)
                c["_score"] = round(score, 3)
                picked[c["id"]] = c
        for c in dcases.get("closed_cases", [])[:4]:
            if c.get("outcome") == "confirmed_fraud":
                add(c, 0.9)
        same_card = [c for c in prior.get("closed_cases", []) if c["card_id"] == x["card_id"]]
        want = "cleared" if hyp == "none" else "confirmed_fraud"
        for c in sorted(same_card, key=lambda c: c["opened_at"], reverse=True):
            if c["outcome"] == want and (hyp == "none" or c["pattern"] == hyp):
                add(c, 0.8)
                if len(picked) >= 3:
                    break
        for c in prior.get("connected_closed_cases", [])[:2]:
            add(c, 0.85)
        for c, s in sim_cc:
            if len(picked) >= 5:
                break
            if (hyp == "none" and c["outcome"] == "cleared") or (hyp != "none" and c["pattern"] == hyp):
                add(c, s)
        return list(picked.values())[:5]

    def _confidence(self, p, ff, lf) -> str:
        t = POLICY.t
        if (p >= t.fraud_stop and len(ff) >= t.min_independent_evidence) or \
                (p <= t.legit_stop and len(lf) >= t.min_independent_evidence):
            return "high"
        if ff and lf:
            return "low (conflicting evidence)"
        return "medium" if (p >= 0.7 or p <= 0.3) else "low"

    def _gaps(self, x, evs, ff, lf, trig) -> list[str]:
        gaps = []
        if trig != "customer_report":
            gaps.append("Cardholder has not confirmed or denied the transaction")
        if x["channel"] == "online" and x["device_status"] == "New":
            gaps.append("Device ownership not established (new device for this account)")
        if ff and lf:
            gaps.append("Evidence conflicts: " + ", ".join(sorted(ff)) + " point to fraud; " + ", ".join(sorted(lf)) +
                        " point to legitimate use")
        if x["channel"] == "online" and not x["device_profile"].strip():
            gaps.append("No device record for an online transaction")
        return gaps

    def _llm_investigate(self, x, evs, f, grounding) -> str:
        tools = [
            {"name": "card_window", "description": "All transactions on a card between two timestamps",
             "parameters": {"type": "object", "properties": {"card_id": {"type": "string"}, "t0": {"type": "string"},
                                                             "t1": {"type": "string"}}, "required": ["card_id", "t0", "t1"]}},
            {"name": "device_fanout", "description": "Transactions on any card that used a device profile in a window",
             "parameters": {"type": "object", "properties": {"device": {"type": "string"}, "t0": {"type": "string"},
                                                             "t1": {"type": "string"}}, "required": ["device", "t0", "t1"]}},
            {"name": "prior_cases", "description": "Closed and agent cases for a customer id (e.g. C01234)",
             "parameters": {"type": "object", "properties": {"customer_id": {"type": "string"}}, "required": ["customer_id"]}},
            {"name": "policy_search", "description": "Search the fraud policy, typologies and regulatory guidance",
             "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
        ]

        def call(name, args):
            try:
                if name == "card_window":
                    rows = self.tool("llm:card_window", self.g.card_window, card_id=args["card_id"],
                                     t0=args["t0"][:19], t1=args["t1"][:19])
                    return [{k: r[k] for k in ("id", "ts", "amount", "product", "channel", "addr1", "device_profile",
                                               "model_score")} for r in rows[:40]]
                if name == "device_fanout":
                    out = self.tool("llm:device_fanout", self.g.device_fanout, device=args["device"],
                                    t0=args["t0"][:19], t1=args["t1"][:19])
                    return [{k: r[k] for k in ("id", "ts", "amount", "card_id", "device_status", "proxy", "model_score")}
                            for r in out["txns"][:40]]
                if name == "prior_cases":
                    out = self.tool("llm:prior_cases", self.g.prior_cases, customer_id=args["customer_id"])
                    return [{k: cc[k] for k in ("id", "card_id", "outcome", "pattern", "exposure_usd", "opened_at")}
                            for cc in out["closed_cases"][:20]]
                if name == "policy_search":
                    out = self.tool("llm:policy_search", self.g.policy_search, vec=embed(args["query"]), k=3)
                    return [{"section": h["section"], "text": h["text"][:500]} for h, _ in out]
            except Exception as e:
                return {"error": str(e)[:200]}
            return {"error": "unknown tool"}

        digest = {"flagged": {k: x[k] for k in ("id", "ts", "amount", "product", "channel", "addr1", "device_status",
                                                "proxy", "device_profile", "card_id", "customer_id")},
                  "evidence": [e.claim for e in evs], "connected_cards": list(f.connected_cards)[:15],
                  "policy": [g["section"] for g in grounding]}
        system = ("You are a senior card-fraud investigator working inside a controlled agent. Deterministic graph "
                  "queries have already run. Decide whether ONE or TWO extra tool calls would change the assessment "
                  "(for example: checking a connected card's history, or the device's use on other cards). Use tools only "
                  "if they add information. Then reply with at most 4 short sentences of analyst notes: what the extra "
                  "lookups showed and any remaining uncertainty. Never invent IDs. Do not recommend actions. " + S_DATA_NOTE)
        notes, calls = self.llm.investigate(system, json.dumps(digest, default=str), tools, call,
                                            self.llm.s.llm_max_tool_calls)
        unsafe = check_output(notes) if notes else []
        if unsafe:
            self.log("guardrail", f"LLM investigator notes dropped: {'; '.join(unsafe)[:200]}", None)
            notes = ""
        self.log("llm", f"LLM investigator ({self.llm.model}): {len(calls)} extra tool call(s)",
                 {"calls": [{"tool": cl["tool"], "args": cl["args"]} for cl in calls], "notes": notes})
        return notes

    def _write(self, facts: dict) -> dict:
        allowed = {facts["case_id"], facts["graph_case_id"], facts["flagged"]["id"], facts["flagged"]["card_id"],
                   facts["flagged"]["customer_id"]} | {t["id"] for t in facts["affected_txns"]} | \
                  set(facts["connected_cards"]) | set(facts["similar_prior_cases"])
        import re
        from agent.llm import ID_PATTERNS
        for e in facts["evidence"]:
            for pat in ID_PATTERNS:
                allowed |= set(re.findall(pat, e))
        system = ("You write fraud case records for a bank. Use ONLY facts in the JSON you are given; never invent IDs, "
                  "dates, amounts or entities, and quote probabilities and amounts exactly. Plain, precise English; no "
                  "markdown. Cite policy rule numbers where given. Actions routed L1 or L2 are RECOMMENDED and await "
                  "human approval (say 'recommended' / 'pending approval'); only 'auto' actions were executed. An "
                  "evidence_request with assumed_response is a simulated assumption - say 'assumed' when you mention it; one "
                  "with status 'answered' is the cardholder's real reply; one with status 'pending' is still awaited. "
                  "Billing regions are "
                  "anonymised codes; call them 'billing region <code>'. Never explain HOW other cards are linked in your "
                  "own words: the field how_cards_are_linked is the only allowed explanation (it is appended to the "
                  "report automatically). Do not call anything a ring, a device-sharing scheme or a shared device unless "
                  "link_kinds contains shared_device or peer_device; the flagged card's own device is not a shared device. "
                  + S_DATA_NOTE)
        prompt = ("Write these fields as a JSON object:\n"
                  "summary: 2-6 sentences an analyst can read: what triggered the case, what the graph showed, the verdict "
                  "and why, and what happens next.\n"
                  "sar_narrative: " + ("6-12 sentences for a regulator that stand alone: WHO (customer, cards, devices), "
                                       "WHAT happened, WHEN (dates), WHERE (channel, region), HOW it was carried out, WHY "
                                       "it is suspicious, the total amount, and the actions taken."
                                       if facts["sar_required"] else "empty string") + "\n"
                  "pattern_description: " + ("2-3 sentences describing the undocumented pattern: what it is, who it "
                                             "affects, how it was found." if facts["pattern"] == "undocumented"
                                             else "empty string") + "\n"
                  "\nFACTS:\n" +
                  json.dumps(facts, default=str))
        out = self.llm.complete_json(system, prompt) or {}
        clean = {}
        for k, v in out.items():
            if not isinstance(v, str):
                continue
            bad = foreign_ids(v, allowed)
            if bad:
                self.log("guardrail", f"LLM text for '{k}' rejected: unknown IDs {bad[:5]}; template used", None)
                continue
            unsafe = check_output(v, facts["verdict"] if k == "summary" else None)
            if unsafe:
                from agent import obs
                obs.event("llm_output_rejected", "WARNING", field=k, reason="; ".join(unsafe)[:200], case_id=facts["case_id"])
                self.log("guardrail", f"LLM text for '{k}' rejected: {'; '.join(unsafe)[:200]}; template used", None)
                continue
            wrong = unsupported_link_claims(v, set(facts["link_kinds"]))
            if wrong:
                self.log("guardrail", f"LLM text for '{k}' rejected: claims a link the evidence does not show "
                                      f"{wrong[:4]}; template used", None)
                continue
            clean[k] = v.strip()
        if out:
            self.log("llm", f"Case narrative written by {self.llm.model} ({len(clean)}/{len(out)} fields accepted)",
                     {**{k: v[:300] for k, v in clean.items()},
                      **({"personal_data_masked": dict(self.llm.pii_masked)} if self.llm.pii_masked else {})})
        return clean

    # ------------------------------------------------------------------ templates (no-LLM fallback)
    def _summary_template(self, F: dict) -> str:
        fl = F["flagged"]
        s = (f"{F['trigger'].replace('_', ' ').capitalize()} alert on transaction {fl['id']} (${fl['amount']:.2f}, "
             f"{fl['channel'].replace('_', ' ')}) on card {fl['card_id']}. ")
        if F["verdict"] == "legitimate":
            s += ("Graph evidence and the cardholder's (assumed) confirmation support legitimate use; " if F["evidence_request"]
                  else "Graph evidence supports legitimate use on independent lines of evidence; ")
            s += "the alert is closed with no fraud. "
        elif F["verdict"] == "fraud":
            s += (f"Assessed as {F['pattern'].replace('_', ' ')} with fraud probability {F['fraud_probability_final']:.2f}; "
                  f"{len(F['affected_txns'])} transaction(s) totalling ${F['exposure_usd']:,.2f} are in the episode. ")
            if F["connected_cards"]:
                s += f"{F['how_cards_are_linked']} They are under monitoring. "
        else:
            s += f"Evidence remains inconclusive (probability {F['fraud_probability_final']:.2f}); the case stays under review. "
        s += "Final actions: " + ", ".join(a["action"] for a in F["final_actions"]) + "."
        return s

    def _sar_template(self, F: dict, dates: list[str]) -> str:
        fl = F["flagged"]
        cards = list(F["connected_cards"])
        s = [f"Between {dates[0]} and {dates[-1]}, card {fl['card_id']} held by customer {fl['customer_id']} was used for "
             f"{len(F['affected_txns'])} transaction(s) totalling ${F['exposure_usd']:,.2f} that the bank believes were not "
             f"authorised by the cardholder.",
             f"The activity was {fl['channel'].replace('_', ' ')}" + (f" from device profile '{fl['device_profile']}'"
                                                                      if fl['device_profile'].strip() else "") + ".",
             f"The case was opened on a {F['trigger'].replace('_', ' ')} ({F['trigger_text']})."]
        s += [e for e in F["evidence"] if any(k in e for k in ("Threshold", "Shared device", "What happened", "Card-testing",
                                                               "Burst", "Card-present use"))][:3]
        if cards:
            s.append(f"Connected cards: {', '.join(cards[:12])}" + ("..." if len(cards) > 12 else "") + ".")
        s.append(f"The pattern is assessed as {F['pattern'].replace('_', ' ')}.")
        s.append("It is suspicious because the activity departs from the cardholder's established behaviour and matches a "
                 "known or coordinated fraud typology, as documented in the case evidence.")
        s.append("Actions: " + ", ".join(a["action"] for a in F["final_actions"]) + ".")
        return " ".join(s)

    def _pattern_template(self, f, x) -> str:
        if "structuring" in f.flags:
            st = f.flags["structuring"]
            return (f"Threshold structuring: a burst of online purchases within about {st['minutes']} minutes, each just under "
                    f"$500 (total ${st['total']:,.2f}), apparently sized to stay under a $500 authorisation threshold. It "
                    f"recurs on {len(f.flags.get('structuring_network', []))} other cards with the same timing and amount "
                    f"band (no shared device links them), and was found by scanning the graph for just-under-$500 "
                    f"clusters.")
        if "device_ring" in f.flags:
            r = f.flags["device_ring"]
            return (f"Device-sharing ring: one device profile ('{r['device']}') is used across {len(r['cards']) + 1} unrelated "
                    f"cards, marked New on each account and behind an anonymising proxy. Found by fanning out from the "
                    f"device in the graph and matching it to earlier undocumented closed cases.")
        return "Activity fits none of the documented typologies; see evidence."

    def _changed_template(self, initial, final, reply, p0, p1) -> str:
        if reply is None:
            return "nothing"
        a0 = {a["action"] for a in initial.actions}
        a1 = {a["action"] for a in final.actions}
        added, dropped = sorted(a1 - a0, key=policy.ORDER.index), sorted(a0 - a1, key=policy.ORDER.index)
        q = "" if reply.real else "(assumed) "
        what = {"denied": f"The cardholder's {q}denial", "confirmed": f"The cardholder's {q}confirmation",
                "no_reply": f"No reply within {POLICY.reply_wait_hours:g} hours" if reply.real else "No reply within 24 hours"}[reply.kind]
        rule = {"denied": "R2", "confirmed": "R3", "no_reply": "R4"}[reply.kind]
        if final.verdict == "legitimate" and any(a["action"] == "WARN_CUSTOMER" for a in final.actions):
            rule = "R7/R3"
        return (f"{what} moved the fraud probability from {p0:.2f} to {p1:.2f}, so {rule} now applies"
                + (f": added {', '.join(added)}" if added else "")
                + (f"; dropped {', '.join(dropped)}" if dropped else "") + ".")

    def _stop_template(self, p, reply, initial) -> str:
        if reply is not None:
            what = {"denied": "cardholder's denial", "confirmed": "cardholder's confirmation",
                    "no_reply": "absence of a reply within 24 hours"}[reply.kind]
            return (f"Stopped after the evidence request: the {what} settles the next step under policy section 6 "
                    f"(probability {p:.2f}); further queries would not change the actions.")
        if initial.stop:
            return (f"Stopped under policy section 6: probability {p:.2f} with at least two independent lines of evidence; "
                    f"further steps would not change the decision.")
        return "Further evidence would not change the recommended actions."


# ---------------------------------------------------------------------- utils
def _fmt_arg(v):
    if isinstance(v, list):
        return f"<vector {len(v)}>"
    return v


def _summarise(out):
    if isinstance(out, list):
        return {"rows": len(out)}
    if isinstance(out, dict):
        return {k: (len(v) if isinstance(v, (list, dict)) else v) for k, v in out.items() if k != "amounts"}
    return str(out)[:200]


def _jsonable(x):
    return json.loads(json.dumps(x, default=str))


def _select_evidence(evs: list[S.Evidence]) -> list[S.Evidence]:
    keep = []
    for e in evs:
        if e.family == "trigger" or e.family == "ml" or e.direction != "neutral" or e.family == "context" \
                or e.source == "customer":
            keep.append(e)
    return keep
