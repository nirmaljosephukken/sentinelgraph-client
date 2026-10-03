"""
LLM layer (provider-agnostic: Gemini or Claude over plain REST, or none).

The LLM has two jobs, both bounded:
Gemini is reached either through the Gemini API (GEMINI_API_KEY) or through Vertex AI
(GOOGLE_GENAI_USE_VERTEXAI=TRUE + GOOGLE_CLOUD_PROJECT/LOCATION, authenticated with Application Default Credentials,
i.e. the Cloud Run service account). Both take the same request body and return the same response shape.

  1. Investigator: after the deterministic playbook, it reads an evidence digest and may call up to
     LLM_MAX_TOOL_CALLS extra graph/RAG tools (function calling) to probe open questions. It returns
     analyst notes. It cannot change probabilities or actions.
  2. Writer: turns the structured case into the case summary, the SAR narrative, the undocumented-pattern
     description, what changed, and the stop reason. Output is validated: any ID not present in the evidence
     is rejected and the deterministic template is used instead.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

import requests

from agent.config import SETTINGS, Settings

GEMINI_FALLBACKS = ["gemini-flash-latest", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.8-flash",
                    "gemini-3.5-flash-lite", "gemini-flash-lite-latest"]
ID_PATTERNS = [r"\bC\d{5}-K\d\b", r"\bC\d{5}\b", r"\b3\d{6}\b", r"\bCC-\d{4}\b", r"\bALR-\d{3}\b"]
try:  # client id formats declared in config/mappings/*.yaml, so invented client ids are caught too
    from agent.ingest.mapping import client_id_patterns
    ID_PATTERNS += [p for p in client_id_patterns() if p not in ID_PATTERNS]
except Exception:  # noqa: BLE001  a bad mapping file must not stop the guardrail
    pass


class LLM:
    _shared_good_model: str | None = None  # remembered across cases (skips overloaded models)

    def __init__(self, s: Settings = SETTINGS):
        self.s = s
        has_gemini = bool(s.gemini_key or (s.vertex and s.gcp_project))
        self.provider = s.llm_provider if (has_gemini or s.anthropic_key) else "none"
        if self.provider == "gemini" and not has_gemini:
            self.provider = "anthropic" if s.anthropic_key else "none"
        self.tokens = 0
        self.calls = 0
        self.errors: list[str] = []
        self._good_model: str | None = None
        self.used_model: str | None = None
        self.pii_masked: dict[str, int] = {}     # what agent.safety hid before sending, for the audit trail

    @property
    def model(self) -> str:
        if self.used_model:
            return self.used_model
        return {"gemini": self.s.gemini_model, "anthropic": self.s.anthropic_model}.get(self.provider, "template")

    # ------------------------------------------------------------------ transport
    def _outgoing(self, items: list[dict]) -> list[dict]:
        """Last checkpoint before text leaves for the model: personal data is masked in everything we send
        (prompts, tool results). The model's own earlier turns are passed through unchanged."""
        from agent.safety import mask_obj
        out = []
        for it in items:
            if it.get("role") in ("model", "assistant"):
                out.append(it)
                continue
            m, found = mask_obj(it)
            for k, v in found.items():
                self.pii_masked[k] = self.pii_masked.get(k, 0) + v
            out.append(m)
        return out

    def _gemini(self, contents: list[dict], system: str, tools: list[dict] | None, json_mode: bool) -> dict:
        contents = self._outgoing(contents)
        body: dict[str, Any] = {"contents": contents, "systemInstruction": {"parts": [{"text": system}]},
                                "generationConfig": {"temperature": 0.2, "maxOutputTokens": 4096}}
        if json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        if tools:
            body["tools"] = [{"functionDeclarations": tools}]
        chain = [self.s.gemini_model] + [m for m in GEMINI_FALLBACKS if m != self.s.gemini_model]
        good = self._good_model or LLM._shared_good_model
        if good:
            chain = [good] + [m for m in chain if m != good]
        last = ""
        for model in chain:
            url, headers = self._gemini_endpoint(model)
            for attempt in range(2):
                r = requests.post(url, json=body, headers=headers, timeout=120)
                self.calls += 1
                if r.status_code in (500, 503) and attempt == 0:
                    time.sleep(2)
                    continue
                break
            if r.status_code < 400:
                data = r.json()
                self._good_model = model
                LLM._shared_good_model = model
                self.used_model = model
                self.tokens += int(data.get("usageMetadata", {}).get("totalTokenCount", 0))
                return data
            last = f"{model}: HTTP {r.status_code} {r.text[:160]}"
        raise RuntimeError(f"gemini unavailable ({last})")

    _creds = None  # Application Default Credentials, shared and refreshed when expired

    def _gemini_endpoint(self, model: str) -> tuple[str, dict]:
        if not (self.s.vertex and self.s.gcp_project):
            return (f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    {"x-goog-api-key": self.s.gemini_key})
        import google.auth
        from google.auth.transport.requests import Request
        if LLM._creds is None:
            LLM._creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        if not LLM._creds.valid:
            LLM._creds.refresh(Request())
        loc = self.s.gcp_location or "global"
        host = "aiplatform.googleapis.com" if loc == "global" else f"{loc}-aiplatform.googleapis.com"
        url = (f"https://{host}/v1/projects/{self.s.gcp_project}/locations/{loc}/publishers/google/models/"
               f"{model}:generateContent")
        return url, {"Authorization": f"Bearer {LLM._creds.token}"}

    def _anthropic(self, messages: list[dict], system: str, tools: list[dict] | None) -> dict:
        messages = self._outgoing(messages)
        body: dict[str, Any] = {"model": self.s.anthropic_model, "max_tokens": 4096, "system": system,
                                "messages": messages, "temperature": 0.2}
        if tools:
            body["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                             for t in tools]
        r = requests.post("https://api.anthropic.com/v1/messages", json=body, timeout=120,
                          headers={"x-api-key": self.s.anthropic_key, "anthropic-version": "2023-06-01"})
        self.calls += 1
        if r.status_code >= 400:
            raise RuntimeError(f"anthropic HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        u = data.get("usage", {})
        self.tokens += int(u.get("input_tokens", 0)) + int(u.get("output_tokens", 0))
        return data

    # ------------------------------------------------------------------ JSON completion
    def complete_json(self, system: str, prompt: str) -> dict | None:
        if self.provider == "none":
            return None
        try:
            if self.provider == "gemini":
                d = self._gemini([{"role": "user", "parts": [{"text": prompt}]}], system, None, True)
                text = "".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"])
            else:
                d = self._anthropic([{"role": "user", "content": prompt + "\nRespond with one JSON object only."}],
                                    system, None)
                text = "".join(b.get("text", "") for b in d["content"] if b.get("type") == "text")
            m = re.search(r"\{.*\}", text, re.S)
            return json.loads(m.group(0)) if m else None
        except Exception as e:  # never let the writer break an investigation
            self.errors.append(str(e)[:200])
            from agent import obs
            obs.event("llm_error", "WARNING", provider=self.provider, error=str(e)[:200])
            return None

    # ------------------------------------------------------------------ tool-using investigator
    def investigate(self, system: str, prompt: str, tools: list[dict], call_tool: Callable[[str, dict], Any],
                    max_calls: int) -> tuple[str, list[dict]]:
        """Bounded function-calling loop. Returns (notes, calls)."""
        if self.provider == "none" or max_calls <= 0:
            return "", []
        calls: list[dict] = []
        try:
            if self.provider == "gemini":
                contents = [{"role": "user", "parts": [{"text": prompt}]}]
                for _ in range(max_calls + 1):
                    d = self._gemini(contents, system, tools if len(calls) < max_calls else None, False)
                    parts = d["candidates"][0]["content"].get("parts", [])
                    fcs = [p["functionCall"] for p in parts if "functionCall" in p]
                    if not fcs:
                        return "".join(p.get("text", "") for p in parts).strip(), calls
                    contents.append({"role": "model", "parts": parts})
                    resp_parts = []
                    for fc in fcs[: max_calls - len(calls)] or fcs[:1]:
                        out = call_tool(fc["name"], dict(fc.get("args") or {}))
                        calls.append({"tool": fc["name"], "args": fc.get("args"), "result": _short(out)})
                        resp_parts.append({"functionResponse": {"name": fc["name"], "response": {"result": _short(out)}}})
                    contents.append({"role": "user", "parts": resp_parts})
                return "", calls
            messages = [{"role": "user", "content": prompt}]
            for _ in range(max_calls + 1):
                d = self._anthropic(messages, system, tools if len(calls) < max_calls else None)
                uses = [b for b in d["content"] if b.get("type") == "tool_use"]
                if not uses:
                    return "".join(b.get("text", "") for b in d["content"] if b.get("type") == "text").strip(), calls
                messages.append({"role": "assistant", "content": d["content"]})
                results = []
                for u in uses:
                    out = call_tool(u["name"], u.get("input") or {})
                    calls.append({"tool": u["name"], "args": u.get("input"), "result": _short(out)})
                    results.append({"type": "tool_result", "tool_use_id": u["id"], "content": json.dumps(_short(out))})
                messages.append({"role": "user", "content": results})
            return "", calls
        except Exception as e:
            self.errors.append(str(e)[:200])
            from agent import obs
            obs.event("llm_error", "WARNING", provider=self.provider, error=str(e)[:200])
            return "", calls


def _short(x: Any, limit: int = 3500) -> Any:
    s = json.dumps(x, default=str)
    if len(s) <= limit:
        return x
    return {"truncated": True, "preview": s[:limit]}


def foreign_ids(text: str, allowed: set[str]) -> list[str]:
    found = set()
    for pat in ID_PATTERNS:
        found |= set(re.findall(pat, text or ""))
    # a customer id is allowed if any allowed card id starts with it
    return sorted(i for i in found if i not in allowed and not any(a.startswith(i + "-") for a in allowed))
