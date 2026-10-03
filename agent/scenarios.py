"""
Scenario lab: generate a fresh synthetic bank, plant one pattern, let the unchanged agent investigate it.

    world = background cardholders with a home region, their own devices and email, everyday spending
            (in person and online) over ~3.5 months, all synthetic
    plant = one scenario on top: a fraud pattern, or a harmless look-alike
    alert = what the bank would raise (a risk-score alert or a customer report) on one planted transaction

Nothing in the world says "fraud". The planted transactions look like the real thing: shared devices, timing,
amounts, regions. The agent (same detectors, policy and stop rule as in production) has to find the pattern and
reach a verdict, which the lab then compares with what was planted. Scores the bank's case-memory model would
produce are drawn synthetically for the scenario (low for everyday spending, higher for planted fraud), and are
labelled as such in the UI.
"""
from __future__ import annotations

import random
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from agent.backends import LocalBackend
from agent.config import SETTINGS
from agent import knowledge

START = datetime(2026, 6, 1)
EVENT = datetime(2026, 9, 10, 14, 0)
EMAILS = ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "aol.com"]
PHONES = [("SM-G991B", "Android 14"), ("Pixel 7", "Android 14"), ("SM-A536E", "Android 13"), ("moto g54", "Android 13"),
          ("CPH2449", "Android 13"), ("2201117TI", "Android 12")]
DESKTOPS = [("Windows", "Windows 11", "chrome 128.0"), ("MacOS", "Mac OS X 14_5", "safari 17.5"),
            ("Windows", "Windows 10", "edge 127.0"), ("Linux", "Ubuntu 22.04", "firefox 129.0")]

SCENARIOS = {
    "device_ring": {"label": "Device ring", "expect": "fraud",
                    "blurb": "One device, behind a proxy, makes near-identical online purchases on several cards within days."},
    "structuring": {"label": "Threshold structuring", "expect": "fraud",
                    "blurb": "Bursts of online purchases just under $500 within an hour, repeated on other cards. No shared device."},
    "card_testing": {"label": "Card testing", "expect": "fraud",
                     "blurb": "Several tiny authorisations within an hour from one device, then one large purchase."},
    "traveller": {"label": "Traveller (harmless)", "expect": "legitimate",
                  "blurb": "The bank's score flags card-present use far from home, but the card keeps being used there for days: a trip."},
    "subscription": {"label": "Disputed subscription (harmless)", "expect": "legitimate",
                     "blurb": "The customer disputes a charge that has recurred monthly for months, same amount, device and email."},
}


class SandboxBackend(LocalBackend):
    """The in-memory graph over a generated world. Cases are written to a throwaway folder, never to the bank graph."""
    name = "sandbox"
    _chunks = None

    def __init__(self, t, cc: list[dict] | None = None):
        self.s = SETTINGS
        self.transport_log = []
        self._index(t, cc or [])
        self.cc_emb = []
        if SandboxBackend._chunks is None:
            SandboxBackend._chunks = knowledge.chunks()
        self.chunks = SandboxBackend._chunks
        from pathlib import Path
        self.case_dir = Path(tempfile.mkdtemp(prefix="sg-lab-"))

    def _log(self, q):
        self.transport_log.append(f"sandbox:{q}")


@dataclass
class Scenario:
    kind: str
    backend: SandboxBackend
    alert: dict
    expect: str
    planted: dict
    stats: dict = field(default_factory=dict)


