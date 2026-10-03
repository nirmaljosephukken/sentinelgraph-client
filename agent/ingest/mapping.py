"""
Map a client's file onto SentinelGraph's fields and check every row.

A mapping file (config/mappings/<name>.yaml) says, for each of our fields, where the value comes from:

    id: TXN_REF                                        # a column, copied as text
    ts: {column: TXN_TIME, format: "%d/%m/%Y %H:%M"}   # a time with an explicit format (or epoch_s, epoch_ms, iso)
    amount: {column: AMT_PAISE, scale: 0.01}           # a number, multiplied
    channel: {column: CHANNEL, values: {ECOM: online, POS: in_person, "*": in_person}}
    p_email: {column: EMAIL, email_domain: true}       # keep only the domain, never the mailbox
    addr1: {column: PIN_CODE, prefix: 3}               # first characters only
    device_profile: {join: [MODEL, OS, BROWSER], sep: " | "}
    network: {value: rupay}                            # a constant

Optional on any rule: default (used when the value is empty), lower / upper, strip (default true).

apply() never stops at the first problem. It returns the rows that passed, and a report listing every rejected
row with the reason, so the client can fix their export.
"""
from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yaml

from agent.ingest.schema import ALERT, TS_FMT, TXN, Field

ROOT = Path(__file__).resolve().parent.parent.parent
MAPPINGS_DIR = Path(os.getenv("MAPPINGS_DIR", ROOT / "config" / "mappings"))
RULE_KEYS = {"column", "join", "sep", "value", "format", "scale", "values", "email_domain", "prefix", "default",
             "lower", "upper", "strip", "dayfirst"}
MAX_ERRORS = 500


class MappingError(ValueError):
    """The mapping or the file's columns are wrong; nothing was imported."""


@dataclass
class Mapping:
    name: str
    title: str
    description: str
    transactions: dict
    alerts: dict
    timezone: str = ""
    max_rejected_share: float = 0.05
    id_patterns: list = field(default_factory=list)
    source: str = ""

    def columns(self, which: str = "transactions") -> set[str]:
        out = set()
        for rule in (self.transactions if which == "transactions" else self.alerts).values():
            r = _rule(rule)
            out |= set(r.get("join") or ([r["column"]] if r.get("column") else []))
        return out


@dataclass
class Report:
    kind: str
    mapping: str
    rows_in: int = 0
    rows_ok: int = 0
    rejected: list = field(default_factory=list)       # [{row, field, value, problem}]
    warnings: list = field(default_factory=list)
    coverage: dict = field(default_factory=dict)       # field -> share of accepted rows with a value
    refused: str = ""                                  # set when the whole batch is refused
    flagged_values: dict = field(default_factory=dict)  # field -> values that read like instructions

    @property
    def n_rejected(self) -> int:
        return len({e["row"] for e in self.rejected})

    def problems(self) -> list[tuple[str, int]]:
        return Counter(f"{e['field']}: {e['problem']}" for e in self.rejected).most_common()

    def detectors(self) -> dict[str, bool]:
        c = self.coverage
        has = lambda *fs: all(c.get(f, 0) > 0 for f in fs)  # noqa: E731  (sparse fields such as proxy still count)
        return {"Card history, velocity and card testing": has("id", "ts", "amount", "card_id"),
                "Recurring charges": has("product"),
                "Out-of-region use": has("addr1"),
                "Device sharing and device rings": has("device_profile"),
                "New-device evidence": has("device_status"),
                "Anonymous proxy": has("proxy"),
                "Email links between cards": has("p_email"),
                "Case-memory model prior": has("model_score")}

    def to_dict(self) -> dict:
        return {"kind": self.kind, "mapping": self.mapping, "rows_in": self.rows_in, "rows_ok": self.rows_ok,
                "rows_rejected": self.n_rejected, "refused": self.refused,
                "problems": [{"problem": p, "rows": n} for p, n in self.problems()],
                "rejected_rows": self.rejected[:MAX_ERRORS], "warnings": self.warnings,
                "coverage": {k: round(v, 3) for k, v in self.coverage.items()}, "detectors": self.detectors(),
                "flagged_values": self.flagged_values}


# ---------------------------------------------------------------------------------------------- loading
def _rule(rule) -> dict:
    if isinstance(rule, str):
        return {"column": rule}
    if isinstance(rule, dict):
        return rule
    raise MappingError(f"a field rule must be a column name or a mapping, got {rule!r}")


