"""The 20 benchmark alerts give exactly the recorded decisions (tests/golden/benchmark.json), and the committed
answer files pass the answer checker. Runs in a fresh process on the full dataset, so it is marked slow."""
import json
import os
import subprocess
import sys

import pytest

from tests.conftest import ROOT


@pytest.mark.slow
def test_benchmark_decisions_unchanged(tmp_path):
    env = {**os.environ, "DATA_SOURCE": "demo", "AGENT_CASES_DIR": str(tmp_path / "cases"),
           "ACCESS_DIR": str(tmp_path / "a"), "LIVE_DIR": str(tmp_path / "l")}
    env.pop("REPLY_MODE", None)
    r = subprocess.run([sys.executable, str(ROOT / "tests/benchmark_run.py")], cwd=ROOT, env=env, capture_output=True,
                       text=True, timeout=900)
    assert r.returncode == 0, r.stderr[-2000:]
    got = json.loads(r.stdout.strip().splitlines()[-1])
    want = json.loads((ROOT / "tests/golden/benchmark.json").read_text(encoding="utf-8"))
    changed = {k: [f for f in want[k] if want[k][f] != got[k][f]] for k in want if want[k] != got.get(k)}
    assert not changed, f"decisions changed: {changed}. If intended, regenerate tests/golden/benchmark.json and say why."


@pytest.mark.slow
def test_committed_answers_pass_the_checker():
    r = subprocess.run([sys.executable, "validate_answers.py"], cwd=ROOT, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0 and "OK" in r.stdout, r.stdout[-2000:] + r.stderr[-1000:]
