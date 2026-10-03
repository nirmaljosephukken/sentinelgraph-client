"""
Evidence layer: turns raw graph results into typed, citable evidence with likelihood ratios.

Design
------
* Every detector returns `Evidence` objects: a human-readable claim, its source and query reference, the
  entity IDs it rests on, a likelihood ratio (LR > 1 supports fraud, LR < 1 supports legitimate), and a
  `family`. Families are independent lines of evidence (ml, trigger, sequence, network, device, behaviour,
  history, customer). Within a family LRs are combined conservatively (strongest one, not the product), so
  correlated signals are not double counted. Across families LRs multiply (naive Bayes in log-odds).
* The starting point is the case-memory model's calibrated probability for the flagged transaction
  (calibrated on the October hold-out). The bank's own risk score is reported but deliberately given almost
  no weight: in the bank's memory every one of the 900 score-only alerts was a false alarm.
* The LLM never computes these numbers. It reads them.
"""
from __future__ import annotations

import math
import os
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from agent.policy_config import POLICY

# DATA_SOURCE=client: the graph holds only the client's own data. Evidence wording then avoids facts about the
# reference bank, and a client-supplied model score is read as a calibrated probability.
CLIENT_MODE = os.getenv("DATA_SOURCE", "both").lower() == "client"

FMT = "%Y-%m-%d %H:%M:%S"

# October hold-out calibration of the memory model (prep/train_memory_model.py -> model_report.json)
CALIBRATION = [(0.0, 0.004), (0.025, 0.0105), (0.10, 0.104), (0.22, 0.188), (0.40, 0.335), (0.60, 0.448),
               (0.78, 0.604), (0.93, 0.69), (1.0, 0.72)]

FAMILY_CAP = {"trigger": (1 / 50, 50), "ml": (1 / 50, 50), "sequence": (1 / 20, 60), "network": (1 / 20, 80),
              "device": (1 / 5, 4), "behaviour": (1 / 30, 6), "history": (1 / 3, 3), "customer": (1 / 50, 20)}


def ts(s: str) -> datetime:
    return datetime.strptime(s[:19], FMT)


def fmt(d: datetime) -> str:
    return d.strftime(FMT)


def _money(rows) -> str:
    return ', '.join('$%.2f' % r['amount'] for r in rows)


def calibrate(score: float) -> float:
    for (x0, y0), (x1, y1) in zip(CALIBRATION, CALIBRATION[1:]):
        if score <= x1:
            return y0 + (y1 - y0) * (score - x0) / (x1 - x0)
    return CALIBRATION[-1][1]


def logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


@dataclass
class Evidence:
    claim: str
    source: str            # graph | document | customer | external
    ref: str               # query / document section / request id
    entity_ids: list[str]
    lr: float = 1.0
    family: str = "context"
    direction: str = "neutral"   # fraud | legit | neutral
    step: int = 0

    def to_answer(self) -> dict:
        return {"claim": self.claim, "source": self.source, "ref": self.ref, "entity_ids": self.entity_ids}

    def to_dict(self) -> dict:
        return asdict(self)


def ev(claim, source, ref, ids, lr=1.0, family="context", step=0) -> Evidence:
    direction = "fraud" if lr > 1.05 else ("legit" if lr < 0.95 else "neutral")
    return Evidence(claim, source, ref, [str(i) for i in ids], round(lr, 3), family, direction, step)


def fuse(prior: float, evidence: list[Evidence]) -> tuple[float, dict]:
    """Combine evidence. Returns probability and per-family log-LR contributions."""
    fam: dict[str, list[float]] = {}
    for e in evidence:
        if e.family in FAMILY_CAP and abs(math.log(max(e.lr, 1e-6))) > 1e-3:
            fam.setdefault(e.family, []).append(e.lr)
    contrib = {}
    x = logit(prior)
    for f, lrs in fam.items():
        up = max([l for l in lrs if l > 1] or [1.0])
        down = min([l for l in lrs if l < 1] or [1.0])
        lo, hi = FAMILY_CAP[f]
        lr = min(max(up * down, lo), hi)
        contrib[f] = round(math.log(lr), 3)
        x += math.log(lr)
    return min(max(sigmoid(x), 0.01), 0.99), contrib


