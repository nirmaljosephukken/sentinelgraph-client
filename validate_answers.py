"""
Validate the answer files against the README's answer format and the policy:
  * every required field present with the right type and enum values
  * every ID exists in the dataset (transactions, cards, customers, closed cases)
  * exposure == sum of affected amounts; legitimate => empty episode, 0 exposure, no SAR
  * sar.file agrees with FILE_REPORT in the final actions; routes match the policy table
  * final == initial when no evidence was requested
  * every stated link mechanism is real: a narrative, summary, pattern description or action reason may only say
    that cards share a device (or form a ring) if the raw data shows a connected card using one of the episode's
    device profiles, and may only claim a shared email link if the evidence records one

    python validate_answers.py            (exit code 1 if any problem)
"""
from __future__ import annotations

import json
import sys

import pandas as pd

from agent.config import SETTINGS
from agent.policy import route
from agent.linkage import unsupported_link_claims

PATTERNS = {"card_testing", "card_not_present_fraud", "card_not_present_new_device", "out_of_region_use",
            "account_takeover", "undocumented", "none"}
ACTIONS = {"ALLOW_TRANSACTION", "DECLINE_TRANSACTION", "MONITOR_CARD", "MONITOR_CONNECTED_CARDS", "WARN_CUSTOMER",
           "VERIFY_WITH_CUSTOMER", "STEP_UP_AUTH", "BLOCK_CARD", "BLOCK_ALL_CARDS", "GENERATE_REPORT", "CREATE_CASE",
           "FILE_REPORT", "ESCALATE_TO_ANALYST", "CLOSE_NO_FRAUD"}


def link_problems(a: dict) -> list[str]:
    """Stated link mechanisms (shared device, ring, shared email) must be visible in the raw data."""
    c, s, out = a["case"], a["sar"], []
    episode_devices = {dev.get(x) for x in c["affected_txn_ids"]} - {None, ""}
    kinds = set()
    if episode_devices and any(episode_devices & card_devices.get(k, set()) for k in c["connected_card_ids"]):
        kinds.add("shared_device")
    episode_emails = {emails.get(x) for x in c["affected_txn_ids"]} - {None, ("", "")}
    episode_emails = {e for e in episode_emails if e[0] and e[1]}
    if episode_emails and any(episode_emails & card_emails.get(k, set()) for k in c["connected_card_ids"]):
        kinds.add("peer_email")
    texts = {"summary": c["summary"], "pattern_description": c["pattern_description"],
             "sar.narrative": s["narrative"], "sar.reason": s["reason"]}
    texts.update({f"evidence[{i}] {e['ref']}": e["claim"] for i, e in enumerate(c["evidence"])})
    texts.update({f"action {x['action']}": x["reason"] for x in a["next_best_actions"]["final"] +
                  a["next_best_actions"]["initial"]})
    for where, text in texts.items():
        wrong = unsupported_link_claims(text, kinds)
        if wrong:
            out.append(f"{where} claims a link the data does not show: {wrong}")
    return out


