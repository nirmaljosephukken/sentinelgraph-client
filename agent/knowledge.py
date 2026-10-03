"""Chunk the knowledge documents (policy, typologies, regulatory digest, memory lessons) for GraphRAG."""
from __future__ import annotations

import re
from pathlib import Path

from agent.config import SETTINGS
from agent.embeddings import embed

DOCS = {
    "fraud_policy.md": "Fraud Policy v1.0",
    "fraud_patterns.md": "Known fraud patterns",
    "regulatory_guidance.md": "Regulatory guidance digest",
    "case_memory_lessons.md": "Case memory lessons",
}


def chunks(folder: Path | None = None) -> list[dict]:
    folder = folder or SETTINGS.knowledge_dir
    out = []
    for fname, title in DOCS.items():
        p = folder / fname
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8")
        # split on headings and on policy rule paragraphs (**R1.** ...), keep them small and citable
        parts = re.split(r"\n(?=#{2,3} )|\n(?=\*\*R\d+\.)|\n(?=\*\*\d\. )", text)
        for i, part in enumerate(parts):
            part = part.strip()
            if len(part) < 40:
                continue
            head = part.splitlines()[0].strip("#* ").strip()
            m = re.match(r"(R\d+)\.", head)
            section = m.group(1) if m else head[:80]
            cid = f"{fname.split('.')[0]}#{i:02d}"
            out.append({"id": cid, "doc": title, "section": section, "text": part[:2400],
                        "emb": embed(part, [section.lower().replace(" ", "_")])})
    return out