def families(evidence: list[Evidence], direction: str, min_abs_log: float = math.log(1.5)) -> set[str]:
    out = set()
    for e in evidence:
        if e.family not in FAMILY_CAP:
            continue
        if direction == "fraud" and e.lr >= math.exp(min_abs_log):
            out.add(e.family)
        if direction == "legit" and e.lr <= math.exp(-min_abs_log):
            out.add(e.family)
    return out


# ---------------------------------------------------------------------------------------------------------
# Detectors. Each takes the investigation context (dict of tool results) and returns (evidence, findings)
# ---------------------------------------------------------------------------------------------------------
@dataclass
class Findings:
    episode: dict[str, dict] = field(default_factory=dict)        # txn_id -> row (the fraud episode)
    connected_cards: dict[str, str] = field(default_factory=dict)  # card_id -> why
    connected_devices: dict[str, str] = field(default_factory=dict)
    links: dict[str, set] = field(default_factory=dict)           # card_id -> link kinds (see agent/linkage.py)
    pattern_votes: dict[str, float] = field(default_factory=dict)
    flags: dict[str, object] = field(default_factory=dict)

    def link(self, card: str, kind: str, why: str) -> None:
        self.links.setdefault(card, set()).add(kind)
        if card not in self.connected_cards:
            self.connected_cards[card] = why
        elif why not in self.connected_cards[card]:
            self.connected_cards[card] += "; " + why

    def vote(self, pattern: str, w: float):
        self.pattern_votes[pattern] = max(self.pattern_votes.get(pattern, 0.0), w)


def specific_device(profile: str, global_n: int, max_n: int = 400) -> bool:
    parts = [p.strip() for p in profile.split("|")]
    filled = sum(bool(p) for p in parts)
    return bool(profile.strip()) and global_n <= max_n and filled >= 2


def d_trigger(case: dict, flagged: dict, step: int) -> list[Evidence]:
    t = case["trigger_type"]
    if t == "customer_report":
        why = ("Cardholder reports are weighted as strong evidence (calibrated on the reference dataset until this "
               "bank's own outcomes are available)" if CLIENT_MODE else
               "In the bank's memory, all 4,665 cardholder-reported cases were confirmed fraud")
        return [ev(f"Cardholder reported the transaction as unauthorised. {why}", "customer", "trigger:customer_report",
                   [case["customer_id"], flagged["id"]], 40.0, "trigger", step)]
    if t == "analyst_request":
        return [ev("Fraud analyst asked for a review of related activity", "external", "trigger:analyst_request",
                   [flagged["id"]], 2.0, "trigger", step)]
    rs = flagged.get("risk_score") or 0.0
    why = ("The bank's own score is treated as a reason to look, not as evidence of fraud" if CLIENT_MODE else
           "In the bank's memory, all 900 alerts raised by the score alone were cleared as false alarms, so the "
           "score is treated as a reason to look only")
    return [ev(f"Bank model scored the transaction {rs:.2f}. {why}",
               "graph", "txn_context", [flagged["id"]], 1.0, "trigger", step)]


def d_model(flagged: dict, step: int) -> tuple[float, list[Evidence]]:
    if not flagged.get("model_scored", True):
        p = POLICY.unscored_prior
        return p, [ev(f"No case-memory model score on this transaction (the feed does not carry one). The "
                      f"investigation starts from the base fraud rate of {p:.1%} and relies on the graph evidence",
                      "graph", "txn.model_score", [flagged["id"]], 1.0, "ml", step)]
    ms = flagged.get("model_score") or 0.0
    if CLIENT_MODE:
        p = min(max(ms, 0.001), 0.999)
        return p, [ev(f"Model score {ms:.3f} from the client feed, read as a calibrated fraud probability",
                      "graph", "txn.model_score", [flagged["id"]], 1.0, "ml", step)]
    p = calibrate(ms)
    return p, [ev(f"Case-memory model (trained on the bank's closed cases, AUC 0.91 on an October hold-out vs 0.87 "
                  f"for the bank score) gives this transaction {ms:.3f}, a calibrated fraud rate of {p:.1%}",
                  "graph", "txn.model_score", [flagged["id"]], 1.0, "ml", step)]