def main() -> int:
    global dev, card_devices, emails, card_emails
    t = pd.read_csv(SETTINGS.prepared_dir / "txn.csv.gz", usecols=["txn_id", "amount", "card_id", "customer_id",
                                                                     "device_profile", "p_email", "r_email"], dtype={"txn_id": str})
    amounts = dict(zip(t.txn_id, t.amount))
    cards, custs = set(t.card_id), set(t.customer_id)
    devices = set(t.device_profile.dropna())
    dev = dict(zip(t.txn_id, t.device_profile.fillna("").str.strip()))
    emails = dict(zip(t.txn_id, zip(t.p_email.fillna(""), t.r_email.fillna(""))))
    te = t.dropna(subset=["p_email", "r_email"])
    card_emails = te.assign(pair=list(zip(te.p_email, te.r_email))).groupby("card_id").pair.apply(set).to_dict()
    card_devices = t.dropna(subset=["device_profile"]).groupby("card_id").device_profile.apply(
        lambda x: {v.strip() for v in x if v.strip()}).to_dict()
    ccs = set(pd.read_csv(SETTINGS.prepared_dir / "closed_cases.csv", usecols=["case_id"]).case_id)
    pack = pd.read_csv(SETTINGS.prepared_dir / "case_pack.csv", dtype=str)
    problems = []
    for cid in pack.case_id:
        p = SETTINGS.cases_dir / f"{cid}.json"
        if not p.exists():
            problems.append(f"{cid}: missing answer file")
            continue
        a = json.loads(p.read_text())
        c = a["case"]
        err = lambda m: problems.append(f"{cid}: {m}")  # noqa: E731
        for k in ("case_id", "case", "evidence_requests", "next_best_actions", "sar", "stop_reason", "tool_calls",
                  "tokens", "latency_s"):
            if k not in a:
                err(f"missing {k}")
        if c["status"] not in ("open", "closed_fraud", "closed_legitimate", "escalated"):
            err("bad status")
        if c["verdict"] not in ("fraud", "legitimate", "uncertain"):
            err("bad verdict")
        if c["pattern"] not in PATTERNS:
            err("bad pattern")
        if c["pattern"] == "undocumented" and not c["pattern_description"]:
            err("undocumented without description")
        for x in c["affected_txn_ids"] + ([c["first_suspicious_txn_id"]] if c["first_suspicious_txn_id"] else []):
            if x not in amounts:
                err(f"unknown txn {x}")
        for k in c["connected_card_ids"]:
            if k not in cards:
                err(f"unknown card {k}")
        for d in c["connected_device_profiles"]:
            if d not in devices:
                err(f"unknown device profile {d!r}")
        for k in c["similar_prior_cases"]:
            if k not in ccs:
                err(f"unknown closed case {k}")
        exp = round(sum(abs(amounts[x]) for x in c["affected_txn_ids"] if x in amounts), 2)
        if abs(exp - c["exposure_usd"]) > 0.01:
            err(f"exposure {c['exposure_usd']} != sum {exp}")
        if c["verdict"] == "legitimate" and (c["affected_txn_ids"] or c["exposure_usd"] or a["sar"]["file"]):
            err("legitimate verdict with episode/exposure/SAR")
        for e in c["evidence"]:
            if set(e) != {"claim", "source", "ref", "entity_ids"} or e["source"] not in ("graph", "document", "customer", "external"):
                err("bad evidence item")
        nba = a["next_best_actions"]
        for stage in ("initial", "final"):
            for x in nba[stage]:
                if x["action"] not in ACTIONS:
                    err(f"unknown action {x['action']}")
                if x["route"] != route(x["action"], c["exposure_usd"] if x["action"] == "BLOCK_CARD" else 0):
                    err(f"route mismatch {x['action']} {x['route']}")
        if not a["evidence_requests"] and nba["initial"] != nba["final"]:
            err("final differs from initial without an evidence request")
        final_names = [x["action"] for x in nba["final"]]
        if a["sar"]["file"] != ("FILE_REPORT" in final_names):
            err("sar.file disagrees with FILE_REPORT")
        s = a["sar"]
        if s["file"]:
            if not s["narrative"] or not s["subjects"] or len(s["activity_dates"]) != 2:
                err("incomplete SAR")
            for sub in s["subjects"]:
                if sub not in cards and sub not in custs and sub not in devices:
                    err(f"unknown SAR subject {sub}")
        elif s["narrative"] or s["subjects"] or s["total_amount_usd"] or s["activity_dates"]:
            err("SAR fields must be empty when file is false")
        for m in link_problems(a):
            err(m)
        if not c["written_to_graph"]:
            err("not written to graph")
        for r in a["evidence_requests"]:
            if r["type"] not in ("customer_validation", "step_up_auth", "analyst_info"):
                err("bad evidence request type")
    pro = sorted((SETTINGS.cases_dir.parent / "proactive" / "cases").glob("*.json"))
    for p in pro:  # cases the agent opened on its own: same link check
        a = json.loads(p.read_text())
        problems += [f"{p.stem}: {m}" for m in link_problems(a)]
    for p in problems:
        print("PROBLEM", p)
    print(f"checked {len(pack)} cases + {len(pro)} proactive: {'OK' if not problems else str(len(problems)) + ' problem(s)'}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
