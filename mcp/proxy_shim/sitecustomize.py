"""Optional shim for sandboxed networks: makes aiohttp (used by pyTigerGraph inside tigergraph-mcp) honour
HTTPS_PROXY / SSL_CERT_FILE. Enabled only when TG_MCP_TRUST_ENV=1 (agent.mcp_bridge adds this folder to
PYTHONPATH). Not needed on a normal workstation."""
import os

if os.getenv("TG_MCP_TRUST_ENV") == "1":
    try:
        import aiohttp

        _orig = aiohttp.ClientSession.__init__

        def _init(self, *a, **kw):
            kw.setdefault("trust_env", True)
            return _orig(self, *a, **kw)

        aiohttp.ClientSession.__init__ = _init
    except Exception:
        pass