def d_recurring(flagged: dict, card_rows: list[dict], step: int, f: Findings) -> list[Evidence]:
    """Recurring charge: >= 3 earlier charges of the same amount (+-0.5%), same product, region and email (and,
    online, the same specific device), at a regular cadence (weekly to monthly)."""
    amt = flagged["amount"]
    tol = max(0.35, 0.005 * amt)
    online = flagged["channel"] == "online"
    same = [r for r in card_rows if r["id"] != flagged["id"] and abs(r["amount"] - amt) <= tol
            and r["product"] == flagged["product"] and r["addr1"] == flagged["addr1"]
            and r["p_email"] == flagged["p_email"]
            and (not online or (r["device_profile"] == flagged["device_profile"] and
                                sum(bool(p.strip()) for p in flagged["device_profile"].split("|")) >= 2))]
    before = sorted([r for r in same if r["ts"] < flagged["ts"]], key=lambda r: r["ts"])
    if len(before) < 3:
        return []
    days = [ts(r["ts"]) for r in before] + [ts(flagged["ts"])]
    gaps = [(b - a).total_seconds() / 86400 for a, b in zip(days, days[1:])]
    spaced = [g for g in gaps if g >= 4.5]
    if len(spaced) < 3 or (days[-1] - days[0]).days < 14:
        return []
    med = statistics.median(spaced)
    cv = statistics.pstdev(spaced) / med if med else 9
    if not (5 <= med <= 45) or cv > 1.5:
        return []
    cadence = "weekly" if med < 10 else ("monthly" if 20 <= med <= 45 else f"every ~{med:.0f} days")
    f.flags["recurring"] = {"n": len(before), "cadence": cadence, "ids": [r["id"] for r in before[-6:]]}
    return [ev(f"Recurring charge: {len(before)} earlier transactions of ${amt:.2f} (+-{tol:.2f}) with the same "
               f"product code {flagged['product']}, billing region {flagged['addr1'] or 'n/a'} and email, "
               f"{cadence} since {before[0]['ts'][:10]}", "graph", "card_window(recurring)",
               [r["id"] for r in before[-6:]] + [flagged["id"]], 0.08, "behaviour", step)]


def d_region(flagged: dict, client_rows: list[dict], card_profile: dict, card_rows: list[dict], step: int,
             f: Findings) -> list[Evidence]:
    if flagged["channel"] != "in_person" or not flagged["addr1"]:
        return []
    out = []
    reg = flagged["addr1"]
    prior_client = [r for r in client_rows if r["ts"] < flagged["ts"] and r["addr1"] == reg]
    card_region_n = int((card_profile.get("regions") or {}).get(reg, 0))
    if len(prior_client) >= 3:
        out.append(ev(f"Home region: this cardholder profile (client {flagged['client_id']}) made "
                      f"{len(prior_client)} earlier card-present purchases in billing region {reg}",
                      "graph", "client_history", [flagged["client_id"]] + [r["id"] for r in prior_client[-4:]],
                      0.3, "behaviour", step))
        f.flags["home_region"] = True
    elif card_region_n == 0:
        same_day_other = [r for r in card_rows if r["channel"] == "in_person" and r["addr1"] and r["addr1"] != reg
                          and abs((ts(r["ts"]) - ts(flagged["ts"])).total_seconds()) < 86400]
        in_new = [r for r in card_rows if r["addr1"] == reg and r["ts"] >= flagged["ts"]]
        days_new = {r["ts"][:10] for r in in_new}
        if len(days_new) >= 3:
            out.append(ev(f"Purchases in new region {reg} on {len(days_new)} separate days: consistent with a trip "
                          f"rather than a cloned card", "graph", "card_window", [r["id"] for r in in_new[:5]],
                          0.4, "behaviour", step))
        else:
            lr = 5.0 if same_day_other else 3.0
            msg = (f"; the card was also used in person in region(s) "
                   f"{sorted({r['addr1'] for r in same_day_other})[:3]} within 24h" if same_day_other else "")
            out.append(ev(f"Card-present use in billing region {reg}, where this card has no prior history{msg}",
                          "graph", "card_profile", [flagged["card_id"], flagged["id"]] +
                          [r["id"] for r in same_day_other[:3]], lr, "behaviour", step))
            f.vote("out_of_region_use", 0.8)
    elif card_region_n >= 10:
        out.append(ev(f"Billing region {reg} is routine for this card ({card_region_n} earlier purchases)",
                      "graph", "card_profile", [flagged["card_id"]], 0.6, "behaviour", step))
    return out


