"""Scenario lab: the engine finds the planted pattern on synthetic banks it has never seen."""
import pytest

from agent import scenarios as SC
from agent.config import SETTINGS
from agent.llm import LLM
from agent.orchestrator import Investigation


@pytest.mark.parametrize("kind", list(SC.SCENARIOS))
@pytest.mark.parametrize("seed", [1, 7])
def test_planted_verdict_is_found(kind, seed):
    sc = SC.generate(kind, seed=seed)
    ans = Investigation(sc.backend, LLM(SETTINGS), sc.alert, use_llm_investigator=False, replies="simulate").run()
    assert ans["case"]["verdict"] == sc.expect
