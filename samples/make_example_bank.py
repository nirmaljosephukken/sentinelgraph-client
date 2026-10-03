"""
Write samples/example_bank/*.csv: a synthetic bank (from the scenario lab) exported the way a real Indian bank
might export it, to test the intake and the example_bank mapping end to end.

    python samples/make_example_bank.py
"""
from __future__ import annotations

import csv
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent.scenarios import generate  # noqa: E402

OUT = Path(__file__).resolve().parent / "example_bank"


def main(seed: int = 7) -> None:
    sc = generate("device_ring", seed=seed, linked=6, proxy=True)
    rng = random.Random(seed)
    t = sc.backend.t
    tok = {c: f"CT{rng.getrandbits(40):010X}" for c in t["card_id"].unique()}
    cif = {c: f"CIF{1000000 + i}" for i, c in enumerate(sorted(t["customer_id"].unique()))}
    ref = {i: f"TXN{400000000 + n}" for n, i in enumerate(t["id"])}
    users = {c: rng.choice(["asha", "ravi.k", "meera_s", "kiran", "vik", "anil88", "deepa.r"]) for c in t["customer_id"].unique()}
    rows = []
    for r in t.to_dict("records"):
        utc = datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S")
        local = utc + timedelta(hours=5, minutes=30)
        dom = r["p_email"] or ""
        rows.append({
            "TXN_REF": ref[r["id"]], "TXN_DATE_TIME": local.strftime("%d/%m/%Y %H:%M:%S"),
            "AMOUNT_PAISE": str(int(round(r["amount"] * 100))), "CARD_TOKEN": tok[r["card_id"]],
            "CIF": cif[r["customer_id"]], "CHANNEL": ("ECOM" if r["channel"] == "online" else rng.choice(["POS", "POS", "ATM"])),
            "MCC": r["product"], "RULE_SCORE": str(int(round(r["risk_score"] * 100))),
            "BILLING_PIN": f"{int(r['addr1']):03d}{rng.randint(0, 999):03d}",
            "CUSTOMER_EMAIL": f"{users[r['customer_id']]}@{dom}" if dom else "",
            "DEVICE_FINGERPRINT": r["device_profile"] or "",
            "DEVICE_CLASS": {"mobile": "MOB", "desktop": "WEB"}.get(r["device_type"] or "", ""),
            "DEVICE_SEEN_BEFORE": {"Found": "Y", "New": "N"}.get(r["device_status"] or "", ""),
            "VPN_FLAG": "1" if r["proxy"] else ("0" if r["channel"] == "online" else ""),
            "SCHEME": r["network"].upper(), "CARD_KIND": r["card_type"].upper(),
        })
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "transactions.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    a = sc.alert
    fx = sc.backend.t.set_index("id").loc[str(a["flagged_txn_id"])]
    opened = datetime.strptime(a["opened_at"][:19], "%Y-%m-%d %H:%M:%S") + timedelta(hours=5, minutes=30)
    with open(OUT / "alerts.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ALERT_ID", "TXN_REF", "ALERT_TYPE", "ALERT_TEXT", "ALERT_TIME"])
        w.writerow(["EB-ALERT-0001", ref[str(a["flagged_txn_id"])], "RULE",
                    f"Rule engine scored {ref[str(a['flagged_txn_id'])]} (INR {fx['amount']:,.2f}, online) at "
                    f"{int(round(fx['risk_score'] * 100))}/100. Review and decide.", opened.strftime("%d/%m/%Y %H:%M:%S")])
    print(f"wrote {len(rows)} transactions and 1 alert to {OUT}; planted: {sc.planted.get('description')}; expect {sc.expect}")


if __name__ == "__main__":
    main()
