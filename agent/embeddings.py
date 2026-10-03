"""
Deterministic text embeddings for GraphRAG.

We embed case narratives, policy sections and new case descriptions with a signed feature-hashing
vectoriser (unigrams + bigrams + domain tokens, sublinear TF, L2-normalised, 256 dims). It needs no
model download or API call, so every environment (Savanna loader, agent, UI) produces identical
vectors, and the vectors live in TigerGraph's vector index. Domain tokens (pattern names, channel,
amount band, device/proxy flags) are injected so structurally similar cases land close together
even when their wording differs.
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Iterable

DIM = 256
_TOKEN = re.compile(r"[a-z0-9_]+")
_STOP = set("the a an of to in on and or for with by was were is are be this that from at as it its "
            "their they them which who whom these those than then there case cardholder".split())


def _h(tok: str) -> tuple[int, float]:
    d = hashlib.md5(tok.encode("utf-8")).digest()
    return int.from_bytes(d[:4], "little") % DIM, (1.0 if d[4] & 1 else -1.0)


def tokens(text: str) -> list[str]:
    words = [w for w in _TOKEN.findall(text.lower()) if w not in _STOP and not w.isdigit()]
    return words + [a + "_" + b for a, b in zip(words, words[1:])]


def embed(text: str, extra_tokens: Iterable[str] = ()) -> list[float]:
    counts: dict[str, int] = {}
    for t in tokens(text):
        counts[t] = counts.get(t, 0) + 1
    for t in extra_tokens:
        counts["#" + t] = counts.get("#" + t, 0) + 3  # structural tokens weigh more
    v = [0.0] * DIM
    for t, c in counts.items():
        i, s = _h(t)
        v[i] += s * (1.0 + math.log(c))
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [round(x / n, 6) for x in v]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def amount_band(x: float) -> str:
    if x < 10:
        return "amt_micro"
    if x < 100:
        return "amt_small"
    if x < 500:
        return "amt_medium"
    if x < 1000:
        return "amt_large"
    return "amt_xlarge"
