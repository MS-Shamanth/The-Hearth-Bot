"""The shift handoff: what the incoming sibling needs to know, in three lines.

Built deterministically from the parent's own messages (read from her room's
history) and the incident book, so it is the same every time and never
invents anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from hearth.config import HearthConfig, Sibling
from hearth.incidents import Incident
from hearth.triage import assess

_MENTION_LABELS = {
    "pain": "pain",
    "unwell": "feeling unwell",
    "wellbeing": "low mood or poor sleep",
    "missed-medicine": "a missed or finished medicine",
    "money-request": "a stranger asking about money",
    "scam": "a scam call",
    "relative-in-trouble": "a 'your child is in trouble' call",
    "medical-emergency": "a possible emergency",
    "fall": "a fall",
}


@dataclass
class ParentMessage:
    at: datetime
    text: str


def build_handoff(
    cfg: HearthConfig,
    *,
    incoming: Sibling,
    outgoing: Sibling | None,
    messages: list[ParentMessage],
    incidents: list[Incident],
    now: datetime,
    hours: int = 8,
    addressed: bool = False,
) -> str:
    """``addressed``: the message will be sent as a mention of the incoming
    sibling, so the first line says "you" instead of repeating their handle."""
    parent = cfg.parent
    local_in = now.astimezone(incoming.timezone)
    off = f" @{outgoing.handle} is off." if outgoing and outgoing.handle != incoming.handle else ""
    who = "you're" if addressed else f"@{incoming.handle} is"
    lines = [
        f"🌅 Shift handoff: {who} now first on call "
        f"({local_in:%H:%M} {incoming.timezone.key}).{off}"
    ]

    readings: list[str] = []
    mentions: list[str] = []
    took: datetime | None = None
    names = [s.name for s in cfg.siblings]
    for message in messages:
        found = assess(message.text, family_names=names)
        readings += [r for r in found.readings if r not in readings]
        for kind in found.kinds:
            label = _MENTION_LABELS.get(kind)
            if label and label not in mentions:
                mentions.append(label)
        if found.took_medicine:
            took = message.at

    if messages:
        last = messages[-1].at.astimezone(parent.timezone)
        parts = [f"{len(messages)} message{'s' if len(messages) != 1 else ''}, last at {last:%H:%M} her time"]
        if readings:
            parts.append(", ".join(readings[-3:]))
        if took:
            parts.append(f"said she took her medicines at {took.astimezone(parent.timezone):%H:%M}")
        if mentions:
            parts.append("mentioned " + ", ".join(mentions))
        lines.append(f"Last {hours}h with {parent.call_name}: " + "; ".join(parts) + ".")
    else:
        lines.append(f"Last {hours}h with {parent.call_name}: no messages from her. Worth a call.")

    open_now = [i for i in incidents if i.state in ("open", "acked")]
    if open_now:
        items = ", ".join(
            f"{i.label} {i.sev.name} {i.kind} ({'acked by @' + i.acked_by if i.acked_by else 'not acked'})"
            for i in open_now
        )
        lines.append(f"Open incidents: {items}.")
    else:
        lines.append("Open incidents: none.")
    return "\n".join(lines)
