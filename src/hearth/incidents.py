"""The escalation state machine behind hearth-pulse.

Pure: time is passed in, nothing here talks to Switch. That keeps the paging
rules testable without a server.

Paging rules
* RED: page every sibling at once, and again after each timeout until
  someone acknowledges (at most ``max_red_pages`` times).
* HIGH and MEDIUM: page the first person in the follow-the-sun order. With no
  ack before the timeout, page the next person, and so on. After the last
  person, page everyone once.
* LOW: no page. It is noted for the digest.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from hearth.config import Sibling
from hearth.protocol_text import Alert
from hearth.rota import paging_order
from hearth.triage import Severity

OPEN = "open"          # paging, nobody has acknowledged
ACKED = "acked"        # a person said they are on it
RESOLVED = "resolved"  # closed with a note
NOTED = "noted"        # LOW: kept for the digest, never paged


@dataclass
class Page:
    at: str
    step: int
    handles: list[str]
    everyone: bool


@dataclass
class Incident:
    id: int
    severity: int
    kind: str
    ref: str
    opened_at: str
    summary: str = ""
    parent_said: str = ""
    told_parent: str = ""
    why: str = ""
    drill: bool = False
    state: str = OPEN
    order: list[str] = field(default_factory=list)
    step: int = 0
    next_page_at: str | None = None
    pages: list[Page] = field(default_factory=list)
    thread_id: str | None = None
    acked_by: str | None = None
    acked_at: str | None = None
    resolved_by: str | None = None
    resolved_at: str | None = None
    note: str = ""

    @property
    def sev(self) -> Severity:
        return Severity(self.severity)

    @property
    def label(self) -> str:
        return f"ESC-{self.id}"

    def paged_handles(self) -> set[str]:
        return {h for page in self.pages for h in page.handles}


@dataclass
class PageAction:
    incident: Incident
    handles: list[str]
    everyone: bool
    step: int
    reason: str


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _dt(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


class IncidentBook:
    def __init__(
        self,
        siblings: tuple[Sibling, ...] | list[Sibling],
        timeout_for: Callable[[Severity], int],
        *,
        max_red_pages: int = 3,
        path: Path | None = None,
    ) -> None:
        self.siblings = list(siblings)
        self.timeout_for = timeout_for
        self.max_red_pages = max_red_pages
        self.path = path
        self.incidents: list[Incident] = []
        self._next_id = 1

    # ── persistence ──────────────────────────────────────────────────────

    def load(self) -> None:
        if not self.path or not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        self._next_id = int(data.get("next_id", 1))
        self.incidents = []
        for raw in data.get("incidents", []):
            raw["pages"] = [Page(**p) for p in raw.get("pages", [])]
            self.incidents.append(Incident(**raw))

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "next_id": self._next_id,
            "incidents": [asdict(i) for i in self.incidents],
        }
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    # ── queries ──────────────────────────────────────────────────────────

    def get(self, incident_id: int) -> Incident | None:
        return next((i for i in self.incidents if i.id == incident_id), None)

    def by_ref(self, ref: str) -> Incident | None:
        return next((i for i in self.incidents if i.ref == ref), None)

    def unresolved(self) -> list[Incident]:
        return [i for i in self.incidents if i.state in (OPEN, ACKED)]

    def awaiting_ack(self) -> list[Incident]:
        return [i for i in self.incidents if i.state == OPEN]

    # ── transitions ──────────────────────────────────────────────────────

    def open(self, alert: Alert, now: datetime, *, drill: bool = False) -> tuple[Incident, bool]:
        """Open an incident for an alert. The same ref twice is one incident."""
        existing = self.by_ref(alert.ref)
        if existing:
            return existing, False
        incident = Incident(
            id=self._next_id,
            severity=int(alert.severity),
            kind=alert.kind,
            ref=alert.ref,
            opened_at=_iso(now),
            summary=alert.summary,
            parent_said=alert.parent_said,
            told_parent=alert.told_parent,
            why=alert.why,
            drill=drill,
            order=[s.handle for s in paging_order(self.siblings, now)],
        )
        self._next_id += 1
        if alert.severity <= Severity.LOW:
            incident.state = NOTED
        else:
            incident.next_page_at = _iso(now)
        self.incidents.append(incident)
        return incident, True

    def due(self, now: datetime) -> list[PageAction]:
        """Pages that should go out now. Advances each incident's schedule."""
        actions: list[PageAction] = []
        for incident in self.awaiting_ack():
            due_at = _dt(incident.next_page_at)
            if due_at is None or due_at > now:
                continue
            action = self._next_page(incident, now)
            if action:
                incident.pages.append(
                    Page(at=_iso(now), step=action.step, handles=action.handles, everyone=action.everyone)
                )
                actions.append(action)
        return actions

    def _next_page(self, incident: Incident, now: datetime) -> PageAction | None:
        timeout = timedelta(seconds=self.timeout_for(incident.sev))
        step = incident.step
        everyone = [s.handle for s in self.siblings]

        if incident.sev >= Severity.RED:
            if step >= self.max_red_pages:
                incident.next_page_at = None
                return None
            incident.step += 1
            incident.next_page_at = _iso(now + timeout)
            reason = "possible emergency, paging everyone" if step == 0 else (
                f"no ack after {int(timeout.total_seconds())}s, paging everyone again"
            )
            return PageAction(incident, everyone, True, step, reason)

        order = incident.order or everyone
        if step < len(order):
            incident.step += 1
            incident.next_page_at = _iso(now + timeout)
            if step == 0:
                reason = "first page, on duty now"
            else:
                reason = f"no ack from @{order[step - 1]} after {int(timeout.total_seconds())}s"
            return PageAction(incident, [order[step]], False, step, reason)
        if step == len(order):
            incident.step += 1
            incident.next_page_at = None
            return PageAction(
                incident, everyone, True, step,
                f"nobody acknowledged after {len(order)} pages, paging everyone",
            )
        incident.next_page_at = None
        return None

    def ack(self, by: str, incident_id: int | None, now: datetime) -> Incident | None:
        incident = self._pick(by, incident_id, states=(OPEN,))
        if incident is None:
            return None
        incident.state = ACKED
        incident.acked_by = by
        incident.acked_at = _iso(now)
        incident.next_page_at = None
        return incident

    def resolve(self, by: str, incident_id: int | None, note: str, now: datetime) -> Incident | None:
        incident = self._pick(by, incident_id, states=(OPEN, ACKED))
        if incident is None:
            return None
        if incident.state == OPEN:
            incident.acked_by = incident.acked_by or by
            incident.acked_at = incident.acked_at or _iso(now)
        incident.state = RESOLVED
        incident.resolved_by = by
        incident.resolved_at = _iso(now)
        incident.next_page_at = None
        incident.note = note
        return incident

    def resolve_kind(self, kind: str, by: str, note: str, now: datetime) -> list[Incident]:
        """Close every unresolved incident of a kind, e.g. when the companion is back."""
        closed = []
        for incident in self.unresolved():
            if incident.kind == kind:
                self.resolve(by, incident.id, note, now)
                closed.append(incident)
        return closed

    def _pick(self, by: str, incident_id: int | None, *, states: tuple[str, ...]) -> Incident | None:
        if incident_id is not None:
            incident = self.get(incident_id)
            return incident if incident and incident.state in states else None
        candidates = [i for i in self.incidents if i.state in states]
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        mine = [i for i in candidates if by in i.paged_handles() or i.acked_by == by]
        if len(mine) == 1:
            return mine[0]
        return None  # ambiguous: the caller asks for a number

    # ── reporting ────────────────────────────────────────────────────────

    def to_markdown(self, limit: int = 40) -> str:
        rows = ["| # | Opened (UTC) | Severity | Kind | State | Acked by | Resolved by | Note |",
                "|---|---|---|---|---|---|---|---|"]
        for incident in sorted(self.incidents, key=lambda i: i.id, reverse=True)[:limit]:
            opened = (_dt(incident.opened_at) or datetime.min).strftime("%Y-%m-%d %H:%M")
            kind = incident.kind + (" (drill)" if incident.drill else "")
            note = (incident.note or incident.summary or "").replace("|", "/")[:120]
            rows.append(
                f"| {incident.label} | {opened} | {incident.sev.name} | {kind} | {incident.state} "
                f"| {incident.acked_by or ''} | {incident.resolved_by or ''} | {note} |"
            )
        return "\n".join(rows)
