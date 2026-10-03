"""
Case-memory model: learn from the bank's own closed investigations.

The closed cases (Jul-Oct) are the only place the truth is written down. They cover ~3.4% of
Jul-Oct transactions as confirmed fraud, which matches the base rate of the underlying data, so
"not in a confirmed case" is a usable (noisy) negative label. We train a gradient-boosted model on
all 393 original Vesta features plus behavioural aggregates, validate on a strict time split
(train Jul-Sep, test Oct), then refit on Jul-Oct with negative down-sampling (re-weighted so that
probabilities stay calibrated) and score every transaction.

This score is ONE evidence signal for the agent ("memory_model_score"). The agent never treats it
as a verdict: it is fused with graph evidence, pattern detectors, customer responses and policy.

Result on the October hold-out (reproduce with this script):
    memory model  AUC 0.914  AP 0.46
    bank risk     AUC 0.866  AP 0.25

Usage:
  python -m prep.train_memory_model --raw "<IEEE_CIS_RAW folder>"   (needs ~6 GB RAM, ~6 min)
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import time

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import average_precision_score, roc_auc_score

from prep.prepare_data import derive_card_ids

CAT = ["ProductCD", "card4", "card6", "P_emaildomain", "R_emaildomain", "M1", "M2", "M3", "M4", "M5",
       "M6", "M7", "M8", "M9", "id_12", "id_15", "id_16", "id_23", "id_27", "id_28", "id_29", "id_30",
       "id_31", "id_33", "id_34", "id_35", "id_36", "id_37", "id_38", "DeviceType", "DeviceInfo"]
VCOLS = [f"V{i}" for i in range(1, 340)]


def build_matrix(raw: str):
    cols = pd.read_csv(os.path.join(raw, "transactions.csv"), nrows=0).columns
    base = [c for c in cols if not c.startswith("V")]
    t = pd.read_csv(os.path.join(raw, "transactions.csv"), usecols=base, low_memory=False)
    t = t.merge(pd.read_csv(os.path.join(raw, "identity.csv"), low_memory=False), on="TransactionID", how="left")
    t = t.sort_values("TransactionID").reset_index(drop=True)
    t["card_id"] = derive_card_ids(t)
    dp = (t.DeviceInfo.fillna("") + "|" + t.id_30.fillna("") + "|" + t.id_31.fillna("") + "|" + t.id_33.fillna(""))
    cc = pd.read_csv(os.path.join(raw, "closed_cases_history.csv"))
    fr = cc[cc.outcome == "confirmed_fraud"].txn_ids.str.split("|").explode().astype(int)
    y = t.TransactionID.isin(set(fr)).astype(int).values
    day = (t.TransactionDT // 86400).astype(int)
    d1n = day - t.D1
    uid = t.card_id + "_" + t.addr1.fillna(-1).astype(str) + "_" + d1n.fillna(-999).astype(str)
    F = {"hour": pd.to_datetime(t.ts).dt.hour, "cents": (t.TransactionAmt * 100 % 100).round(), "D1n": d1n}
    for k, s in [("uid", uid), ("card", t.card_id)]:
        g = t.TransactionAmt.groupby(s)
        F[k + "_amt_mean"] = g.transform("mean")
        F[k + "_n"] = g.transform("size")
        F[k + "_amt_ratio"] = t.TransactionAmt / F[k + "_amt_mean"]
    F["dev_cards"] = t.card_id.groupby(dp).transform("nunique")
    for c in CAT:
        F[c + "_c"] = t[c].astype("category").cat.codes
    for c in t.columns:
        if c not in CAT and pd.api.types.is_numeric_dtype(t[c]) and c not in ("TransactionID", "TransactionDT", "risk_score"):
            F[c] = t[c]
    names = list(F)
    X = np.empty((len(t), len(names) + len(VCOLS)), np.float32)
    for j, k in enumerate(names):
        X[:, j] = np.asarray(F[k], dtype=np.float32)
    meta = pd.DataFrame({"TransactionID": t.TransactionID.values, "month": pd.to_datetime(t.ts).dt.month.values,
                         "risk_score": t.risk_score.values})
    del F, t
    gc.collect()
    V = pd.read_csv(os.path.join(raw, "transactions.csv"), usecols=["TransactionID"] + VCOLS,
                    dtype={c: "float32" for c in VCOLS}).sort_values("TransactionID")
    X[:, len(names):] = V[VCOLS].values
    del V
    gc.collect()
    return X, y, meta, names + VCOLS


def model():
    return HistGradientBoostingClassifier(max_iter=400, learning_rate=0.06, max_leaf_nodes=63,
                                          min_samples_leaf=40, l2_regularization=1.0, random_state=7)


def main(raw: str, out: str, neg_rate: float = 0.35) -> None:
    t0 = time.time()
    X, y, meta, feats = build_matrix(raw)
    mo = meta.month.values
    rng = np.random.default_rng(7)
    report = {}
    # 1) honest time-split validation
    tr = (mo <= 9) & ((y == 1) | (rng.random(len(y)) < neg_rate))
    w = np.where(y[tr] == 1, 1.0, 1.0 / neg_rate)
    clf = model().fit(X[tr], y[tr], sample_weight=w)
    va = mo == 10
    p = np.concatenate([clf.predict_proba(X[va][i:i + 50000])[:, 1] for i in range(0, va.sum(), 50000)])
    report["oct_holdout"] = {"model_auc": roc_auc_score(y[va], p), "model_ap": average_precision_score(y[va], p),
                             "bank_auc": roc_auc_score(y[va], meta.risk_score.values[va]),
                             "bank_ap": average_precision_score(y[va], meta.risk_score.values[va])}
    bins = [0, .05, .15, .3, .5, .7, .85, 1.01]
    report["oct_calibration"] = [{"bin": f"{lo}-{hi}", "n": int(((p >= lo) & (p < hi)).sum()),
                                  "fraud_rate": float(y[va][(p >= lo) & (p < hi)].mean()) if ((p >= lo) & (p < hi)).any() else None}
                                 for lo, hi in zip(bins[:-1], bins[1:])]
    print(json.dumps(report, indent=1))
    # 2) refit on all labelled months, score everything
    tr = (mo <= 10) & ((y == 1) | (rng.random(len(y)) < neg_rate))
    w = np.where(y[tr] == 1, 1.0, 1.0 / neg_rate)
    clf = model().fit(X[tr], y[tr], sample_weight=w)
    scores = np.concatenate([clf.predict_proba(X[i:i + 50000])[:, 1] for i in range(0, len(X), 50000)])
    os.makedirs(out, exist_ok=True)
    pd.DataFrame({"TransactionID": meta.TransactionID, "model_score": scores.round(5)}).to_csv(
        os.path.join(out, "model_scores.csv.gz"), index=False, compression="gzip")
    with open(os.path.join(out, "model_report.json"), "w") as fh:
        json.dump(report, fh, indent=1)
    print(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "data", "prepared"))
    a = ap.parse_args()
    main(a.raw, a.out)