class _World:
    def __init__(self, rng: random.Random, n_cards: int):
        self.rng = rng
        self.rows: list[dict] = []
        self.next_id = 3900000
        self.cards = []
        for i in range(n_cards):
            self.cards.append(self._card(i))
        for c in self.cards:
            self._everyday(c)

    # ---------------------------------------------------------------- building blocks
    def device(self, mobile: bool | None = None) -> tuple[str, str]:
        r = self.rng
        if mobile if mobile is not None else r.random() < 0.6:
            model, osv = r.choice(PHONES)
            prof = f"{model} Build/{r.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')}{r.randint(1, 9)}{r.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')}{r.randint(10, 99)}.{r.randint(100000, 999999)} | {osv} | chrome {r.randint(118, 128)}.0 for android | {r.choice(['1080x2400', '1080x2340', '720x1600'])}"
            return prof, "mobile"
        a, b, c = r.choice(DESKTOPS)
        return f"{a} | {b} | {c} | {r.choice(['1920x1080', '2560x1440', '1366x768', '1536x864'])} | build {r.randint(1000, 9999)}", "desktop"

    def _card(self, i: int) -> dict:
        r = self.rng
        cust = f"C9{i:04d}"
        card = f"{cust}-K1"
        home = r.randint(100, 499)
        dev, dtype = self.device()
        email = r.choice(EMAILS)
        return {"customer_id": cust, "card_id": card, "home": home, "client_id": f"{card}|{home}|{r.randint(10, 99)}",
                "device": dev, "device_type": dtype, "email": email, "network": r.choice(["visa", "mastercard", "rupay"]),
                "card_type": r.choice(["debit", "debit", "credit"])}

    def add(self, c: dict, when: datetime, amount: float, **kw) -> dict:
        r = self.rng
        online = kw.get("channel", "in_person") == "online"
        row = {"id": str(self.next_id), "ts": when.strftime("%Y-%m-%d %H:%M:%S"), "amount": round(amount, 2),
               "product": kw.get("product", "C" if online else "W"), "channel": "online" if online else "in_person",
               "risk_score": round(kw.get("risk_score", r.betavariate(2, 12)), 2),
               "model_score": round(kw.get("model_score", r.betavariate(0.6, 90)), 4),
               "addr1": kw.get("addr1", c["home"]), "addr2": 87,
               "dist1": None if online else round(r.uniform(0, 18), 0),
               "p_email": kw.get("p_email", c["email"] if online else ""),
               "r_email": kw.get("r_email", c["email"] if online and r.random() < 0.5 else ""),
               "device_status": kw.get("device_status", "Found" if online else ""),
               "proxy": kw.get("proxy", ""), "device_profile": kw.get("device", c["device"] if online else ""),
               "device_type": kw.get("device_type", c["device_type"] if online else ""),
               "m4": "M0" if r.random() < 0.6 else "M1", "m6": "T" if r.random() < 0.5 else "F", "d1": r.randint(30, 600),
               "card_id": c["card_id"], "customer_id": c["customer_id"], "client_id": c["client_id"],
               "network": c["network"], "card_type": c["card_type"]}
        self.next_id += 1
        self.rows.append(row)
        return row

    def _everyday(self, c: dict) -> None:
        r = self.rng
        t = START + timedelta(hours=r.uniform(0, 72))
        while t < EVENT + timedelta(days=5):
            if r.random() < 0.72:
                self.add(c, t.replace(hour=r.randint(8, 21), minute=r.randint(0, 59)), r.lognormvariate(3.7, 0.6))
            else:
                self.add(c, t.replace(hour=r.randint(7, 23), minute=r.randint(0, 59)), r.lognormvariate(3.9, 0.7),
                         channel="online", product=r.choice(["C", "H", "R"]))
            t += timedelta(days=r.uniform(1.2, 4.5))

    def others(self, exclude: dict, n: int) -> list[dict]:
        return self.rng.sample([c for c in self.cards if c is not exclude], n)


def _alert(kind: str, row: dict, c: dict, trigger: str, opened: datetime) -> dict:
    if trigger == "customer_report":
        text = f"Customer {c['customer_id']} message: 'I never made this ${row['amount']:.2f} purchase. Please check my card.'"
        risk = ""
    else:
        where = "online" if row["channel"] == "online" else f"in billing region {row['addr1']}"
        risk = f"{row['risk_score']:.2f}"
        text = (f"Real-time model scored transaction {row['id']} (${row['amount']:,.2f}, {where}) at {risk}. "
                f"Review and decide.")
    return {"case_id": f"LAB-{uuid.uuid4().hex[:5].upper()}", "opened_at": opened.strftime("%Y-%m-%d %H:%M:%S"),
            "trigger_type": trigger, "trigger_text": text, "flagged_txn_id": row["id"], "card_id": c["card_id"],
            "customer_id": c["customer_id"], "risk_score": risk}


