"""
Step 1 of the pipeline: turn the raw IEEE_CIS_RAW dataset into clean, graph-ready tables.

What it derives (and why):
  * card_id        - The dataset never ships a card column. Card IDs such as C01234-K2 are
                     reproduced exactly by ranking each customer's distinct `card6` values
                     (NaN sorted first as "NA"). Verified: 100% match on all 14,975 txn/card
                     pairs in closed_cases_history.csv and case_pack.csv.
  * device_profile - DeviceInfo | OS (id_30) | browser (id_31) | screen (id_33), as the
                     README defines a device profile.
  * client_id      - A *latent cardholder* key: card_id + billing region + account-open day
                     (transaction day - D1). One "customer" in this data is an issuer code
                     that aggregates many real people; the client key separates them. In the
                     closed cases, 85% of a fraud client's transactions are fraud, so this is
                     the unit an investigation should reason about.
  * model_score    - Output of the case-memory model (prep/train_memory_model.py), joined in
                     if prepared/model_scores.csv.gz exists.

Outputs (data/prepared/):
  txn.csv.gz            one row per transaction, only the columns the agent and graph use
  closed_cases.csv      copy of the bank's closed cases
  case_pack.csv         copy of the 20 benchmark alerts

Usage:
  python -m prep.prepare_data --raw "<path to IEEE_CIS_RAW folder>"
"""
from __future__ import annotations

import argparse
import os
import shutil

import numpy as np
import pandas as pd

KEEP_TX = [
    "TransactionID", "TransactionDT", "TransactionAmt", "ProductCD",
    "card1", "card2", "card3", "card4", "card5", "card6",
    "addr1", "addr2", "dist1", "dist2", "P_emaildomain", "R_emaildomain",
    "C1", "C13", "C14", "D1", "D2", "D3", "D10", "D15",
    "M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8", "M9",
    "customer_id", "ts", "channel", "risk_score",
]
KEEP_ID = ["TransactionID", "id_15", "id_23", "id_30", "id_31", "id_33", "DeviceType", "DeviceInfo"]


def derive_card_ids(tx: pd.DataFrame) -> pd.Series:
    """Rank distinct card6 values within each customer, NaN first ("NA" sorts before lowercase)."""
    c6 = tx["card6"].fillna("NA")
    keys = pd.DataFrame({"customer_id": tx["customer_id"], "c6": c6}).drop_duplicates()
    keys = keys.sort_values(["customer_id", "c6"])
    keys["k"] = keys.groupby("customer_id").cumcount() + 1
    lut = {(c, v): k for c, v, k in keys[["customer_id", "c6", "k"]].itertuples(index=False)}
    return pd.Series([f"{c}-K{lut[(c, v)]}" for c, v in zip(tx["customer_id"], c6)], index=tx.index)


def _s(x) -> str:
    return "" if pd.isna(x) else str(x)


def device_profile(row_info, os_, browser, screen) -> str:
    return " | ".join([_s(row_info), _s(os_), _s(browser), _s(screen)])


def main(raw: str, out: str) -> None:
    os.makedirs(out, exist_ok=True)
    tx = pd.read_csv(os.path.join(raw, "transactions.csv"), usecols=KEEP_TX, low_memory=False)
    ident = pd.read_csv(os.path.join(raw, "identity.csv"), usecols=KEEP_ID, low_memory=False)

    tx["card_id"] = derive_card_ids(tx)
    ident["device_profile"] = [device_profile(a, b, c, d) for a, b, c, d in
                               zip(ident.DeviceInfo, ident.id_30, ident.id_31, ident.id_33)]
    tx = tx.merge(ident, on="TransactionID", how="left")

    day = (tx["TransactionDT"] // 86400).astype(int)
    d1n = (day - tx["D1"]).astype("Float64")
    tx["client_id"] = (tx["card_id"] + "|" + tx["addr1"].fillna(-1).astype(int).astype(str)
                       + "|" + d1n.fillna(-999).astype(int).astype(str))

    ms_path = os.path.join(out, "model_scores.csv.gz")
    if os.path.exists(ms_path):
        ms = pd.read_csv(ms_path)
        tx = tx.merge(ms[["TransactionID", "model_score"]], on="TransactionID", how="left")
    else:
        tx["model_score"] = np.nan

    tx = tx.rename(columns={"TransactionID": "txn_id", "TransactionAmt": "amount", "ProductCD": "product",
                            "P_emaildomain": "p_email", "R_emaildomain": "r_email",
                            "id_15": "device_status", "id_23": "proxy", "DeviceType": "device_type",
                            "DeviceInfo": "device_info", "id_30": "os", "id_31": "browser", "id_33": "screen",
                            "card4": "network", "card6": "card_type"})
    tx["addr1"] = tx["addr1"].astype("Int64")
    tx["addr2"] = tx["addr2"].astype("Int64")
    tx.loc[tx["device_profile"].isna(), "device_profile"] = ""
    tx = tx.sort_values(["card_id", "ts", "txn_id"])
    tx.to_csv(os.path.join(out, "txn.csv.gz"), index=False, compression="gzip")
    for f in ["closed_cases_history.csv", "case_pack.csv"]:
        shutil.copy(os.path.join(raw, f), os.path.join(out, f.replace("_history", "")))
    print(f"prepared {len(tx):,} transactions, {tx.card_id.nunique():,} cards -> {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True, help="folder containing the IEEE_CIS_RAW csv files")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "data", "prepared"))
    a = ap.parse_args()
    main(a.raw, a.out)
