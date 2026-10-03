#!/bin/sh
# Container start for the console. With AUTH_MODE=oidc, write Streamlit's sign-in settings from environment
# variables (the client secret and cookie secret arrive from Secret Manager), then start the console.
set -e
if [ "$AUTH_MODE" = "oidc" ]; then
  mkdir -p .streamlit
  python - <<'PY'
import json, os
need = ["AUTH_REDIRECT_URI", "AUTH_COOKIE_SECRET", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET"]
missing = [k for k in need if not os.environ.get(k)]
if missing:
    raise SystemExit(f"AUTH_MODE=oidc but {', '.join(missing)} not set")
q = json.dumps
conf = ["[auth]",
        f"redirect_uri = {q(os.environ['AUTH_REDIRECT_URI'])}",
        f"cookie_secret = {q(os.environ['AUTH_COOKIE_SECRET'])}",
        f"client_id = {q(os.environ['AUTH_CLIENT_ID'])}",
        f"client_secret = {q(os.environ['AUTH_CLIENT_SECRET'])}",
        f"server_metadata_url = {q(os.environ.get('AUTH_METADATA_URL', 'https://accounts.google.com/.well-known/openid-configuration'))}"]
with open(".streamlit/secrets.toml", "w") as fh:
    fh.write("\n".join(conf) + "\n")
PY
fi
exec streamlit run ui/app.py --server.port="${PORT:-8080}" --server.address=0.0.0.0 --server.headless=true \
  --browser.gatherUsageStats=false