def d_card_testing(flagged: dict, card_rows: list[dict], step: int, f: Findings) -> list[Evidence]:
    """>=3 small (<$10) online authorisations within 60 minutes from one actor (same device or email),
    followed within 24h by a larger purchase from the same actor."""
    ft = ts(flagged["ts"])
    win = [r for r in card_rows if r["channel"] == "online" and ft - timedelta(hours=24) <= ts(r["ts"]) <= ft + timedelta(hours=2)]
    best = None
    for key in ("device_profile", "p_email"):
        groups: dict[str, list[dict]] = {}
        for r in win:
            if r[key]:
                groups.setdefault(r[key], []).append(r)
        for k, rows in groups.items():
            small = [r for r in rows if r["amount"] < 10]
            for i in range(len(small)):
                cl = [r for r in small if 0 <= (ts(r["ts"]) - ts(small[i]["ts"])).total_seconds() <= 3600]
                if len(cl) >= 3:
                    last = max(ts(r["ts"]) for r in cl)
                    big = [r for r in rows if r["amount"] >= 20 and last <= ts(r["ts"]) <= last + timedelta(hours=24)]
                    if big and (flagged in cl or flagged in big or flagged["id"] in {r["id"] for r in cl + big}):
                        if not best or len(cl) > len(best[0]):
                            best = (cl, big, key, k)
    if not best:
        return []
    cl, big, key, k = best
    for r in cl + big:
        f.episode[r["id"]] = r
    f.vote("card_testing", 1.0)
    f.flags["card_testing"] = {"small": [r["id"] for r in cl], "large": [r["id"] for r in big],
                               "large_cleared_over_100": any(r["amount"] > POLICY.money.card_testing_large_purchase for r in big)}
    return [ev(f"Card-testing sequence: {len(cl)} online authorisations under $10 within an hour "
               f"({_money(cl[:5])}) then {len(big)} larger purchase(s) up to "
               f"${max(r['amount'] for r in big):.2f}, all sharing {key.replace('_', ' ')} '{k}'",
               "graph", "card_window(card_testing)", [r["id"] for r in cl + big], 25.0, "sequence", step)]


def d_structuring(flagged: dict, card_rows: list[dict], step: int, f: Findings) -> list[Evidence]:
    """>=3 online authorisations just under $500 within 60 minutes with varied amounts (not one repeated
    product price), totalling > $1,300."""
    ft = ts(flagged["ts"])
    rows = sorted([r for r in card_rows if r["channel"] == "online" and 440 <= r["amount"] < 500
                   and abs((ts(r["ts"]) - ft).total_seconds()) <= 3 * 3600], key=lambda r: r["ts"])
    for i in range(len(rows)):
        cl = [r for r in rows if 0 <= (ts(r["ts"]) - ts(rows[i]["ts"])).total_seconds() <= 3600]
        amts = [r["amount"] for r in cl]
        if len(cl) >= 3 and sum(amts) > 1300 and (max(amts) - min(amts)) >= 5 and \
                any(r["id"] == flagged["id"] for r in cl):
            for r in cl:
                f.episode[r["id"]] = r
            f.vote("undocumented", 1.0)
            f.flags["structuring"] = {"ids": [r["id"] for r in cl], "total": round(sum(amts), 2),
                                      "minutes": round((ts(cl[-1]["ts"]) - ts(cl[0]["ts"])).total_seconds() / 60)}
            return [ev(f"Threshold structuring: {len(cl)} online purchases within "
                       f"{f.flags['structuring']['minutes']} minutes, each just under $500 "
                       f"({', '.join('$%.2f' % a for a in amts)}), total ${sum(amts):,.2f}. Amounts vary, so this is "
                       f"not one product bought repeatedly; they appear chosen to stay under a $500 threshold",
                       "graph", "card_window(structuring)", [r["id"] for r in cl], 40.0, "sequence", step)]
    return []