def generate(kind: str, seed: int | None = None, linked: int = 5, proxy: bool = True, amount: float | None = None,
             trigger: str | None = None, n_cards: int = 150) -> Scenario:
    """Build a world, plant scenario `kind`, return the alert to investigate."""
    import pandas as pd
    seed = seed if seed is not None else random.randrange(1, 10_000)
    r = random.Random(seed)
    w = _World(r, n_cards)
    target = w.cards[0]
    planted: dict = {"seed": seed}

    if kind == "device_ring":
        amt = amount or round(r.uniform(60, 180), 2)
        dev, dtype = w.device(mobile=True)
        victims = [target] + w.others(target, linked)
        ring = []
        for j, c in enumerate(victims[1:] + [target]):
            when = EVENT - timedelta(days=4) + timedelta(hours=j * r.uniform(6, 16))
            for _ in range(r.choice([1, 1, 2])):
                ring.append(w.add(c, when, amt + r.uniform(-0.6, 0.6), channel="online", product="C", device=dev,
                                  device_type=dtype, device_status="New", p_email="protonmail.com", r_email="gmail.com",
                                  proxy="IP_PROXY:ANONYMOUS" if proxy else "", model_score=r.uniform(0.55, 0.85),
                                  risk_score=r.uniform(0.6, 0.95)))
                when += timedelta(minutes=r.randint(20, 300))
        flagged = ring[-1]
        trigger = trigger or "risk_score"
        planted.update(description=f"One device used on {len(victims)} cards within days, near-identical ${amt:.2f} "
                                   f"purchases{', behind an anonymous proxy' if proxy else ''}",
                       device=dev, cards=[c["card_id"] for c in victims], txn_ids=[x["id"] for x in ring])

    elif kind == "structuring":
        def burst(c, day):
            when = day.replace(hour=r.randint(0, 23), minute=0) + timedelta(minutes=r.randint(0, 20))
            dev, dtype = w.device()
            out = []
            for amt in r.sample(range(445, 499), 4):
                out.append(w.add(c, when, amt + r.random(), channel="online", product="C", device=dev, device_type=dtype,
                                 device_status="New", model_score=r.uniform(0.04, 0.2), risk_score=r.uniform(0.3, 0.7)))
                when += timedelta(minutes=r.randint(5, 11))
            return out
        others = w.others(target, linked)
        for c in others:
            burst(c, EVENT + timedelta(days=r.randint(-20, 20)))
        mine = burst(target, EVENT)
        flagged = mine[-1]
        trigger = trigger or "customer_report"
        planted.update(description=f"4 online purchases just under $500 within an hour on the target card, and the same "
                                   f"pattern on {linked} other cards, each from its own device",
                       cards=[target["card_id"]] + [c["card_id"] for c in others], txn_ids=[x["id"] for x in mine])

    elif kind == "card_testing":
        dev, dtype = w.device(mobile=False)
        when = EVENT.replace(hour=2, minute=r.randint(0, 20))
        small = []
        for a in r.sample([0.99, 1.00, 1.50, 2.49, 3.00, 4.75, 5.00], 4):
            small.append(w.add(target, when, a, channel="online", product="C", device=dev, device_type=dtype,
                               device_status="New", p_email="mail.ru", proxy="IP_PROXY:HIDDEN" if proxy else "",
                               model_score=r.uniform(0.2, 0.45)))
            when += timedelta(minutes=r.randint(4, 12))
        big = w.add(target, when + timedelta(hours=r.uniform(2, 10)), amount or round(r.uniform(400, 900), 2),
                    channel="online", product="H", device=dev, device_type=dtype, device_status="New", p_email="mail.ru",
                    proxy="IP_PROXY:HIDDEN" if proxy else "", model_score=r.uniform(0.5, 0.8), risk_score=0.88)
        flagged = big
        trigger = trigger or "risk_score"
        planted.update(description=f"4 authorisations under $10 within an hour from one new device, then a "
                                   f"${big['amount']:.2f} purchase from the same device",
                       device=dev, cards=[target["card_id"]], txn_ids=[x["id"] for x in small] + [big["id"]])

    elif kind == "traveller":
        trip = r.choice([x for x in range(100, 500) if abs(x - target["home"]) > 80])
        days = max(3, min(linked, 7))
        trip_rows = []
        for d in range(days):
            for _ in range(r.randint(1, 3)):
                trip_rows.append(w.add(target, (EVENT + timedelta(days=d)).replace(hour=r.randint(9, 21),
                                                                                    minute=r.randint(0, 59)),
                                       r.lognormvariate(3.9, 0.5), addr1=trip, risk_score=r.uniform(0.3, 0.6)))
        trip_rows.sort(key=lambda x: x["ts"])
        flagged = trip_rows[0]
        flagged["risk_score"] = 0.81
        w.rows = [x for x in w.rows if not (x["card_id"] == target["card_id"] and x["channel"] == "in_person"
                                             and x["addr1"] == target["home"] and EVENT <= datetime.strptime(x["ts"], "%Y-%m-%d %H:%M:%S") <= EVENT + timedelta(days=days))]
        trigger = trigger or "risk_score"
        planted.update(description=f"Home region {target['home']}; card-present purchases in region {trip} on {days} "
                                   f"separate days from {EVENT:%d %b}", cards=[target["card_id"]],
                       txn_ids=[x["id"] for x in trip_rows])

    elif kind == "subscription":
        amt = amount or round(r.uniform(9, 60), 2)
        when = START + timedelta(days=r.randint(2, 9), hours=r.randint(1, 5))
        subs = []
        while when < EVENT:
            subs.append(w.add(target, when, amt, channel="online", product="S", p_email=target["email"],
                              r_email=target["email"], model_score=r.uniform(0.002, 0.02)))
            when += timedelta(days=r.choice([29, 30, 31]), minutes=r.randint(-40, 40))
        flagged = subs[-1]
        trigger = trigger or "customer_report"
        planted.update(description=f"{len(subs)} monthly charges of ${amt:.2f} since {subs[0]['ts'][:10]}, same "
                                   f"product, device and email; the customer disputes the latest one",
                       cards=[target["card_id"]], txn_ids=[x["id"] for x in subs])
    else:
        raise ValueError(f"unknown scenario {kind}")

    opened = datetime.strptime(flagged["ts"], "%Y-%m-%d %H:%M:%S") + timedelta(minutes=45 if trigger == "risk_score" else 360)
    alert = _alert(kind, flagged, target, trigger, opened)
    t = pd.DataFrame(w.rows)
    g = SandboxBackend(t)
    stats = {"cards": len(w.cards), "transactions": len(t), "devices": int((t.device_profile != "").sum() and
                                                                        t.loc[t.device_profile != "", "device_profile"].nunique())}
    return Scenario(kind, g, alert, SCENARIOS[kind]["expect"], planted, stats)
