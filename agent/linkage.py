"""
How connected cards are linked, stated only from the evidence.

Every connected card carries one or more link kinds, recorded by the detector that found it:

  shared_device   the card used the same (rare) device profile as the flagged card        (device_fanout)
  peer_device     a near-identical purchase within 48h on the same device profile          (peer_txns)
  peer_email      a near-identical purchase within 48h with the same purchaser and
                  recipient email domains                                                   (peer_txns)
  structuring     the same just-under-$500 burst (amount and timing), no shared entity      (amount_band_scan)

`link_statement` turns these into the one sentence that explains the link, in code, so no LLM ever
describes the mechanism. `unsupported_link_claims` rejects text that asserts a mechanism the evidence does
not contain (for example "device-sharing ring" when the only link is amount and timing). The validator
applies the same check to the answer files.
"""
from __future__ import annotations

import re

DEVICE_KINDS = {"shared_device", "peer_device"}
EMAIL_KINDS = {"peer_email"}

LABEL = {
    "shared_device": "use the same rare device profile as this card",
    "peer_device": "made a near-identical purchase within 48 hours on the same device profile",
    "peer_email": "made a near-identical purchase within 48 hours with the same purchaser and recipient email domains",
    "structuring": ("show the same structuring pattern (a burst of just-under-$500 online purchases within an hour); "
                    "the link is amount and timing only"),
}

# phrases that assert a device link, an email link, or an organised ring built on shared infrastructure
DEVICE_CLAIM = re.compile(
    r"device[- ]shar\w*|shar\w*[\s-]+(?:a\s+|the\s+same\s+|one\s+|common\s+)?(?:rare\s+)?device|same\s+device|"
    r"common\s+device|device\s+ring|device[- ]based\s+ring|device\s+fan-?out\s+link\w*|linked\s+by\s+(?:a\s+)?device",
    re.I)
EMAIL_CLAIM = re.compile(r"shar\w*\s+(?:the\s+same\s+)?email|same\s+email|common\s+email", re.I)
RING_CLAIM = re.compile(r"\bring\b", re.I)
# explicit negations ("no shared device", "None of them shares a device profile ...") are not claims
NEGATION = re.compile(r"\b(?:no|not|none of them|without|lack of|absence of)\b[^.;:()]{0,40}?(?:device|email|ring)[^.;:()]*",
                      re.I)


def kinds_of(links: dict[str, set[str]]) -> set[str]:
    out: set[str] = set()
    for k in links.values():
        out |= set(k)
    return out


def link_statement(links: dict[str, set[str]]) -> str:
    """One deterministic sentence (or two) explaining why the connected cards are linked."""
    if not links:
        return ""
    by_kind: dict[str, list[str]] = {}
    for card, kinds in sorted(links.items()):
        for k in sorted(kinds):
            by_kind.setdefault(k, []).append(card)
    parts = []
    for k in ("shared_device", "peer_device", "peer_email", "structuring"):
        cards = by_kind.get(k)
        if cards:
            parts.append(f"{len(cards)} card(s) {LABEL[k]}")
    s = f"How the {len(links)} connected card(s) are linked: " + "; ".join(parts) + "."
    if not (kinds_of(links) & DEVICE_KINDS):
        s += " None of them shares a device profile with this card."
    return s


def unsupported_link_claims(text: str, kinds: set[str]) -> list[str]:
    """Mechanisms asserted in `text` that the evidence does not support."""
    bad = []
    text = NEGATION.sub(" ", text or "")
    if not (kinds & DEVICE_KINDS):
        bad += [m.group(0) for m in DEVICE_CLAIM.finditer(text)]
        bad += [m.group(0) for m in RING_CLAIM.finditer(text)]
    if not (kinds & EMAIL_KINDS):
        bad += [m.group(0) for m in EMAIL_CLAIM.finditer(text)]
    return sorted(set(bad))


def strip_unsupported(text: str, kinds: set[str]) -> tuple[str, list[str]]:
    """Drop the sentences of free text (e.g. LLM investigator notes) that assert an unsupported link mechanism."""
    keep, dropped = [], []
    for sent in re.split(r"(?<=[.!?])\s+", (text or "").strip()):
        (dropped if unsupported_link_claims(sent, kinds) else keep).append(sent)
    return " ".join(k for k in keep if k), dropped


SHORT = {"shared_device": "same rare device profile", "peer_device": "near-identical purchase on the same device profile",
         "peer_email": "near-identical purchase with the same email domains",
         "structuring": "same just-under-$500 structuring pattern, linked by amount and timing only"}


def link_summary(links: dict[str, set[str]]) -> str:
    """Short form for action reasons, e.g. '11 via same just-under-$500 structuring pattern, ...'."""
    counts: dict[str, int] = {}
    for kinds in links.values():
        for k in kinds:
            counts[k] = counts.get(k, 0) + 1
    return "; ".join(f"{counts[k]} via {SHORT[k]}" for k in ("shared_device", "peer_device", "peer_email", "structuring")
                     if k in counts)
