"""hearth-pulse: the deterministic agent on the family desk.

No LLM. It turns the companion's alerts into incidents, pages the sibling on
duty, re-pages the next one when nobody acknowledges, tells the parent who is
calling her, watches that some companion holds the `companion` role, and
keeps the incident log document current. Timers and guarantees live here,
judgment lives in the LLM agents.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from hearth import learned
from hearth.config import STATE_DIR, ConfigError, HearthConfig, Sibling, demo_mode, load_config
from hearth.handoff import ParentMessage, build_handoff
from hearth.incidents import Incident, IncidentBook, PageAction
from hearth.phrases import phrase
from hearth.protocol_text import Alert, parse_alert, parse_reply
from hearth.pulse.setup import RoomsInfo, ensure_rooms, scam_patterns_text, try_set_addressing
from hearth.rota import describe, paging_order
from hearth.switch.client import (
    AgentConnection,
    AgentCredentials,
    Frame,
    MessageEvent,
    SwitchAPI,
    SwitchError,
)
from hearth.switch.people import RoomPeople
from hearth.triage import Severity

log = logging.getLogger("hearth.pulse")

_ICON = {Severity.RED: "🚨", Severity.HIGH: "🔴", Severity.MEDIUM: "🟠", Severity.LOW: "🟢"}
_SCAM_KINDS = {"scam", "relative-in-trouble", "money-request"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _ignore(_frame: Frame) -> None:
    return None


class Pulse:
    def __init__(self, cfg: HearthConfig, api: SwitchAPI, *, bridge: str | None = None) -> None:
        self.cfg = cfg
        self.api = api
        self.bridge = bridge
        self.demo = demo_mode()
        self.parent_room_id = ""
        self.family_room_id = ""
        self.conn = AgentConnection(api, on_connected=self._on_connected)
        self.people: RoomPeople | None = None
        self.book = IncidentBook(
            cfg.siblings,
            lambda severity: cfg.escalation.timeout_for(severity.label, self.demo),
            path=STATE_DIR / "state" / "incidents.json",
        )
        self.book.load()
        self.state_path = STATE_DIR / "state" / "pulse.json"
        self.learned_path = STATE_DIR / "state" / "learned-scams.json"
        self.state: dict[str, Any] = self._load_state()
        self.rooms: RoomsInfo | None = None
        self.parent_people: RoomPeople | None = None
        self._dirty_log = True
        self._last_log_write = 0.0
        self._offline_since: datetime | None = None
        self._setup_done = asyncio.Event()

    # ── state ────────────────────────────────────────────────────────────

    def _load_state(self) -> dict[str, Any]:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass
        return {}

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    def _changed(self) -> None:
        self.book.save()
        self._dirty_log = True

    # ── lifecycle ────────────────────────────────────────────────────────

    async def setup(self) -> None:
        reader = asyncio.create_task(self.conn.run(self._on_frame))
        try:
            await asyncio.wait_for(self.conn.ready.wait(), 20)
            await self._do_setup()
        finally:
            self.conn.stop()
            reader.cancel()

    async def _do_setup(self) -> None:
        self.rooms = await ensure_rooms(
            self.conn, self.cfg, bridge=self.bridge, learned_path=self.learned_path
        )
        self.parent_room_id, self.family_room_id = self.rooms.parent_id, self.rooms.family_id
        self.conn.rooms = [self.family_room_id]
        await try_set_addressing(self.api, "anyone")
        self.people = RoomPeople(self.api, self.family_room_id)
        await self.people.refresh()
        self.parent_people = RoomPeople(self.api, self.parent_room_id)
        await self.parent_people.refresh()
        self._setup_done.set()

    async def run(self) -> None:
        if not await self.api.health():
            raise ConfigError(f"Switch is not answering at {self.api.base_url}/health")
        reader = asyncio.create_task(self.conn.run(self._on_frame))
        await asyncio.wait_for(self.conn.ready.wait(), 20)
        await self._do_setup()
        await self._kickoff_scribe()
        await self._post(
            f"💓 hearth-pulse is on the desk{' (demo timers)' if self.demo else ''}. "
            f"Paging order right now:\n{describe(self.cfg.siblings)}\n"
            "Reply `ack` (or `ack 12`) when you take an alert, `resolved 12: note` when it's done, "
            "`status` to see what's open, `handoff` for a shift summary."
        )
        await asyncio.gather(
            reader, self._tick_loop(), self._watchdog_loop(), self._digest_loop(), self._handoff_loop()
        )

    async def _on_connected(self) -> None:
        # After a reconnect, claim the family room again and refresh who's who.
        if self._setup_done.is_set():
            await self.conn.op("connect_to_room", {"room_id": self.family_room_id})
            if self.people:
                await self.people.refresh()

    async def _kickoff_scribe(self) -> None:
        if self.state.get("scribe_kicked_off"):
            return
        scribe = self.cfg.agents.scribe
        if self.people and scribe.lower() not in self.people.agent_names:
            log.info("%s isn't in the family room yet; the kickoff waits for the next start", scribe)
            return
        await self._targeted(
            f"you're the scribe for {self.cfg.parent.name}'s care book in this room. Read your "
            "instructions and this room's documents, then: (1) create the room document `care-plan` from "
            "carebook/care-plan.md in your folder, (2) reply here with a three-line summary of the care "
            "plan, (3) wait for care updates from the companion.",
            [scribe],
        )
        self.state["scribe_kicked_off"] = True
        self._save_state()

    # ── events ───────────────────────────────────────────────────────────

    async def _on_frame(self, frame: Frame) -> None:
        if frame.event == "room_join" and self.people:
            await self.people.refresh()
            return
        if frame.event != "message" or not self._setup_done.is_set():
            return
        event = MessageEvent.from_frame(frame.data, frame.sequence)
        if event.room_id != self.family_room_id or not self.people:
            return
        await self.people.refresh(max_age=300)
        if self.people.is_agent(event):
            alert = parse_alert(event.body)
            if alert:
                await self._open(alert)
            return
        who = self._handle_for(event.sender_name)
        now = _utcnow()
        self._seen(who, now)
        reply = parse_reply(event.body)
        if reply is None:
            return
        if reply.action == "ack":
            incident = self.book.ack(who, reply.incident_id, now)
            if incident is None:
                await self._ambiguous("ack", reply.incident_id)
                return
            self._changed()
            await self._post(
                f"✅ {incident.label}: @{who} is on it, paging stopped. "
                f"When it's sorted, reply `resolved {incident.id}: what happened`."
            )
            await self._tell_parent(who, incident.drill)
        elif reply.action == "resolve":
            incident = self.book.resolve(who, reply.incident_id, reply.note, now)
            if incident is None:
                await self._ambiguous("resolve", reply.incident_id)
                return
            self._changed()
            note = f": {reply.note}" if reply.note else ""
            learn = ""
            if incident.kind in _SCAM_KINDS and not incident.drill:
                learn = (f"\nIf it was a scam, reply `learn {incident.id}: \"the exact words they used\"` "
                         "and every companion will recognise them next time.")
            await self._post(
                f"☑️ {incident.label} resolved by @{who} {note.lstrip(': ') and '· ' + note.lstrip(': ')}"
                f"\nLogged in `hearth-incident-log`.{learn}")
        elif reply.action == "status":
            await self._post(self._status())
        elif reply.action == "handoff":
            await self._post_handoff(paging_order(self.cfg.siblings, now)[0], None, now)
        elif reply.action == "safe":
            await self._on_safe(who, now)
        elif reply.action == "learn":
            await self._learn(who, reply.incident_id, reply.note, reply.phrases or [], now)
        elif reply.action == "drill":
            alert = Alert(
                severity=reply.severity, kind=reply.kind, ref=f"drill-{int(time.time())}",
                summary=f"DRILL started by @{who}. Nothing is wrong; practise the ack.",
            )
            await self._open(alert, drill=True)

    # ── the "your child is in trouble" shield ────────────────────────────

    def _seen(self, handle: str, now: datetime) -> None:
        if self.cfg.sibling_by_handle(handle):
            self.state.setdefault("last_seen", {})[handle] = now.isoformat()
            self._save_state()

    def _last_seen(self, handle: str) -> datetime | None:
        raw = (self.state.get("last_seen") or {}).get(handle)
        return datetime.fromisoformat(raw) if raw else None

    async def _shield(self, incident: Incident, alert: Alert) -> None:
        """A caller says one of the children is detained or hurt. Ask that child,
        and tell the parent what we know, without guessing."""
        about = alert.about.lower()
        named = [s for s in self.cfg.siblings if s.name.lower() in about] or list(self.cfg.siblings)
        now = _utcnow()
        recent = timedelta(minutes=self.cfg.shield.recent_minutes)
        lines = [f"🛡️ {incident.label}: a caller told {self.cfg.parent.name} that "
                 f"{' / '.join(s.name for s in named)} is in trouble. This is a common scam opener."]
        just_seen: tuple[Sibling, int] | None = None
        for sibling in named:
            seen = self._last_seen(sibling.handle)
            if seen:
                minutes = max(1, int((now - seen).total_seconds() // 60))
                lines.append(f"- @{sibling.handle}: last active here {minutes} min ago.")
                if now - seen <= recent and just_seen is None:
                    just_seen = (sibling, minutes)
            else:
                lines.append(f"- @{sibling.handle}: not seen here yet.")
            self.state.setdefault("shield", {})[sibling.handle] = incident.id
        self._save_state()
        lines.append("If that's you and you're fine, reply `safe` (just that word) and I'll tell her right away.")
        await self._targeted("\n".join(lines), [s.handle for s in named])
        if just_seen and not incident.drill:
            sibling, minutes = just_seen
            await self._say_to_parent("relative_seen", name=sibling.name, minutes=str(minutes))

    async def _on_safe(self, who: str, now: datetime) -> None:
        sibling = self.cfg.sibling_by_handle(who)
        if sibling is None:
            return
        pending = (self.state.get("shield") or {}).pop(who, None)
        self._save_state()
        incident = self.book.get(pending) if pending else next(
            (i for i in reversed(self.book.unresolved()) if i.kind == "relative-in-trouble"), None)
        if incident is None:
            await self._post(f"Thanks @{who}. No open 'in trouble' alert right now.")
            return
        if not incident.drill:
            await self._say_to_parent("relative_safe", name=sibling.name)
        if incident.state == "open":
            self.book.ack(who, incident.id, now)
            self._changed()
        await self._post(f"✅ Told {self.cfg.parent.call_name} that {sibling.name} is safe ({incident.label}). "
                         f"Please call her when you can, then `resolved {incident.id}`.")

    # ── learning scam patterns ───────────────────────────────────────────

    async def _learn(self, who: str, incident_id: int | None, note: str, phrases: list[str], now: datetime) -> None:
        incident = self.book.get(incident_id) if incident_id is not None else next(
            (i for i in reversed(self.book.incidents) if i.kind in _SCAM_KINDS and not i.drill), None)
        if incident is None:
            await self._post("Which alert? Reply `learn <number>: \"the exact words they used\"`.")
            return
        if not phrases:
            await self._post(f"Put the caller's exact words in quotes, e.g. "
                             f"`learn {incident.id}: \"your electricity will be cut tonight\"`.")
            return
        items = learned.load(self.learned_path)
        description = (incident.why or incident.summary or incident.kind).rstrip(".") + "."
        items.append(learned.LearnedScam(
            day=now.astimezone(self.cfg.parent.timezone).date().isoformat(),
            incident=incident.label, description=description, phrases=phrases, by=who,
        ))
        learned.save(self.learned_path, items)
        updated = await self._refresh_scam_documents()
        quoted = ", ".join(f'"{p}"' for p in phrases)
        where = "both rooms" if updated == 2 else "the family room" if updated == 1 else "my notes (documents not updated)"
        await self._post(f"🛡️ Added to `hearth-scam-patterns` in {where}: {quoted}. Every companion treats "
                         "these words as HIGH from its next document refresh (within 2 minutes).")

    async def _refresh_scam_documents(self) -> int:
        """Rewrite hearth-scam-patterns in both rooms. Returns how many were updated."""
        if not self.rooms:
            return 0
        content = scam_patterns_text(self.cfg, self.learned_path)
        updated = 0
        family_doc = self.rooms.family_docs.get("hearth-scam-patterns")
        if family_doc:
            try:
                await self.conn.op("update_room_document", {"document_id": family_doc, "content": content})
                updated += 1
            except SwitchError as exc:
                log.warning("could not update the family room's scam patterns: %s", exc.detail)
        parent_doc = self.rooms.parent_docs.get("hearth-scam-patterns")
        if parent_doc:
            # Room documents are edited through a connection bound to their room. The main
            # connection must stay on the family room, so open a short second one.
            side = AgentConnection(self.api, rooms=[self.parent_room_id])
            task = asyncio.create_task(side.run(_ignore))
            try:
                await asyncio.wait_for(side.ready.wait(), 10)
                await side.op("connect_to_room", {"room_id": self.parent_room_id})
                await side.op("update_room_document", {"document_id": parent_doc, "content": content})
                updated += 1
            except (SwitchError, TimeoutError) as exc:
                log.warning("could not update the parent room's scam patterns: %s", exc)
            finally:
                side.stop()
                task.cancel()
        return updated

    # ── shift handoff ────────────────────────────────────────────────────

    async def _handoff_loop(self) -> None:
        while True:
            try:
                now = _utcnow()
                first = paging_order(self.cfg.siblings, now)[0]
                previous = self.state.get("on_call")
                if previous != first.handle:
                    self.state["on_call"] = first.handle
                    self._save_state()
                    if previous is not None:
                        await self._post_handoff(first, self.cfg.sibling_by_handle(previous), now)
            except Exception as exc:  # noqa: BLE001
                log.warning("handoff check failed: %s", exc)
            await asyncio.sleep(30)

    async def _post_handoff(self, incoming: Sibling, outgoing: Sibling | None, now: datetime) -> None:
        hours = self.cfg.handoff_hours
        messages = await self._parent_messages(now - timedelta(hours=hours))
        text = build_handoff(self.cfg, incoming=incoming, outgoing=outgoing, messages=messages,
                             incidents=self.book.incidents, now=now, hours=hours, addressed=True)
        await self._targeted(text, [incoming.handle])

    async def _parent_messages(self, since: datetime) -> list[ParentMessage]:
        """The parent's own messages since then, read from her room's history."""
        try:
            context = await self.conn.op("read_context", {
                "room_id": self.parent_room_id, "limit": 200, "since": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
        except SwitchError as exc:
            log.warning("could not read the parent's room: %s", exc.detail)
            return []
        agents: set[str] = set()
        if self.parent_people:
            await self.parent_people.refresh(max_age=300)
            agents = self.parent_people.agent_names | self.parent_people.agent_labels
        messages = []
        for thread in context.get("threads", []):
            for entry in [thread.get("root") or {}, *(thread.get("replies") or [])]:
                if entry.get("kind") != "message" or not entry.get("body"):
                    continue
                if str(entry.get("sender_name") or "").lower() in agents:
                    continue
                at = datetime.fromtimestamp(int(entry.get("timestamp") or 0) / 1000, timezone.utc)
                messages.append(ParentMessage(at=at, text=str(entry["body"])))
        return sorted(messages, key=lambda m: m.at)

    async def _say_to_parent(self, key: str, **values: str) -> None:
        text = phrase(self.cfg.parent.template_style, key, call=self.cfg.parent.call_name, **values)
        try:
            await self.api.post_message(self.parent_room_id, text)
        except SwitchError as exc:
            log.warning("could not tell the parent: %s", exc)

    def _handle_for(self, sender_name: str) -> str:
        """Map the sender's name in an event to a sibling's handle. Chat apps
        often report a display name ("Shamanth MS") rather than the username."""
        name = sender_name.lstrip("@").strip()
        lowered = name.lower()
        sibling = self.cfg.sibling_by_handle(name) or next(
            (s for s in self.cfg.siblings if s.name.lower() == lowered), None
        ) or next(
            (s for s in self.cfg.siblings
             if s.handle.lower() in lowered or lowered.split(" ")[0] == s.name.lower()), None
        )
        return sibling.handle if sibling else name

    async def _ambiguous(self, action: str, incident_id: int | None) -> None:
        open_ids = ", ".join(i.label for i in self.book.unresolved()) or "none"
        if incident_id is not None:
            await self._post(f"I can't {action} ESC-{incident_id}: it isn't open. Open now: {open_ids}.")
        elif not self.book.unresolved():
            await self._post(f"Nothing to {action}: no open incidents.")
        else:
            await self._post(f"Which one? Open now: {open_ids}. Reply `{action} <number>`.")

    # ── incidents and paging ─────────────────────────────────────────────

    async def _open(self, alert: Alert, *, drill: bool = False) -> None:
        incident, created = self.book.open(alert, _utcnow(), drill=drill)
        if not created:
            return  # both companions reported the same message
        log.info("opened %s (%s %s)", incident.label, incident.sev.name, incident.kind)
        self._changed()
        if incident.sev <= Severity.LOW:
            await self._post(f"🟢 Noted for the digest: {alert.summary or alert.kind} (ref {alert.ref}).")
            return
        await self._send_pages(self.book.due(_utcnow()))
        if incident.kind == "relative-in-trouble":
            await self._shield(incident, alert)

    async def _send_pages(self, actions: list[PageAction]) -> None:
        for action in actions:
            incident = action.incident
            timeout = self.cfg.escalation.timeout_for(incident.sev.label, self.demo)
            drill = " · DRILL" if incident.drill else ""
            parent = self.cfg.parent.name
            if action.step == 0:
                lines = [f"{_ICON.get(incident.sev, '🔔')} {incident.label} · {incident.sev.name} · {incident.kind}{drill}"]
                if incident.summary:
                    lines.append(incident.summary)
                if incident.parent_said:
                    lines.append(f'{parent} said: "{incident.parent_said}"')
                if incident.told_parent:
                    lines.append(f'Hearth told her: "{incident.told_parent}"')
                if incident.why:
                    lines.append(f"Why: {incident.why}")
                if action.everyone:
                    lines.append(f"Paging everyone: possible emergency. Call {parent} now, then reply "
                                 f"`ack {incident.id}`.")
                else:
                    nxt = incident.order[1] if len(incident.order) > 1 else None
                    then = f" If nobody acks in {timeout}s I'll page @{nxt}." if nxt else ""
                    first = action.handles[0] if action.handles else "on duty"
                    lines.append(f"First on the rota: @{first}, please call {parent} now and reply "
                                 f"`ack {incident.id}`.{then}")
                body = "\n".join(lines)
            elif action.everyone:
                body = f"{incident.label}: {action.reason}. Whoever is free, call {parent} and reply `ack {incident.id}`."
            else:
                body = f"{incident.label}: {action.reason}. Your turn: call {parent}, then reply `ack {incident.id}`."
            targets = ["everyone"] if action.everyone and action.step > 0 else action.handles
            event_id = await self._targeted(body, targets, thread_id=None)
            if action.step == 0 and event_id:
                incident.thread_id = event_id
            log.info("%s page %d -> %s", incident.label, action.step, targets)
        if actions:
            self._changed()

    async def _tell_parent(self, handle: str, drill: bool) -> None:
        if drill:
            return
        sibling = self.cfg.sibling_by_handle(handle)
        await self._say_to_parent("family_calling", name=sibling.name if sibling else handle)

    def _status(self) -> str:
        lines = ["📋 Hearth status", "Paging order now:", describe(self.cfg.siblings)]
        open_now = self.book.unresolved()
        if open_now:
            lines.append("Open:")
            for incident in open_now:
                lines.append(f"- {incident.label} {incident.sev.name} {incident.kind}: {incident.state}"
                             + (f" by @{incident.acked_by}" if incident.acked_by else ""))
        else:
            lines.append("No open incidents.")
        return "\n".join(lines)

    # ── loops ────────────────────────────────────────────────────────────

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(2)
            try:
                await self._send_pages(self.book.due(_utcnow()))
                if self._dirty_log and time.monotonic() - self._last_log_write > 10:
                    await self._write_incident_log()
            except Exception as exc:  # noqa: BLE001 - paging must survive a server hiccup
                log.warning("tick failed: %s", exc)

    async def _write_incident_log(self) -> None:
        self._dirty_log = False
        self._last_log_write = time.monotonic()
        content = (
            f"# Incident log for {self.cfg.parent.name}\n\n"
            "Written by hearth-pulse. Newest first. Times are UTC.\n\n" + self.book.to_markdown()
        )
        doc_id = self.state.get("incident_log_id")
        try:
            if doc_id:
                await self.conn.op("update_room_document", {"document_id": doc_id, "content": content})
                return
        except SwitchError as exc:
            log.info("incident log document missing, recreating: %s", exc.detail)
        try:
            result = await self.conn.op("create_room_document", {
                "name": "hearth-incident-log",
                "description": "Every Hearth alert: who was paged, who acknowledged, how it ended",
                "instructions": "Read this before answering questions about past alerts or writing a digest.",
                "content": content,
            })
            self.state["incident_log_id"] = result.get("document_id")
            self._save_state()
        except SwitchError as exc:
            log.warning("could not write the incident log: %s", exc.detail)

    async def _watchdog_loop(self) -> None:
        """Page the family if no companion holds the role: nobody would answer the parent."""
        limit = timedelta(seconds=self.cfg.escalation.companion_offline_seconds)
        while True:
            await asyncio.sleep(5)
            try:
                detail = await self.api.op("get_room_detail", {"room_id": self.parent_room_id})
            except Exception as exc:  # noqa: BLE001
                log.debug("watchdog: %s", exc)
                continue
            role = next((r for r in detail.get("roles") or [] if r.get("name") == self.cfg.agents.companion_role), None)
            if role is None:
                continue
            holders = [h.get("name") if isinstance(h, dict) else h for h in role.get("held_by") or []]
            now = _utcnow()
            if holders:
                self._offline_since = None
                closed = self.book.resolve_kind("companion-offline", self.cfg.agents.pulse,
                                                f"back online: {holders[0]}", now)
                if closed:
                    self._changed()
                    await self._post(f"🟢 {holders[0]} is answering {self.cfg.parent.call_name} again. "
                                     f"Closed {', '.join(i.label for i in closed)}.")
                continue
            self._offline_since = self._offline_since or now
            if now - self._offline_since >= limit and not any(
                i.kind == "companion-offline" for i in self.book.unresolved()
            ):
                await self._open(Alert(
                    severity=Severity.HIGH, kind="companion-offline", ref=f"offline-{int(now.timestamp())}",
                    summary=(f"No companion holds the `{self.cfg.agents.companion_role}` role, so nobody is "
                             f"answering {self.cfg.parent.call_name}. Start `hearth-companion` on any laptop, "
                             "or call her."),
                    why=f"role empty for {int((now - self._offline_since).total_seconds())}s",
                ))

    async def _digest_loop(self) -> None:
        digest = self.cfg.digest
        while True:
            await asyncio.sleep(30)
            local = _utcnow().astimezone(digest.timezone)
            key = f"digest:{local:%Y-%W}"
            due = local.replace(hour=digest.time.hour, minute=digest.time.minute, second=0, microsecond=0)
            if local.weekday() != digest.weekday or not (due <= local < due + timedelta(hours=3)):
                continue
            if self.state.get(key):
                continue
            self.state[key] = True
            self._save_state()
            try:
                await self._ask_for_digest()
            except Exception as exc:  # noqa: BLE001
                log.warning("digest request failed: %s", exc)

    async def _ask_for_digest(self) -> None:
        week_ago = _utcnow() - timedelta(days=7)
        recent = [i for i in self.book.incidents
                  if datetime.fromisoformat(i.opened_at) >= week_ago and not i.drill]
        summary = "; ".join(f"{i.label} {i.kind} ({i.state})" for i in recent) or "no incidents"
        await self._targeted(
            f"please post the weekly digest for {self.cfg.parent.name}: the last 7 days of daily logs, "
            f"care-plan changes and decisions, and this week's incidents ({summary}). Keep it to 10 lines, "
            "and end with the open follow-ups and who owns each.",
            [self.cfg.agents.scribe],
        )

    # ── posting ──────────────────────────────────────────────────────────

    async def _post(self, text: str) -> None:
        try:
            await self.api.post_message(self.family_room_id, text)
        except SwitchError as exc:
            log.error("could not post to the family room: %s", exc)

    async def _targeted(self, body: str, names: list[str], *, thread_id: str | None = None) -> str | None:
        """Address people or agents so their apps notify them. Falls back to a plain post."""
        args: dict[str, Any] = {"body": body, "target_names": names}
        if thread_id:
            args["thread_id"] = thread_id
        try:
            result = await self.conn.op("send_targeted_message", args)
            return (result or {}).get("event_id")
        except SwitchError as exc:
            log.warning("targeted message failed (%s); posting plainly", exc.detail)
            mentions = " ".join(f"@{n}" for n in names)
            await self._post(f"{mentions} {body}")
            return None


def _parse_args(argv: list[str] | None, *, setup_only: bool) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Set up Hearth's rooms." if setup_only else "Run hearth-pulse on the family desk."
    )
    parser.add_argument("--bridge", help="Discord connection name, to create the family room from the template")
    parser.add_argument("--participants", action="store_true", help="list the people Switch sees in each room")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


async def _setup(cfg: HearthConfig, args: argparse.Namespace) -> None:
    api = SwitchAPI(AgentCredentials.load(cfg.agents.pulse))
    try:
        if not await api.health():
            raise ConfigError(f"Switch is not answering at {api.base_url}/health")
        pulse = Pulse(cfg, api, bridge=args.bridge)
        await pulse.setup()
        print(f"Parent room: {cfg.rooms.parent_room} ({pulse.parent_room_id})")
        print(f"Paging order now: {', '.join(s.handle for s in paging_order(cfg.siblings))}")
        print(f"Family room: {cfg.rooms.family_room} ({pulse.family_room_id})")
        if args.participants:
            for label, room_id in (("parent", pulse.parent_room_id), ("family", pulse.family_room_id)):
                people = RoomPeople(api, room_id)
                await people.refresh()
                print(f"{label} room people: {', '.join(people.handles()) or '(none yet)'}")
                print(f"{label} room agents: {', '.join(sorted(people.agent_names)) or '(none)'}")
            print("Use the people names as `handle` in config/hearth.yaml.")
        print("Setup done. Start hearth-pulse, then a companion on each laptop.")
    finally:
        await api.close()


async def _run(cfg: HearthConfig, args: argparse.Namespace) -> None:
    api = SwitchAPI(AgentCredentials.load(cfg.agents.pulse))
    try:
        await Pulse(cfg, api, bridge=args.bridge).run()
    finally:
        await api.close()


def _entry(argv: list[str] | None, *, setup_only: bool) -> None:
    args = _parse_args(argv, setup_only=setup_only)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        cfg = load_config()
        asyncio.run(_setup(cfg, args) if setup_only or args.participants else _run(cfg, args))
    except (ConfigError, FileNotFoundError) as exc:
        raise SystemExit(f"hearth: {exc}") from None
    except SwitchError as exc:
        raise SystemExit(f"hearth: {exc}") from None
    except KeyboardInterrupt:
        pass


def main(argv: list[str] | None = None) -> None:
    _entry(argv, setup_only=False)


def setup_main(argv: list[str] | None = None) -> None:
    _entry(argv, setup_only=True)


if __name__ == "__main__":
    main()
