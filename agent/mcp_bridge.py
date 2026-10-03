"""
TigerGraph MCP bridge.

The agent reaches TigerGraph through the official TigerGraph MCP server
(https://github.com/tigergraph/tigergraph-mcp): we launch `tigergraph-mcp` over stdio, discover its tools
with `list_tools`, and call `tigergraph__run_installed_query` for every investigation query. Argument names
are read from the tool's JSON schema at runtime, so the bridge adapts to server versions.

If the MCP server is not installed or a call fails, the caller transparently falls back to direct RESTPP
(agent.tg_client). Every call records which transport served it, so the trace shows MCP usage honestly.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from typing import Any

from agent.config import SETTINGS, Settings


class MCPUnavailable(RuntimeError):
    pass


class TigerGraphMCP:
    def __init__(self, s: Settings = SETTINGS, token: str | None = None):
        try:
            from mcp import ClientSession, StdioServerParameters  # noqa: F401
            from mcp.client.stdio import stdio_client  # noqa: F401
        except Exception as e:  # pragma: no cover
            raise MCPUnavailable(f"python package `mcp` missing: {e}")
        self.s = s
        self.token = token
        self.tools: dict[str, dict] = {}
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._session = None
        self._ctx = None
        self._run(self._start(), timeout=90)

    # ----------------------------------------------------------------- plumbing
    def _run(self, coro, timeout: float = 300):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def _env(self) -> dict:
        env = dict(os.environ)
        host = self.s.tg_host
        env.update({"TG_HOST": host, "TG_GRAPHNAME": self.s.tg_graph})
        if "tgcloud.io" in host:
            env.update({"TG_TGCLOUD": "true", "TG_RESTPP_PORT": "443", "TG_GS_PORT": "443", "TG_SSL_PORT": "443"})
        if self.s.tg_secret:
            env["TG_SECRET"] = self.s.tg_secret
        if self.token:
            env["TG_JWT_TOKEN"] = self.token  # JWT from /gsql/v1/tokens
        if self.s.tg_username:
            env.update({"TG_USERNAME": self.s.tg_username, "TG_PASSWORD": self.s.tg_password})
        if os.getenv("TG_MCP_TRUST_ENV") == "1":  # sandboxed networks: route the MCP server through HTTPS_PROXY
            shim = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mcp", "proxy_shim")
            env["PYTHONPATH"] = shim + os.pathsep + env.get("PYTHONPATH", "")
            ca = env.get("SSL_CERT_FILE") or env.get("REQUESTS_CA_BUNDLE") or env.get("PIP_CERT")
            if ca:
                env["SSL_CERT_FILE"] = ca
        return env

    async def _start(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        params = StdioServerParameters(command=self.s.mcp_command, args=[], env=self._env())
        self._ctx = stdio_client(params)
        read, write = await self._ctx.__aenter__()
        self._session_ctx = ClientSession(read, write)
        self._session = await self._session_ctx.__aenter__()
        await self._session.initialize()
        listed = await self._session.list_tools()
        self.tools = {t.name: (t.inputSchema or {}) for t in listed.tools}
        if not any("installed_query" in n for n in self.tools):
            raise MCPUnavailable(f"MCP server exposes no installed-query tool: {sorted(self.tools)}")

    async def _call(self, name: str, args: dict) -> Any:
        res = await self._session.call_tool(name, args)
        texts = [c.text for c in res.content if getattr(c, "type", "") == "text"]
        if getattr(res, "isError", False):
            raise RuntimeError(" ".join(texts)[:500])
        joined = "\n".join(texts)
        try:
            return json.loads(joined)
        except json.JSONDecodeError:
            start = joined.find("[") if joined.find("[") >= 0 else joined.find("{")
            if start >= 0:
                try:
                    return json.loads(joined[start:])
                except json.JSONDecodeError:
                    pass
            return joined

    # ----------------------------------------------------------------- public
    def run_installed_query(self, query: str, params: dict) -> list[dict]:
        tool = next(n for n in self.tools if n.endswith("run_installed_query"))
        props = (self.tools[tool].get("properties") or {})
        qkey = next((k for k in props if "query" in k.lower() and "param" not in k.lower()), "query_name")
        pkey = next((k for k in props if "param" in k.lower()), "params")
        args = {qkey: query, pkey: params}
        if "graph_name" in props:
            args["graph_name"] = self.s.tg_graph
        out = self._run(self._call(tool, args))
        if isinstance(out, str) and "```json" in out:
            try:
                out = json.loads(out.split("```json", 1)[1].split("```", 1)[0])
            except (json.JSONDecodeError, IndexError):
                pass
        if isinstance(out, dict) and out.get("success") is False:
            raise RuntimeError(str(out.get("summary"))[:300])
        if isinstance(out, dict) and "data" in out and isinstance(out["data"], (list, dict)):
            out = out["data"]
        if isinstance(out, dict):
            out = out.get("results", out.get("result", [out]))
        if not isinstance(out, list):
            raise RuntimeError(f"unexpected MCP result: {str(out)[:300]}")
        return out

    def call(self, tool: str, args: dict) -> Any:
        return self._run(self._call(tool, args))

    def close(self):
        async def _close():
            try:
                await self._session_ctx.__aexit__(None, None, None)
                await self._ctx.__aexit__(None, None, None)
            except Exception:
                pass
        try:
            self._run(_close(), timeout=10)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
