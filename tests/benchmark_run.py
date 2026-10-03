"""Run the 20 benchmark alerts offline and print the decisions as JSON (used by tests/test_benchmark.py)."""
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent.config import SETTINGS  # noqa: E402

SETTINGS.backend, SETTINGS.llm_provider = "local", "none"
from agent.backends import get_backend  # noqa: E402
from agent.llm import LLM  # noqa: E402
from agent.orchestrator import Investigation  # noqa: E402

pack = list(csv.DictReader(open(SETTINGS.prepared_dir / "case_pack.csv", encoding="utf-8")))
g, out = get_backend(SETTINGS), {}
for c in sorted(pack, key=lambda r: r["opened_at"]):
    a = Investigation(g, LLM(SETTINGS), c, use_llm_investigator=False, replies="simulate").run()
    out[c["case_id"]] = {"verdict": a["case"]["verdict"], "fraud_probability": a["case"]["fraud_probability"],
                         "pattern": a["case"]["pattern"], "exposure_usd": a["case"]["exposure_usd"],
                         "initial": [[x["action"], x["route"], x["reason"]] for x in a["next_best_actions"]["initial"]],
                         "final": [[x["action"], x["route"], x["reason"]] for x in a["next_best_actions"]["final"]],
                         "sar": a["sar"]["file"], "sar_reason": a["sar"]["reason"]}
print(json.dumps(out, sort_keys=True))