def d_structuring_network(flagged: dict, band_rows: list[dict], step: int, f: Findings) -> list[Evidence]:
    """Same modus operandi on other cards (amount_band_scan): coordinated scheme."""
    if "structuring" not in f.flags:
        return []
    by_card: dict[str, list[dict]] = {}
    for r in band_rows:
        by_card.setdefault(r["card_id"], []).append(r)
    hits = {}
    for card, rows in by_card.items():
        if card == flagged["card_id"]:
            continue
        rows.sort(key=lambda r: r["ts"])
        for i in range(len(rows)):
            cl = [r for r in rows if 0 <= (ts(r["ts"]) - ts(rows[i]["ts"])).total_seconds() <= 3600]
            amts = [r["amount"] for r in cl]
            if len(cl) >= 3 and sum(amts) > 1300 and (max(amts) - min(amts)) >= 5:
                hits[card] = cl
                break
    if not hits:
        return []
    for c, cl in hits.items():
        f.link(c, "structuring", f"same structuring pattern ({len(cl)} x just-under-$500 within an hour on "
                                 f"{cl[0]['ts'][:10]}); linked by amount and timing, no shared device")
    f.flags["structuring_network"] = sorted(hits)
    ids = sorted(hits)
    return [ev(f"The same structuring pattern appears on {len(hits)} other cards in the surrounding 30 days "
               f"({', '.join(ids[:6])}{'...' if len(ids) > 6 else ''}): coordinated, repeated abuse across customers",
               "graph", "amount_band_scan(440,500)", ids, 6.0, "network", step)]


def d_device(flagged: dict, card_profile: dict, step: int, f: Findings) -> list[Evidence]:
    if flagged["channel"] != "online":
        return []
    out = []
    dp = flagged["device_profile"]
    seen = int((card_profile.get("devices") or {}).get(dp, 0)) if dp else 0
    if flagged["device_status"] == "New":
        out.append(ev("Identity record marks the device as New for this account", "graph", "txn_context",
                      [flagged["id"]], 1.6, "device", step))
    if flagged["proxy"]:
        kind = flagged["proxy"].split(":")[-1].lower()
        out.append(ev(f"Connection through a {kind} proxy", "graph", "txn_context", [flagged["id"]],
                      2.5 if kind in ("anonymous", "hidden") else 1.3, "device", step))
    if dp and seen >= 2 and flagged["device_status"] != "New":
        out.append(ev(f"Device profile '{dp}' already used {seen} times on this card", "graph", "card_profile",
                      [flagged["card_id"]], 0.6, "device", step))
    return out