def _check_rules(rules: dict, fields: dict[str, Field], where: str) -> None:
    if not isinstance(rules, dict):
        raise MappingError(f"{where} must map field names to rules")
    unknown = [k for k in rules if k not in fields]
    if unknown:
        raise MappingError(f"{where}: unknown field(s) {', '.join(unknown)}. Known: {', '.join(fields)}")
    for k, rule in rules.items():
        r = _rule(rule)
        bad = set(r) - RULE_KEYS
        if bad:
            raise MappingError(f"{where}.{k}: unknown option(s) {', '.join(sorted(bad))}")
        if sum(1 for x in ("column", "join", "value") if x in r) != 1:
            raise MappingError(f"{where}.{k}: give exactly one of column, join or value")
    missing = [f.name for f in fields.values() if f.required and f.name not in rules]
    if missing:
        raise MappingError(f"{where}: required field(s) not mapped: {', '.join(missing)}")


def parse(raw: dict, name: str, source: str = "") -> Mapping:
    if not isinstance(raw, dict) or "transactions" not in raw:
        raise MappingError("a mapping needs a 'transactions' section")
    _check_rules(raw["transactions"], TXN, "transactions")
    alerts = raw.get("alerts") or {}
    if alerts:
        _check_rules(alerts, ALERT, "alerts")
    pats = raw.get("id_patterns") or []
    for p in pats:
        try:
            re.compile(p)
        except re.error as ex:
            raise MappingError(f"id_patterns: {p!r} is not a valid regular expression ({ex})") from ex
    share = float(raw.get("max_rejected_share", 0.05))
    if not 0 <= share <= 1:
        raise MappingError("max_rejected_share must be between 0 and 1")
    return Mapping(name=name, title=str(raw.get("title") or name), description=str(raw.get("description") or ""),
                   transactions=raw["transactions"], alerts=alerts, timezone=str(raw.get("timezone") or ""),
                   max_rejected_share=share, id_patterns=list(pats), source=source)


def load(name: str) -> Mapping:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name or ""):
        raise MappingError(f"invalid mapping name {name!r}")
    p = MAPPINGS_DIR / f"{name}.yaml"
    if not p.exists():
        raise MappingError(f"no mapping called {name!r} (looked for {p.name} in config/mappings)")
    try:
        return parse(yaml.safe_load(p.read_text(encoding="utf-8")), name, str(p))
    except yaml.YAMLError as ex:
        raise MappingError(f"{p.name} is not valid YAML: {ex}") from ex


def client_id_patterns() -> list[str]:
    """id_patterns from every mapping, for the language-model guardrail (agent/llm.py)."""
    out = []
    for name in available():
        try:
            out += load(name).id_patterns
        except MappingError:
            pass
    return out


def available() -> list[str]:
    return sorted(p.stem for p in MAPPINGS_DIR.glob("*.yaml")) if MAPPINGS_DIR.exists() else []


# ---------------------------------------------------------------------------------------------- applying
def _blank(s: pd.Series) -> pd.Series:
    return s.isna() | (s.astype(str).str.strip().isin(["", "nan", "NaN", "None", "NULL", "null", "<NA>"]))


def _source(df: pd.DataFrame, r: dict) -> pd.Series:
    if "value" in r:
        return pd.Series([r["value"]] * len(df), index=df.index, dtype=object)
    if "join" in r:
        parts = [df[c].where(~_blank(df[c]), "").astype(str).str.strip() for c in r["join"]]
        joined = parts[0]
        for p in parts[1:]:
            joined = joined + r.get("sep", " | ") + p
        allblank = pd.concat([(p == "") for p in parts], axis=1).all(axis=1)
        return joined.where(~allblank, None)
    return df[r["column"]]


def _text(s: pd.Series, r: dict) -> pd.Series:
    blank = _blank(s)
    t = s.astype(str)
    t = t.str.replace(r"\.0$", "", regex=True) if s.dtype.kind == "f" else t
    from agent.safety import clean
    t = t.map(lambda v: clean(v, 300))          # invisible and control characters out, length capped
    if r.get("strip", True):
        t = t.str.strip()
    if r.get("email_domain"):
        t = t.str.split("@").str[-1]
    if r.get("prefix"):
        t = t.str[: int(r["prefix"])]
    if r.get("lower"):
        t = t.str.lower()
    if r.get("upper"):
        t = t.str.upper()
    if "values" in r:
        vals = {str(k): v for k, v in r["values"].items()}
        star = vals.get("*")
        t = t.map(lambda v: vals.get(v, star if star is not None else v))
    return t.where(~blank, None)


