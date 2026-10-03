"""
Sign-in for the console. AUTH_MODE picks how the person is identified:

    oidc  Streamlit's built-in OpenID Connect sign-in (Google by default; any OIDC provider such as Okta,
          Azure AD / Entra ID or Auth0 by changing AUTH_METADATA_URL). Configured in .streamlit/secrets.toml,
          which deploy/entrypoint.sh writes from environment variables at container start.
    iap   Google Cloud Identity-Aware Proxy in front of Cloud Run. The signed IAP header is verified when
          IAP_AUDIENCE is set.
    dev   local development only: pick a demo user. Refused when running on Cloud Run unless
          ALLOW_DEV_AUTH=1, so a production deployment can never fall back to it silently.

The role comes from the user directory in agent/access.py, never from the identity provider's claims.
"""
from __future__ import annotations

import html
import os

import streamlit as st

from agent import access as A

MODE = os.getenv("AUTH_MODE", "dev").lower()
ON_CLOUD_RUN = bool(os.getenv("K_SERVICE"))
DEV_USERS = [("admin@demo.bank", "Asha (admin)"), ("lead@demo.bank", "Ravi (team lead)"),
             ("manager@demo.bank", "Meera (fraud manager)"), ("analyst@demo.bank", "Kiran (analyst)"),
             ("viewer@demo.bank", "Vik (viewer)")]
DEV_ROLES = {"admin@demo.bank": "admin", "lead@demo.bank": "team_lead", "manager@demo.bank": "fraud_manager",
             "analyst@demo.bank": "analyst", "viewer@demo.bank": "viewer"}


def _e(x) -> str:
    return html.escape(str(x if x is not None else ""))


def _iap_identity() -> tuple[str, str] | None:
    h = st.context.headers
    email = (h.get("X-Goog-Authenticated-User-Email") or "").split(":")[-1]
    aud = os.getenv("IAP_AUDIENCE")
    if aud:  # verify the signed assertion instead of trusting a plain header
        from google.auth.transport import requests as greq
        from google.oauth2 import id_token
        token = h.get("X-Goog-Iap-Jwt-Assertion") or ""
        try:
            claims = id_token.verify_token(token, greq.Request(), audience=aud,
                                           certs_url="https://www.gstatic.com/iap/verify/public_key")
            email = claims.get("email", "")
        except Exception:  # noqa: BLE001
            return None
    return (email, email) if email else None


def _login_screen(message: str = "") -> None:
    st.html("<div style='max-width:560px;margin:12vh auto 0;text-align:center'>"
            "<div class='eyebrow'>SentinelGraph · fraud investigation</div>"
            "<h1 class='page' style='margin-top:10px'>Sign in</h1>"
            "<div class='sub' style='margin:0 auto 22px'>Access is limited to your organisation's fraud team. Your role "
            "decides what you can see and approve, and every decision you make is recorded under your name.</div>"
            + (f"<div class='ev neutral' style='text-align:left;margin-bottom:16px'>{_e(message)}</div>" if message else "")
            + "</div>")


def current_user() -> dict | None:
    """Returns {"email", "name", "role"} for the signed-in person, or renders the sign-in screen and returns None."""
    if MODE == "oidc":
        if not st.user.is_logged_in:
            _login_screen()
            _, mid, _ = st.columns([1.3, 1, 1.3])
            if mid.button("Sign in with your work account", type="primary", use_container_width=True):
                st.login()
            return None
        return A.resolve_user(st.user.get("email", ""), st.user.get("name", ""))
    if MODE == "iap":
        ident = _iap_identity()
        if not ident:
            _login_screen("Identity-Aware Proxy did not provide a verified identity. Check the IAP configuration.")
            return None
        return A.resolve_user(*ident)
    # dev
    if ON_CLOUD_RUN and os.getenv("ALLOW_DEV_AUTH") != "1":
        _login_screen("This deployment has no sign-in configured. Set AUTH_MODE=oidc (or iap) and redeploy.")
        return None
    with st.sidebar:
        st.html("<div class='chip c-warn' style='margin:4px 0 6px'>Development sign-in · not for production</div>")
        labels = [lbl for _, lbl in DEV_USERS]
        pick = st.selectbox("Act as", labels, index=st.session_state.get("_dev_pick", 0), key="dev_user")
        st.session_state["_dev_pick"] = labels.index(pick)
    email = DEV_USERS[labels.index(pick)][0]
    u = A.resolve_user(email, pick.split(" (")[0])
    if u["role"] != DEV_ROLES[email]:  # demo accounts keep their demo roles
        A.store().put_user(email, {"role": DEV_ROLES[email]})
        u["role"] = DEV_ROLES[email]
    return u


def sign_out_button() -> None:
    if MODE == "oidc" and st.button("Sign out", use_container_width=True):
        st.logout()


def user_card(u: dict) -> str:
    return (f"<div class='side-status' style='margin-top:0;border-top:0'><div class='eyebrow'>Signed in</div>"
            f"<div style='color:var(--ink);font-weight:600;margin-top:4px'>{_e(u['name'])}</div>"
            f"<div class='mono' style='font-size:11.5px;color:var(--mute)'>{_e(u['email'])}</div>"
            f"<div style='margin-top:6px'><span class='chip'>{_e(A.ROLE_LABEL.get(u['role'], u['role']))}</span></div></div>")


def denied(permission: str) -> None:
    need = A.ROLE_LABEL[A.PERMISSIONS[permission]]
    st.html(f"<div class='empty'>This needs the <b>{_e(need)}</b> role or above. Ask an administrator to change your "
            "role on the Team &amp; access page.</div>")
