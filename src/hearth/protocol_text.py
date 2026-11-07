"""The plain-text contract between Hearth's agents and the people in the room.

Agents hand work to each other with messages (there is no separate task
protocol in Switch), so the companion's alerts carry one headline line that
hearth-pulse parses, and the siblings answer with short words like "ack 17".
Everything stays readable for the humans in the channel.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from hearth.triage import Severity

# ── Alerts: companion → family room → pulse ──────────────────────────────────

_ALERT_RE = re.compile(
    r"Hearth alert\s*[·|\-]\s*(RED|HIGH|MEDIUM|LOW)\s*[·|\-]\s*([a-z][a-z-]*)\s*[·|\-]\s*ref\s+([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
_FIELD_RE = re.compile(r"^(?P<key>[A-Za-z ]+?)\s*:\s*(?P<value>.+)$")

_ICON = {Severity.RED: "🚨", Severity.HIGH: "🔴", Severity.MEDIUM: "🟠", Severity.LOW: "🟢"}


@dataclass
class Alert:
    severity: Severity
    kind: str
    ref: str
    parent_said: str = ""
    told_parent: str = ""
    why: str = ""
    summary: str = ""
    about: str = ""  # family members the alert is about, comma-separated


def _one_line(text: str, limit: int = 400) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_alert(alert: Alert, parent_name: str) -> str:
    icon = _ICON.get(alert.severity, "🔔")
    lines = [f"{icon} Hearth alert · {alert.severity.name} · {alert.kind} · ref {alert.ref}"]
    if alert.summary:
        lines.append(f"Summary: {_one_line(alert.summary)}")
    if alert.parent_said:
        lines.append(f'{parent_name} said: "{_one_line(alert.parent_said)}"')
    if alert.told_parent:
        lines.append(f'Hearth told her: "{_one_line(alert.told_parent)}"')
    if alert.why:
        lines.append(f"Why: {_one_line(alert.why)}")
    if alert.about:
        lines.append(f"About: {_one_line(alert.about)}")
    return "\n".join(lines)


def parse_alert(body: str) -> Alert | None:
    match = _ALERT_RE.search(body)
    if not match:
        return None
    alert = Alert(
        severity=Severity.parse(match.group(1)),
        kind=match.group(2).lower(),
        ref=match.group(3),
    )
    for line in body[match.end():].splitlines():
        field = _FIELD_RE.match(line.strip())
        if not field:
            continue
        key = field.group("key").strip().lower()
        value = field.group("value").strip().strip('"')
        if key == "summary":
            alert.summary = value
        elif key.endswith(" said"):
            alert.parent_said = value
        elif key == "hearth told her":
            alert.told_parent = value
        elif key == "why":
            alert.why = value
        elif key == "about":
            alert.about = value
    return alert


# ── Care updates: companion → scribe ─────────────────────────────────────────


def format_care_update(scribe: str, topic: str, ref: str, details: str, confirm_with: str) -> str:
    return (
        f"🩺 Hearth care update · {topic} · ref {ref}\n"
        f"@{scribe} please compare this with the care plan, post the exact change you would make, "
        f"and apply it only after @{confirm_with} or another sibling replies `confirm {ref}`.\n"
        f"{details.strip()}"
    )


# ── Replies from the siblings: family room → pulse ───────────────────────────

_LEADING_MENTIONS = re.compile(r"^(\s*@[\w.\-]+[,:]?\s*)+")
_ID = r"(?:\s*(?:esc[-\s]?|#)?(?P<id>\d+))?"
_ACK_RE = re.compile(
    r"^(?:ack(?:ed|ing)?|on it|got it|handling it|calling(?: her| amma| appa| now)?|i'?ll call|i will call)"
    + _ID + r"(?:\b|$)",
    re.IGNORECASE,
)
# "resolved" and "all clear" work alone; "close" and "done" are everyday words,
# so they count only with an incident number ("done 17").
_RESOLVE_RE = re.compile(
    r"^(?:(?:resolve[ds]?|all clear)" + _ID
    + r"|(?:close[ds]?|done)\s*(?:esc[-\s]?|#)?(?P<done_id>\d+))"
    + r"\s*(?:$|[:\-–]\s*|\s+)(?P<note>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_STATUS_RE = re.compile(r"^(?:status|who'?s on duty|on duty\??|rota)\s*\??$", re.IGNORECASE)
_DRILL_RE = re.compile(r"^drill(?:\s+(?P<kind>[a-z-]+))?(?:\s+(?P<severity>red|high|medium|low))?\s*$", re.IGNORECASE)
_HANDOFF_RE = re.compile(r"^(?:handoff|hand over|handover|shift)\s*$", re.IGNORECASE)
# "safe" must be the whole message: "is it safe to give her a dolo?" is a question.
_SAFE_RE = re.compile(
    r"^(?:safe|i'?m safe|i am safe|i'?m (?:ok|okay|fine)|i am (?:ok|okay|fine)|all good(?: here)?|"
    r"i'?m fine,? (?:tell|ask) (?:amma|appa|her|him))\s*[.!]*$",
    re.IGNORECASE,
)
_LEARN_RE = re.compile(r"^learn(?![a-z])" + _ID + r"\s*[:\-–]?\s*(?P<note>.*)$", re.IGNORECASE | re.DOTALL)
_QUOTED = re.compile(r"[\"“”]([^\"“”]{3,160})[\"“”]")


@dataclass
class Reply:
    action: str  # ack | resolve | status | drill | safe | learn | handoff
    incident_id: int | None = None
    note: str = ""
    kind: str = ""
    severity: Severity = Severity.HIGH
    phrases: list[str] | None = None


def learn_phrases(note: str) -> list[str]:
    """Phrases a sibling wants Hearth to recognise next time: quoted text if
    any, otherwise the comma-separated parts of the note."""
    quoted = [q.strip() for q in _QUOTED.findall(note) if q.strip()]
    if quoted:
        return quoted
    return [part.strip() for part in re.split(r"[,;]", note) if len(part.strip()) >= 4]


def strip_mentions(body: str) -> str:
    return _LEADING_MENTIONS.sub("", body).strip()


def parse_reply(body: str) -> Reply | None:
    text = strip_mentions(body)
    if not text:
        return None
    if match := _RESOLVE_RE.match(text):
        raw_id = match.group("id") or match.group("done_id")
        incident = int(raw_id) if raw_id else None
        return Reply("resolve", incident, note=match.group("note").strip())
    if match := _ACK_RE.match(text):
        incident = int(match.group("id")) if match.group("id") else None
        return Reply("ack", incident)
    if _STATUS_RE.match(text):
        return Reply("status")
    if _HANDOFF_RE.match(text):
        return Reply("handoff")
    if _SAFE_RE.match(text):
        return Reply("safe")
    if match := _LEARN_RE.match(text):
        note = match.group("note").strip()
        incident = int(match.group("id")) if match.group("id") else None
        return Reply("learn", incident, note=note, phrases=learn_phrases(note))
    if match := _DRILL_RE.match(text):
        return Reply(
            "drill",
            kind=(match.group("kind") or "scam").lower(),
            severity=Severity.parse(match.group("severity") or "high"),
        )
    return None