def _times(s: pd.Series, r: dict, tz: str) -> pd.Series:
    fmt = r.get("format")
    blank = _blank(s)
    if fmt in ("epoch_s", "epoch_ms"):
        n = pd.to_numeric(s, errors="coerce")
        out = pd.to_datetime(n, unit="s" if fmt == "epoch_s" else "ms", errors="coerce", utc=True)
    elif fmt and fmt != "iso":
        out = pd.to_datetime(s.astype(str).str.strip(), format=fmt, errors="coerce")
    else:
        out = pd.to_datetime(s.astype(str).str.strip(), errors="coerce", dayfirst=bool(r.get("dayfirst")),
                             format="mixed" if not fmt else "ISO8601")
    if getattr(out.dt, "tz", None) is not None:
        out = out.dt.tz_convert("UTC").dt.tz_localize(None)
    elif tz:
        out = out.dt.tz_localize(tz, ambiguous="NaT", nonexistent="NaT").dt.tz_convert("UTC").dt.tz_localize(None)
    return out.where(~blank)


def apply(df: pd.DataFrame, m: Mapping, kind: str = "transactions",
          now: datetime | None = None) -> tuple[pd.DataFrame, Report]:
    """Map and check a client frame. Returns (accepted rows in SentinelGraph fields, report)."""
    rules = m.transactions if kind == "transactions" else m.alerts
    fields = TXN if kind == "transactions" else ALERT
    if not rules:
        raise MappingError(f"mapping {m.name!r} has no {kind} section")
    rep = Report(kind=kind, mapping=m.name, rows_in=len(df))
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    missing = sorted(c for c in m.columns(kind) if c not in df.columns)
    req_cols = set()
    for k, rule in rules.items():
        r = _rule(rule)
        if fields[k].required:
            req_cols |= set(r.get("join") or ([r["column"]] if r.get("column") else []))
    if missing and set(missing) & req_cols:
        raise MappingError(f"the file is missing column(s) needed for required fields: "
                           f"{', '.join(sorted(set(missing) & req_cols))}. Columns found: {', '.join(df.columns[:40])}")
    for c in missing:
        rep.warnings.append(f"column {c!r} is not in the file; the field that uses it is left empty")

    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    bad = pd.Series(False, index=df.index)
    out = pd.DataFrame(index=df.index)

    def reject(mask: pd.Series, fname: str, values: pd.Series, problem: str):
        nonlocal bad
        for i in mask[mask].index[: MAX_ERRORS * 2]:
            v = values.get(i)
            rep.rejected.append({"row": int(i) + 2, "field": fname, "value": None if pd.isna(v) else str(v)[:80],
                                 "problem": problem})
        bad = bad | mask

    for f in fields.values():
        if f.name not in rules:
            out[f.name] = None
            continue
        r = _rule(rules[f.name])
        cols = r.get("join") or ([r["column"]] if r.get("column") else [])
        if any(c not in df.columns for c in cols):
            out[f.name] = None
            continue
        raw = _source(df, r)
        if f.kind == "time":
            v = _times(raw, r, m.timezone)
            unparsed = v.isna() & ~_blank(raw)
            if f.required:
                reject(_blank(raw), f.name, raw, "missing")
            reject(unparsed, f.name, raw, f"not a time{' in format ' + r['format'] if r.get('format') else ''}")
            future = v > (now + timedelta(days=1))
            reject(future.fillna(False), f.name, raw, "more than a day in the future")
            out[f.name] = v.dt.strftime(TS_FMT).where(v.notna(), None)
        elif f.kind == "number":
            num = pd.to_numeric(raw.astype(str).str.replace(",", "", regex=False).str.strip()
                                .where(~_blank(raw), None), errors="coerce")
            if r.get("scale") is not None:
                num = num * float(r["scale"])
            unparsed = num.isna() & ~_blank(raw)
            if f.required:
                reject(_blank(raw), f.name, raw, "missing")
                reject(unparsed, f.name, raw, "not a number")
            elif unparsed.any():
                rep.warnings.append(f"{f.name}: {int(unparsed.sum())} value(s) are not numbers and were left empty")
            if f.name in ("risk_score", "model_score"):
                off = num.notna() & ((num < 0) | (num > 1))
                if off.any():
                    hint = " (they look like percentages: add scale: 0.01)" if num[off].between(0, 100).all() else ""
                    rep.warnings.append(f"{f.name}: {int(off.sum())} value(s) outside 0 to 1 were left empty{hint}")
                    num = num.where(~off)
            if f.name == "amount":
                neg = num < 0
                if neg.any():
                    rep.warnings.append(f"amount: {int(neg.sum())} negative value(s) kept (refunds or reversals)")
            if "default" in r:
                num = num.fillna(float(r["default"]))
            out[f.name] = num.round(4) if f.name != "amount" else num.round(2)
        else:
            t = _text(raw, r)
            if "default" in r:
                t = t.where(t.notna() & (t != ""), str(r["default"]))
            if f.required:
                reject(t.isna() | (t == ""), f.name, raw, "missing")
            if f.choices:
                odd = t.notna() & ~t.isin(f.choices)
                if odd.any():
                    seen = ", ".join(sorted(set(map(str, t[odd].unique())))[:6])
                    rep.warnings.append(f"{f.name}: {int(odd.sum())} value(s) not in {list(c for c in f.choices if c)} "
                                        f"({seen}) were left empty; add a values: map to the mapping")
                    t = t.where(~odd, None)
            out[f.name] = t

    # duplicates inside the file: keep the first
    if "id" in out or "case_id" in out:
        key = "id" if kind == "transactions" else "case_id"
        dup = out[key].notna() & out[key].duplicated(keep="first")
        reject(dup, key, out[key], "duplicate id in this file (first one kept)")

    if kind == "transactions":
        out["customer_id"] = out["customer_id"].where(out["customer_id"].notna(), out["card_id"])
        out["channel"] = out["channel"].where(out["channel"].notna(), "in_person")
        region = out["addr1"].where(out["addr1"].notna(), "NA").astype(str)
        out["client_id"] = out["client_id"].where(out["client_id"].notna(), out["card_id"].astype(str) + "|" + region + "|0")
        for c in ("device_profile", "p_email", "r_email", "device_status", "proxy", "device_type", "product",
                  "addr1", "addr2", "network", "card_type", "m4", "m6"):
            out[c] = out[c].where(out[c].notna(), "")
    elif kind == "alerts":
        out["trigger_type"] = out["trigger_type"].where(out["trigger_type"].notna(), "risk_score")

    ok = out[~bad].reset_index(drop=True)
    rep.rows_ok = len(ok)
    # text a fraudster controls (device strings, product names, alert texts) is checked for instructions to the agent
    from agent.safety import check
    for col in ("device_profile", "product", "network", "card_type"):   # alert texts are checked one by one in load.py
        if col in ok and len(ok):
            vals = [v for v in ok[col].dropna().astype(str).unique()[:20000] if v]
            bad_vals = [v for v in vals if check(v)["flagged"]]
            if bad_vals:
                rep.warnings.append(f"{col}: {len(bad_vals)} value(s) read like instructions to the agent and are treated "
                                    f"as plain data (for example {bad_vals[0][:60]!r}); they are flagged in the audit log")
                rep.flagged_values[col] = bad_vals[:20]
    rep.coverage = {f: float(((ok[f].notna()) & (ok[f].astype(str) != "")).mean()) if len(ok) else 0.0
                    for f in fields}
    if rep.rows_in and rep.n_rejected / rep.rows_in > m.max_rejected_share:
        rep.refused = (f"{rep.n_rejected} of {rep.rows_in} rows failed the checks, more than the "
                       f"{m.max_rejected_share:.0%} this mapping allows. Nothing was imported; fix the rows listed "
                       f"or the mapping and send the file again.")
    return ok, rep


def read_file(data: bytes, filename: str = "upload.csv") -> pd.DataFrame:
    """CSV, TSV, JSON lines, JSON array or Excel into a frame of text columns (no type guessing)."""
    import io
    name = filename.lower()
    buf = io.BytesIO(data)
    if name.endswith((".xlsx", ".xls")):
        return pd.read_excel(buf, dtype=str)
    if name.endswith((".jsonl", ".ndjson")):
        return pd.read_json(buf, lines=True, dtype=False).astype(object)
    if name.endswith(".json"):
        return pd.read_json(buf, dtype=False).astype(object)
    sep = "\t" if name.endswith((".tsv", ".tab")) else None
    return pd.read_csv(buf, dtype=str, sep=sep, engine="python" if sep is None else "c", keep_default_na=False,
                       compression="gzip" if name.endswith(".gz") else None)
