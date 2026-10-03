"""
Run the agent on the case pack and write one answer file per case to cases/<case_id>.json plus the full
investigation trace to traces/<case_id>.json.

    python run_cases.py                    # all 20 cases, TigerGraph backend (from .env)
    python run_cases.py ALR-006 ALR-014    # selected cases
    python run_cases.py --backend local    # offline mirror (no graph writes)
    python run_cases.py --no-llm           # deterministic templates only
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time

from agent.config import SETTINGS
from agent.backends import get_backend
from agent.llm import LLM
from agent.orchestrator import Investigation


def load_pack() -> list[dict]:
    with open(SETTINGS.prepared_dir / "case_pack.csv", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cases", nargs="*")
    ap.add_argument("--backend", choices=["tigergraph", "local"])
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--no-investigator", action="store_true", help="skip the LLM tool-calling investigator step")
    a = ap.parse_args()
    if a.backend:
        SETTINGS.backend = a.backend
    if a.no_llm:
        SETTINGS.llm_provider = "none"
    pack = load_pack()
    if a.cases:
        pack = [c for c in pack if c["case_id"] in set(a.cases)]
    backend = get_backend(SETTINGS)
    print(f"backend={backend.name} llm={SETTINGS.llm_provider} cases={len(pack)}", flush=True)
    SETTINGS.cases_dir.mkdir(exist_ok=True)
    SETTINGS.traces_dir.mkdir(exist_ok=True)
    rows = []
    for c in sorted(pack, key=lambda r: r["opened_at"]):   # chronological, so memory accumulates in time order
        llm = LLM(SETTINGS)
        t0 = time.time()
        inv = Investigation(backend, llm, c, use_llm_investigator=not a.no_investigator, replies="simulate")
        ans = inv.run()
        (SETTINGS.cases_dir / f"{c['case_id']}.json").write_text(json.dumps(ans, indent=2), encoding="utf-8")
        trace = {"case": c, "events": inv.events, "transport": getattr(backend, "transport_log", [])[-60:],
                 "llm": {"provider": llm.provider, "model": llm.model, "calls": llm.calls, "errors": llm.errors}}
        (SETTINGS.traces_dir / f"{c['case_id']}.json").write_text(json.dumps(trace, indent=1, default=str), encoding="utf-8")
        cs = ans["case"]
        fin = ",".join(x["action"] for x in ans["next_best_actions"]["final"])
        print(f"{c['case_id']} {cs['verdict']:<10} p={cs['fraud_probability']:.2f} {cs['pattern']:<28} "
              f"exp=${cs['exposure_usd']:>9,.2f} sar={ans['sar']['file']!s:<5} graph={cs['written_to_graph']} "
              f"[{fin}] {time.time() - t0:.1f}s", flush=True)
        rows.append(ans)
    return 0


if __name__ == "__main__":
    sys.exit(main())
