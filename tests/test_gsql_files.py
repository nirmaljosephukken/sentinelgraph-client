"""Every GSQL query file selects its graph first; without it TigerGraph silently ignores CREATE QUERY."""
import re

from tests.conftest import ROOT


def test_every_query_file_starts_with_use_graph():
    for f in (ROOT / "graph" / "queries").glob("*.gsql"):
        code = [ln for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.strip().startswith("//")]
        assert code and re.match(r"USE GRAPH \w+", code[0].strip()), f"{f.name} must begin with USE GRAPH"
