"""Scam patterns the family confirmed, so Hearth recognises them next time.

When a sibling resolves a scam alert and replies ``learn 17: "exact words"``,
hearth-pulse stores the phrases here and rewrites the "Seen in this family"
section of the ``hearth-scam-patterns`` room document in both rooms. Every
companion reads that section and adds the phrases to its deterministic
triage, so the next call with the same script is flagged in milliseconds.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

HEADING = "## Seen in this family"
_QUOTED = re.compile(r"\"([^\"]{3,160})\"")


@dataclass
class LearnedScam:
    day: str
    incident: str
    description: str
    phrases: list[str] = field(default_factory=list)
    by: str = ""


def load(path: Path) -> list[LearnedScam]:
    if not path.exists():
        return []
    try:
        return [LearnedScam(**item) for item in json.loads(path.read_text(encoding="utf-8"))]
    except (ValueError, TypeError):
        return []


def save(path: Path, items: list[LearnedScam]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(i) for i in items], indent=2, ensure_ascii=False), encoding="utf-8")


def render_section(items: list[LearnedScam]) -> str:
    lines = [
        HEADING,
        "Confirmed by a sibling after a real alert. Companions treat these exact phrases as HIGH.",
        "",
    ]
    if not items:
        lines.append("- Nothing yet.")
    for item in items:
        quoted = ", ".join(f'"{p}"' for p in item.phrases) or "(no exact phrases)"
        by = f", confirmed by @{item.by}" if item.by else ""
        lines.append(f"- {item.day} ({item.incident}{by}): {item.description} Phrases: {quoted}")
    return "\n".join(lines)


def with_section(base: str, items: list[LearnedScam]) -> str:
    """The scam-patterns document: the shipped text plus what this family learned."""
    head = base.split(HEADING, 1)[0].rstrip()
    return f"{head}\n\n{render_section(items)}\n"


def phrases_from_document(text: str) -> list[str]:
    """Read the learned phrases back out of the room document."""
    if HEADING not in text:
        return []
    section = text.split(HEADING, 1)[1]
    phrases = []
    for line in section.splitlines():
        if "Phrases:" in line:
            phrases += _QUOTED.findall(line.split("Phrases:", 1)[1])
    return phrases