def d_device_ring(flagged: dict, fan: dict, dev_cases: dict, step: int, f: Findings) -> list[Evidence]:
    dp = flagged["device_profile"]
    if not dp:
        return []
    gn = int(fan.get("device", {}).get("n_txns") or 0)
    rows = fan.get("txns", [])
    others = [r for r in rows if r["card_id"] != flagged["card_id"]]
    cards = sorted({r["card_id"] for r in others})
    out = []
    confirmed = [c for c in dev_cases.get("closed_cases", []) if c.get("outcome") == "confirmed_fraud"]
    undocumented = [c for c in confirmed if c.get("pattern") == "undocumented"]
    if not specific_device(dp, gn) or len(cards) < 2:
        if confirmed and specific_device(dp, gn):
            out.append(ev(f"Device profile appears in {len(confirmed)} confirmed closed case(s)", "graph",
                          "device_cases", [c["id"] for c in confirmed[:5]], 2.0, "history", step))
        return out
    n_new = sum(r["device_status"] == "New" for r in others)
    n_proxy = sum(bool(r["proxy"]) for r in others)
    susp = {r["card_id"] for r in others if r["model_score"] >= 0.5}
    amts = [r["amount"] for r in others]
    similar_amt = len(amts) >= 2 and statistics.pstdev(amts + [flagged["amount"]]) / max(1.0, statistics.mean(amts + [flagged["amount"]])) < 0.1
    indicators = []
    if n_new >= 0.6 * len(others):
        indicators.append(f"marked New on {n_new}/{len(others)} uses")
    if n_proxy >= 0.5 * len(others):
        indicators.append(f"behind a proxy on {n_proxy}/{len(others)} uses")
    if len(susp) >= 2 or (len(susp) >= 1 and len(cards) <= 3):
        indicators.append(f"high memory-model scores on {len(susp)} of those cards")
    strong = bool(susp) or (len(cards) >= 3 and len(indicators) >= 2)
    if similar_amt:
        indicators.append("near-identical amounts")
    if undocumented or confirmed:
        indicators.append(f"linked to {len(confirmed)} confirmed closed case(s)")
    if not indicators or not strong:
        out.append(ev(f"Device profile shared with {len(cards)} other card(s) in the window"
                      + (f" ({'; '.join(indicators)})" if indicators else "") + ", too weak to indicate a ring",
                      "graph", "device_fanout", cards[:5], 1.0, "network", step))
        return out
    lr = min(80.0, 5.0 + 2.5 * len(cards) + (15.0 if confirmed else 0) + 5.0 * len(susp))
    for c in cards:
        f.link(c, "shared_device", f"shares device profile '{dp}'")
    f.connected_devices[dp] = f"used by {len(cards) + 1} cards between {rows[0]['ts'][:10]} and {rows[-1]['ts'][:10]}"
    for r in [r for r in rows if r["card_id"] == flagged["card_id"]]:
        f.episode[r["id"]] = r
    f.flags["device_ring"] = {"device": dp, "cards": cards, "indicators": indicators,
                              "closed_cases": [c["id"] for c in confirmed[:8]],
                              "undocumented_memory": [c["id"] for c in undocumented[:8]]}
    if undocumented:
        f.vote("undocumented", 0.95)
    out.append(ev(f"Shared device: profile '{dp}' (only {gn} uses in {'all the data' if CLIENT_MODE else 'the whole dataset'}) was used on {len(cards)} other "
                  f"card(s) within +-10 days: {'; '.join(indicators)}", "graph", "device_fanout",
                  [dp] + cards[:25], lr, "network", step))
    if confirmed:
        out.append(ev(f"Case memory: this device profile is on confirmed closed case(s) "
                      f"{', '.join(c['id'] for c in confirmed[:6])}"
                      + (f" (analysts labelled {len(undocumented)} of them as an undocumented pattern)" if undocumented else ""),
                      "graph", "device_cases", [c["id"] for c in confirmed[:6]], 3.0, "history", step))
    return out


def d_peers(flagged: dict, peers: list[dict], step: int, f: Findings) -> list[Evidence]:
    """Other cards, same product, similar amount, +-48h, sharing the device profile or the purchaser AND
    recipient email domains - and scored as likely fraud by the memory model."""
    dev_ok = sum(bool(p.strip()) for p in flagged["device_profile"].split("|")) >= 2
    cand = [r for r in peers if r["card_id"] != flagged["card_id"] and r["product"] == flagged["product"]
            and ((r["via_device"] and dev_ok) or (r["p_email"] == flagged["p_email"] and r["r_email"] == flagged["r_email"]
                                     and flagged["p_email"]))]
    susp = [r for r in cand if r["model_score"] >= 0.5]
    cards = sorted({r["card_id"] for r in susp})
    if not cards:
        if cand:
            return [ev(f"{len(cand)} similar transactions on other cards (same email/device, amount within 1.5%, +-48h) "
                       f"show no fraud indicators", "graph", "peer_txns", [r["id"] for r in cand[:5]], 1.0,
                       "network", step)]
        return []
    for c in cards:
        rows_c = [r for r in susp if r["card_id"] == c]
        if dev_ok and any(r["via_device"] for r in rows_c):
            f.link(c, "peer_device", "near-identical purchase within 48h on the same device profile, scored as likely fraud")
        else:
            f.link(c, "peer_email", "near-identical purchase within 48h with the same purchaser and recipient email "
                                    "domains, scored as likely fraud")
    if dev_ok and any(r["via_device"] for r in susp):
        f.connected_devices.setdefault(flagged["device_profile"], f"also used on {', '.join(cards[:5])}")
    f.flags["peer_cluster"] = {"cards": cards, "txns": [r["id"] for r in susp]}
    lr = 10.0 if len(cards) >= 2 else 3.0
    return [ev(f"What happened on other cards: {len(susp)} transaction(s) on {len(cards)} other card(s) "
               f"({', '.join(cards[:6])}) within 48h at the same amount (+-1.5%), same product code and shared "
               f"{'device profile' if any(r['via_device'] for r in susp) else 'purchaser/recipient email domains'}, "
               f"each scored >= 0.5 by the memory model", "graph", "peer_txns",
               [r["id"] for r in susp[:8]] + cards[:6], lr, "network", step)]


