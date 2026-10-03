"""Console smoke tests (Streamlit AppTest) on client data: every page renders for the roles that may see it."""
import pytest
from streamlit.testing.v1 import AppTest

from tests.conftest import ROOT


def _app():
    import os
    import runpy

    import streamlit as st
    os.chdir(os.environ["SG_ROOT"])
    st.navigation = lambda pages, **k: type("N", (), {"run": lambda self: None})()
    g = runpy.run_path("ui/app.py")
    g[st.session_state["_pg"]]()


def run(page, who):
    at = AppTest.from_function(_app, default_timeout=120)
    at.session_state["_pg"] = page
    at.run()
    [s for s in at.selectbox if s.label == "Act as"][0].set_value(who).run()
    return at


def html(at):
    return " ".join(h.proto.body for h in at.get("html"))


PAGES = ["page_investigate", "page_live", "page_outcomes", "page_policies", "page_audit", "page_activity", "page_intake", "page_data",
         "page_team", "page_lab"]


@pytest.mark.slow
@pytest.mark.parametrize("page", PAGES)
def test_pages_render_for_admin(page, example_bank, client_mode, monkeypatch):
    monkeypatch.setenv("SG_ROOT", str(ROOT))
    at = run(page, "Asha (admin)")
    assert not at.exception, [str(x.message)[:300] for x in at.exception]


@pytest.mark.slow
@pytest.mark.parametrize("page, permission_text", [("page_data", "needs the"), ("page_team", "needs the"),
                                                    ("page_lab", "needs the")])
def test_viewer_is_kept_out(page, permission_text, example_bank, client_mode, monkeypatch):
    monkeypatch.setenv("SG_ROOT", str(ROOT))
    at = run(page, "Vik (viewer)")
    assert not at.exception and permission_text in html(at)


@pytest.mark.slow
def test_policies_page_shows_the_active_policy(example_bank, client_mode, monkeypatch):
    from agent.policy_config import POLICY
    monkeypatch.setenv("SG_ROOT", str(ROOT))
    body = html(run("page_policies", "Vik (viewer)"))
    assert POLICY.fingerprint in body and "Decision thresholds" in body
