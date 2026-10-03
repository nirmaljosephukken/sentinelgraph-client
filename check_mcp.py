"""Quick check that the agent reaches TigerGraph through the TigerGraph MCP server.

    python check_mcp.py
"""
import traceback

from agent.config import SETTINGS
from agent.backends import get_backend

g = get_backend(SETTINGS)
print("backend:", g.name)
print("MCP connected:", getattr(g, "mcp", None) is not None)
if getattr(g, "mcp", None) is not None:
    print("MCP tools:", len(g.mcp.tools))
try:
    ctx = g.txn_context("3514030")
    print("test query ok:", bool(ctx))
except Exception:
    traceback.print_exc()
print("transport log:")
for line in getattr(g, "transport_log", []):
    print("  ", line)