def d_episode(flagged: dict, card_rows: list[dict], step: int, f: Findings) -> list[Evidence]:
    """CNP burst: same-card online transactions within 48h sharing the flagged device profile or email pair,
    that the memory model also scores as likely fraud."""
    if flagged["channel"] != "online":
        return []
    ft = ts(flagged["ts"])
    mates = [r for r in card_rows if r["id"] != flagged["id"] and r["channel"] == "online"
             and abs((ts(r["ts"]) - ft).total_seconds()) <= 48 * 3600 and r["model_score"] >= 0.5
             and ((flagged["device_profile"] and r["device_profile"] == flagged["device_profile"])
                  or (r["p_email"] == flagged["p_email"] and r["r_email"] == flagged["r_email"] and flagged["p_email"]
                      and abs(r["amount"] - flagged["amount"]) <= 0.2 * flagged["amount"]))]
    if not mates:
        return []
    for r in mates:
        f.episode[r["id"]] = r
    f.flags["burst"] = [r["id"] for r in mates]
    return [ev(f"Burst: {len(mates)} other online transaction(s) on this card within 48h from the same "
               f"device/email ({_money(mates[:4])}), also scored as likely fraud",
               "graph", "card_window(burst)", [r["id"] for r in mates], 2.5, "sequence", step)]


def d_amount_profile(flagged: dict, card_profile: dict, step: int) -> list[Evidence]:
    amts = sorted(card_profile.get("amounts") or [])
    if len(amts) < 10:
        return []
    p90 = amts[int(0.9 * (len(amts) - 1))]
    prods = card_profile.get("products") or {}
    out = []
    if flagged["amount"] > p90 * 1.5 and flagged["amount"] > 150:
        out.append(ev(f"Amount ${flagged['amount']:.2f} is well above this card's 90th percentile (${p90:.2f})",
                      "graph", "card_profile", [flagged["card_id"]], 1.4, "behaviour", step))
    if flagged["product"] not in prods:
        out.append(ev(f"Product code {flagged['product']} never used before on this card", "graph", "card_profile",
                      [flagged["card_id"]], 1.3, "behaviour", step))
    return out


def d_history(flagged: dict, prior: dict, step: int) -> list[Evidence]:
    subj = [c for c in prior.get("closed_cases", []) if c["card_id"] == flagged["card_id"]]
    conf = [c for c in subj if c["outcome"] == "confirmed_fraud"]
    cleared = [c for c in subj if c["outcome"] == "cleared"]
    out = []
    if conf:
        pats = sorted({c["pattern"] for c in conf})
        out.append(ev(f"Card has {len(conf)} earlier confirmed fraud case(s) ({', '.join(pats)}), most recent "
                      f"{max(c['opened_at'] for c in conf)[:10]}; the card was reissued each time, so this is context, "
                      f"not proof", "graph", "prior_cases", [c["id"] for c in conf[-4:]], 1.3, "history", step))
    if cleared:
        out.append(ev(f"Card also has {len(cleared)} cleared alert(s) (e.g. {cleared[-1]['analyst_notes'][:90]})",
                      "graph", "prior_cases", [c["id"] for c in cleared[-3:]], 0.85, "history", step))
    return out


def d_ato(flagged: dict, card_rows: list[dict], f: Findings, step: int) -> list[Evidence]:
    ft = ts(flagged["ts"])
    near = [r for r in card_rows if r["client_id"] == flagged["client_id"] and abs((ts(r["ts"]) - ft).total_seconds()) <= 48 * 3600
            and r["model_score"] >= 0.5]
    chans = {r["channel"] for r in near}
    if len(chans) == 2 and len(near) >= 2:
        f.vote("account_takeover", 0.85)
        for r in near:
            f.episode[r["id"]] = r
        return [ev(f"Mixed-channel activity for the same cardholder within 48h ({len(near)} likely-fraud transactions "
                   f"across in-person and online), consistent with compromised credentials", "graph", "client_history",
                   [r["id"] for r in near], 2.0, "sequence", step)]
    return []
