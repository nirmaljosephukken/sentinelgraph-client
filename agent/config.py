"""Central configuration. Reads a .env file (repo root or parent folder) without extra dependencies."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    for p in (ROOT / ".env", ROOT.parent / ".env"):
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            break


_load_dotenv()


@dataclass
class Settings:
    # TigerGraph (Savanna or Community Edition)
    tg_host: str = os.getenv("TG_HOST", "").rstrip("/")
    tg_graph: str = os.getenv("TG_GRAPH", os.getenv("TG_GRAPHNAME", "FraudGraph"))
    tg_secret: str = os.getenv("TG_SECRET", "")
    tg_username: str = os.getenv("TG_USERNAME", "")
    tg_password: str = os.getenv("TG_PASSWORD", "")
    tg_token: str = os.getenv("TG_API_TOKEN", "")
    tg_restpp_port: str = os.getenv("TG_RESTPP_PORT", "")      # blank = Savanna (443, /restpp prefix)
    tg_gs_port: str = os.getenv("TG_GS_PORT", "")
    tg_verify_ssl: bool = os.getenv("TG_VERIFY_SSL", "true").lower() != "false"
    # backend: "tigergraph" (default when TG_HOST is set) or "local" (pandas mirror, for offline dev/tests)
    backend: str = os.getenv("GRAPH_BACKEND", "tigergraph" if os.getenv("TG_HOST") else "local")
    # use the TigerGraph MCP server for installed-query calls (falls back to RESTPP automatically)
    use_mcp: bool = os.getenv("USE_TG_MCP", "true").lower() != "false"
    mcp_command: str = os.getenv("TG_MCP_COMMAND", "tigergraph-mcp")
    # LLM
    # Gemini on Vertex AI (Cloud Run): no API key, the service account authenticates
    vertex: bool = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in ("true", "1")
    gcp_project: str = os.getenv("GOOGLE_CLOUD_PROJECT", "")
    gcp_location: str = os.getenv("GOOGLE_CLOUD_LOCATION", "global")
    llm_provider: str = os.getenv("LLM_PROVIDER", "gemini" if (os.getenv("GEMINI_API_KEY") or
                                                               os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").lower()
                                                               in ("true", "1")) else
                                  ("anthropic" if os.getenv("ANTHROPIC_API_KEY") else "none"))
    gemini_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
    anthropic_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
    llm_max_tool_calls: int = int(os.getenv("LLM_MAX_TOOL_CALLS", "4"))
    # data
    prepared_dir: Path = field(default_factory=lambda: Path(os.getenv("PREPARED_DIR", ROOT / "data" / "prepared")))
    cases_dir: Path = field(default_factory=lambda: ROOT / "cases")
    traces_dir: Path = field(default_factory=lambda: ROOT / "traces")
    knowledge_dir: Path = field(default_factory=lambda: ROOT / "knowledge")


SETTINGS = Settings()
