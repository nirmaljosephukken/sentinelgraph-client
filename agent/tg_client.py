"""
Minimal TigerGraph REST client (TigerGraph 4.x / Savanna). Only `requests` is needed.

  * token()        - JWT from a Savanna secret (POST /gsql/v1/tokens), or basic auth
  * gsql()         - run GSQL statements (POST /gsql/v1/statements)
  * run_query()    - call an installed query (RESTPP /restpp/query/<graph>/<name>)
  * upsert()       - upsert vertices/edges (RESTPP /restpp/graph/<graph>)
"""
from __future__ import annotations

import json
import time
import urllib.parse
from typing import Any

import requests

from agent.config import SETTINGS, Settings


class TigerGraphError(RuntimeError):
    pass


class TGClient:
    def __init__(self, s: Settings = SETTINGS):
        if not s.tg_host:
            raise TigerGraphError("TG_HOST is not set (see .env.example)")
        self.s = s
        self.base = s.tg_host + (f":{s.tg_gs_port}" if s.tg_gs_port else "")
        self.restpp = (s.tg_host + f":{s.tg_restpp_port}") if s.tg_restpp_port else self.base + "/restpp"
        self.graph = s.tg_graph
        self._token = s.tg_token or None
        self.http = requests.Session()
        self.http.verify = s.tg_verify_ssl
        self.calls = 0

    # ------------------------------------------------------------------ auth
    def wait_until_awake(self, max_wait: float = 240.0, step: float = 10.0):
        """Savanna suspends idle workspaces; the first request then gets a 502 'Starting workspace' page.
        Poll until the workspace answers (it resumes on its own after a request), up to max_wait seconds."""
        import time
        t0 = time.time()
        while True:
            try:
                r = self.http.get(f"{self.base}/api/ping", timeout=20)
                starting = r.status_code in (502, 503, 504) or "Starting workspace" in r.text
            except requests.RequestException:
                starting = True
            if not starting:
                return
            if time.time() - t0 > max_wait:
                raise TigerGraphError("TigerGraph Savanna workspace is still starting after "
                                      f"{int(max_wait)}s; resume it in the Savanna console and retry")
            print(f"[tigergraph] workspace is starting, waiting {step:.0f}s ...", flush=True)
            time.sleep(step)

    def token(self) -> str | None:
        if self._token:
            return self._token
        if self.s.tg_secret:
            self.wait_until_awake()
            import time
            t0 = time.time()
            while True:  # the GSQL service can still be starting after /api/ping answers
                r = self.http.post(f"{self.base}/gsql/v1/tokens",
                                   json={"secret": self.s.tg_secret, "graph": self.graph, "lifetime": "604800"},
                                   timeout=60)
                if not (r.status_code in (502, 503, 504) or "Starting workspace" in r.text) or time.time() - t0 > 240:
                    break
                print("[tigergraph] GSQL service is starting, waiting 10s ...", flush=True)
                time.sleep(10)
            if r.status_code == 404:  # graph not created yet: global token
                r = self.http.post(f"{self.base}/gsql/v1/tokens",
                                   json={"secret": self.s.tg_secret, "lifetime": "604800"}, timeout=60)
            if r.ok and "token" in r.text:
                self._token = r.json().get("token") or r.json().get("results", {}).get("token")
            else:  # TigerGraph 3.x style fallback
                r2 = self.http.post(f"{self.restpp}/requesttoken", json={"secret": self.s.tg_secret,
                                                                         "graph": self.graph}, timeout=60)
                if not r2.ok:
                    raise TigerGraphError(f"token request failed: {r.status_code} {r.text[:300]} | {r2.text[:300]}")
                self._token = r2.json()["results"]["token"]
        elif self.s.tg_username:
            r = self.http.post(f"{self.base}/gsql/v1/tokens", json={"graph": self.graph},
                               auth=(self.s.tg_username, self.s.tg_password), timeout=60)
            if r.ok and "token" in r.text:
                self._token = r.json().get("token")
        return self._token

    def _headers(self, extra: dict | None = None) -> dict:
        h = {"GSQL-TIMEOUT": "600000"}
        tok = self.token()
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        if extra:
            h.update(extra)
        return h

    def _auth(self):
        return (self.s.tg_username, self.s.tg_password) if (self.s.tg_username and not self._token) else None

    # ------------------------------------------------------------------ gsql
    def gsql(self, statements: str, timeout: int = 1800) -> str:
        r = self.http.post(f"{self.base}/gsql/v1/statements", data=statements.encode("utf-8"),
                           headers=self._headers({"Content-Type": "text/plain"}), auth=self._auth(),
                           timeout=timeout)
        self.calls += 1
        if r.status_code >= 400:
            raise TigerGraphError(f"GSQL HTTP {r.status_code}: {r.text[:1000]}")
        return r.text

    # ------------------------------------------------------------------ queries
    def run_query(self, name: str, params: dict[str, Any] | None = None, timeout: int = 300) -> list[dict]:
        params = params or {}
        url = f"{self.restpp}/query/{self.graph}/{name}"
        has_list = any(isinstance(v, (list, tuple)) for v in params.values())
        t0 = time.time()
        if has_list:
            r = self.http.post(url, data=json.dumps(params), headers=self._headers({"Content-Type": "application/json"}),
                               auth=self._auth(), timeout=timeout)
        else:
            q = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in params.items()}
            qs = urllib.parse.urlencode(q, quote_via=urllib.parse.quote)  # spaces as %20, not '+'
            r = self.http.get(f"{url}?{qs}" if qs else url, headers=self._headers(), auth=self._auth(), timeout=timeout)
        self.calls += 1
        if r.status_code >= 400:
            raise TigerGraphError(f"query {name} HTTP {r.status_code}: {r.text[:500]}")
        body = r.json()
        if body.get("error"):
            raise TigerGraphError(f"query {name}: {body.get('message')}")
        body["_latency"] = time.time() - t0
        return body.get("results", [])

    # ------------------------------------------------------------------ writes
    def upsert(self, vertices: dict | None = None, edges: dict | None = None) -> dict:
        payload = {}
        if vertices:
            payload["vertices"] = vertices
        if edges:
            payload["edges"] = edges
        r = self.http.post(f"{self.restpp}/graph/{self.graph}", data=json.dumps(payload),
                           headers=self._headers({"Content-Type": "application/json"}), auth=self._auth(),
                           timeout=600)
        self.calls += 1
        if r.status_code >= 400:
            raise TigerGraphError(f"upsert HTTP {r.status_code}: {r.text[:500]}")
        body = r.json()
        if body.get("error"):
            raise TigerGraphError(f"upsert: {body.get('message')}")
        return body.get("results", [{}])[0] if body.get("results") else {}

    def ping(self) -> bool:
        try:
            r = self.http.get(f"{self.restpp}/echo", headers=self._headers(), timeout=30)
            return r.ok
        except requests.RequestException:
            return False
