"""hearth-companion: the agent the parent talks to, in her Telegram group.

Two copies run, one on each sibling's laptop (hearth-companion-a and -b).
Both sit in the parent's room. Only the one holding the room's exclusive
`companion` role answers. Switch ties that role to a heartbeat lease: when
the holder's laptop closes, its connection stops answering pings, the lease
lapses within seconds, and the standby takes the role. The parent keeps
talking to "Hearth" and never notices.

Safety first: every message from the parent goes through the deterministic
triage in hearth.triage before the LLM is asked anything. Emergencies and
scams get a fixed, immediate reply and an alert to the family room.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from hearth import learned
from hearth.companion.prompt import summary_prompt, system_prompt
from hearth.config import STATE_DIR, ConfigError, HearthConfig, env, load_config
from hearth.llm import ChatModel, LLMError, LLMSettings, image_part
from hearth.speech import Transcriber, Transcript, TranscriptionError, is_audio
from hearth.phrases import phrase
from hearth.protocol_text import Alert, format_alert, format_care_update
from hearth.rota import paging_order
from hearth.switch.client import (
    AgentConnection,
    AgentCredentials,
    Frame,
    MessageEvent,
    SwitchAPI,
    SwitchError,
    find_room_id,
)
from hearth.switch.people import RoomPeople
from hearth.triage import Assessment, Severity, assess

log = logging.getLogger("hearth.companion")

_IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif", "image/heic")
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_AUDIO_BYTES = 20 * 1024 * 1024  # Telegram's own limit for files a bot receives
_HISTORY_TURNS = 16
_SCAM_KINDS = {"scam", "relative-in-trouble", "money-request"}


def _squash(name: str) -> str:
    """"Shreya BJ", "@ShreyaBJ" and "shreyabj" are the same person."""
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _safety_reply(style: str, severity: Severity, kind: str, values: dict[str, str]) -> str | None:
    """The fixed reply for the cases where speed and exact wording matter most."""
    if severity >= Severity.RED:
        return phrase(style, "emergency", **values)
    if severity >= Severity.HIGH and kind == "relative-in-trouble":
        return phrase(style, "relative", **values)
    if severity >= Severity.HIGH and kind == "scam":
        return phrase(style, "scam", **values)
    return None


@dataclass
class Turn:
    who: str  # "parent" or "hearth"
    text: str
    at: datetime


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Companion:
    def __init__(self, cfg: HearthConfig, api: SwitchAPI, model: ChatModel | None) -> None:
        self.cfg = cfg
        self.api = api
        self.model = model
        self.me = api.name
        self.role = cfg.agents.companion_role
        self.holder = False
        self.parent_room_id = ""
        self.family_room_id = ""
        self.conn: AgentConnection | None = None
        self.people: RoomPeople | None = None
        self.room_instructions = ""
        self.house_rules = ""
        self.history: list[Turn] = []
        self.day_log: list[str] = []
        self.last_holder: str | None = None
        self.last_holder_seen: datetime | None = None
        self.last_parent_message_at: datetime | None = None
        self.pending_checkin: datetime | None = None
        self.pending_quiet: datetime | None = None
        self.learned_phrases: list[str] = []
        self._doc_ids: list[str] = []
        self.transcriber: Transcriber | None = None
        self.state_path = STATE_DIR / "state" / f"{self.me}.json"
        self.done: set[str] = self._load_done()

    # ── state ────────────────────────────────────────────────────────────

    def _load_done(self) -> set[str]:
        if self.state_path.exists():
            try:
                return set(json.loads(self.state_path.read_text(encoding="utf-8")).get("done", []))
            except (ValueError, OSError):
                return set()
        return set()

    def _mark_done(self, key: str) -> None:
        self.done.add(key)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        recent = sorted(self.done)[-200:]
        self.state_path.write_text(json.dumps({"done": recent}, indent=2), encoding="utf-8")

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        if not await self.api.health():
            raise ConfigError(f"Switch is not answering at {self.api.base_url}/health")
        self.parent_room_id = await self._room(self.cfg.rooms.parent_room)
        self.family_room_id = await self._room(self.cfg.rooms.family_room)
        self.people = RoomPeople(self.api, self.parent_room_id)
        self.conn = AgentConnection(
            self.api, rooms=[self.parent_room_id], on_connected=self._on_connected
        )
        log.info("%s starting in %r", self.me, self.cfg.rooms.parent_room)
        await asyncio.gather(
            self.conn.run(self._on_frame),
            self._role_loop(),
            self._schedule_loop(),
            self._documents_loop(),
        )

    async def _room(self, name: str) -> str:
        room_id = await find_room_id(self.api, name)
        if not room_id:
            raise ConfigError(
                f"{self.me} is not a member of a room named {name!r}. Run hearth-setup first."
            )
        return room_id

    async def _on_connected(self) -> None:
        assert self.conn and self.people
        context = await self.conn.op("connect_to_room", {"room_id": self.parent_room_id})
        self.room_instructions = str(context.get("instructions") or "")[-6000:]
        await self._load_documents(context)
        await self.people.refresh()
        await self._seed_history()
        await self._check_role(announce=True)

    async def _load_documents(self, context: dict[str, Any]) -> None:
        """House rules for the prompt, and the scam phrases this family has confirmed."""
        if not self.conn:
            return
        docs = [d for d in context.get("documents") or []
                if any(word in str(d.get("name", "")).lower() for word in ("rule", "scam"))]
        self._doc_ids = [d["id"] for d in docs]
        await self._reload_documents()

    async def _reload_documents(self) -> None:
        if not self.conn or not self._doc_ids:
            return
        try:
            loaded = await self.conn.op("load_internal_documents", {"ids": self._doc_ids})
        except SwitchError:
            return
        rules = [str(d.get("content") or "") for d in loaded or [] if "rule" in str(d.get("name", "")).lower()]
        self.house_rules = "\n\n".join(rules)[:4000]
        phrases: list[str] = []
        for doc in loaded or []:
            if "scam" in str(doc.get("name", "")).lower():
                phrases += learned.phrases_from_document(str(doc.get("content") or ""))
        if phrases != self.learned_phrases:
            log.info("scam phrases this family confirmed: %d", len(phrases))
        self.learned_phrases = phrases

    async def _documents_loop(self) -> None:
        """Pick up newly learned scam phrases without a restart."""
        while True:
            await asyncio.sleep(120)
            if not self.conn or not self.conn.ready.is_set():
                continue
            try:
                await self._reload_documents()
            except Exception as exc:  # noqa: BLE001
                log.debug("document refresh failed: %s", exc)

    async def _seed_history(self) -> None:
        """Read recent room history so a companion that just took over knows the conversation."""
        if self.history or not self.conn or not self.people:
            return
        try:
            context = await self.conn.op("read_context", {"limit": 30})
        except SwitchError:
            return
        for thread in context.get("threads", []):
            for entry in [thread.get("root") or {}, *(thread.get("replies") or [])]:
                if entry.get("kind") != "message" or not entry.get("body"):
                    continue
                name = str(entry.get("sender_name") or "").lower()
                who = "hearth" if name in self.people.agent_names | self.people.agent_labels else "parent"
                at = datetime.fromtimestamp(int(entry.get("timestamp") or 0) / 1000, timezone.utc)
                self.history.append(Turn(who, str(entry["body"]), at))
                if who == "parent" and (self.last_parent_message_at is None or at > self.last_parent_message_at):
                    # Survives a restart or a takeover: the quiet check must not fire
                    # just because this process didn't see her morning message.
                    self.last_parent_message_at = at
        self.history.sort(key=lambda t: t.at)
        self.history = self.history[-_HISTORY_TURNS:]

    # ── the role ─────────────────────────────────────────────────────────

    async def _role_entry(self) -> dict[str, Any] | None:
        assert self.conn
        roles = await self.conn.op("list_roles")
        return next((r for r in roles or [] if r.get("name") == self.role), None)

    @staticmethod
    def _holders(entry: dict[str, Any]) -> list[str]:
        out = []
        for holder in entry.get("held_by") or []:
            out.append(str(holder.get("name") if isinstance(holder, dict) else holder))
        return out

    async def _check_role(self, *, announce: bool = False) -> None:
        if not self.conn or not self.conn.ready.is_set():
            return
        try:
            entry = await self._role_entry()
        except SwitchError as exc:
            log.warning("list_roles failed: %s", exc)
            return
        if entry is None:
            # No role defined (setup not run). Fall back to the first companion answering.
            primary = self.cfg.agents.companions[0]
            if self.holder != (self.me == primary):
                log.warning("role %r is not defined in the room; run hearth-setup", self.role)
            self.holder = self.me == primary
            return
        holders = self._holders(entry)
        if self.me in holders:
            if not self.holder:
                log.info("holding the %s role", self.role)
            self.holder = True
            return
        if self.holder:
            log.warning("lost the %s role (holders now: %s)", self.role, holders or "nobody")
        self.holder = False
        if holders:
            if holders[0] != self.last_holder:
                log.info("standing by: the %s role is held by %s", self.role, holders[0])
            self.last_holder = holders[0]
            self.last_holder_seen = _utcnow()
            return
        await self._take_role(announce=announce)

    async def _take_role(self, *, announce: bool) -> None:
        assert self.conn
        try:
            await self.conn.op("assume_role", {"role": self.role})
        except SwitchError as exc:
            detail = str(exc.detail).lower()
            if "already hold" in detail:
                await self.conn.op("release_role")
                await self.conn.op("assume_role", {"role": self.role})
            else:
                log.info("could not take the %s role yet: %s", self.role, exc.detail)
                return
        self.holder = True
        previous = self.last_holder
        log.info("took the %s role (previous holder: %s)", self.role, previous or "none")
        call = self.cfg.parent.call_name
        if previous and previous != self.me:
            since = self.last_holder_seen.astimezone(self.cfg.parent.timezone) if self.last_holder_seen else None
            when = f" (last seen {since:%H:%M} {self.cfg.parent.timezone.key})" if since else ""
            await self._post_family(
                f"🔁 {self.me} took over {call}'s chat: {previous} went offline{when}. "
                f"Switch released its `{self.role}` role and this laptop picked it up. "
                f"Nothing changes for {call}: same chat, same Hearth."
            )
        elif announce:
            await self._post_family(f"🟢 {self.me} is online and answering {call} (holds the `{self.role}` role).")

    async def _role_loop(self) -> None:
        """Standby: look for a free role every 3 s. Holder: confirm it every 15 s."""
        tick = 0
        while True:
            await asyncio.sleep(3)
            tick += 1
            if self.holder and tick % 5:
                continue
            try:
                await self._check_role()
            except Exception as exc:  # noqa: BLE001 - keep checking through server hiccups
                log.warning("role check failed: %s", exc)

    # ── events ───────────────────────────────────────────────────────────

    async def _on_frame(self, frame: Frame) -> None:
        if frame.event == "room_join" and self.people:
            await self.people.refresh()
            return
        if frame.event != "message":
            return
        event = MessageEvent.from_frame(frame.data, frame.sequence)
        if event.room_id != self.parent_room_id or not self.people:
            return
        await self.people.refresh(max_age=300)
        now = _utcnow()
        if self.people.is_agent(event):
            # Keep the other companion's and pulse's messages, so a takeover has
            # the whole conversation. Our own posts come back too; skip those.
            if event.body and not any(t.text == event.body for t in self.history[-3:]):
                self.history.append(Turn("hearth", event.body, now))
            return
        handle = _squash(self.cfg.parent.handle)
        if handle and _squash(event.sender_name) != handle:
            return  # a sibling or someone else in the group, not the parent
        if event.body.startswith(("!", "/")):
            return  # a Switch command, handled by Switch
        self.last_parent_message_at = now
        self.pending_checkin = None
        self.pending_quiet = None
        self.history.append(Turn("parent", event.body or "[attachment]", now))
        self.history = self.history[-_HISTORY_TURNS:]
        if not self.holder:
            await self._check_role()
            if not self.holder:
                return
        await self._respond(event)

    async def _respond(self, event: MessageEvent) -> None:
        cfg = self.cfg
        now = _utcnow()
        order = paging_order(cfg.siblings, now)
        on_duty = order[0]
        values = {
            "call": cfg.parent.call_name,
            "name": on_duty.name,
            "names": " and ".join(s.name for s in cfg.siblings),
            "ambulance": cfg.parent.ambulance_number,
            "emergency": cfg.parent.emergency_number,
        }
        style = cfg.parent.template_style
        names = [s.name for s in cfg.siblings]
        images = await self._attachments(event, kind="image")
        audio = await self._attachments(event, kind="audio")

        # Voice notes: transcribe first, so a spoken emergency gets the same triage as a typed one.
        text, heard, voice_failed = event.body, "", False
        if audio:
            transcript = await self._transcribe(audio[0])
            if transcript:
                heard = transcript.text
                text = "\n".join(filter(None, [event.body, transcript.for_triage]))
                if self.history and self.history[-1].who == "parent":
                    self.history[-1].text = f"🎤 {heard}"
            else:
                voice_failed = True

        found = assess(text, family_names=names, learned_phrases=self.learned_phrases)
        severity, kind = found.severity, found.primary_kind
        why = "; ".join(found.reasons)
        about = ", ".join(found.about)
        summary = ""
        care: dict[str, Any] | None = None

        reply = _safety_reply(style, severity, kind, values)
        if voice_failed and severity < Severity.MEDIUM:
            # A voice note nobody understood could be anything: a person listens.
            severity, kind = Severity.MEDIUM, "voice-note"
            why = "a voice note Hearth couldn't transcribe"
            summary = f"{cfg.parent.name} sent a voice note I couldn't understand. Please listen to it below."
            reply = phrase(style, "voice_failed", **values)

        if reply is None:
            out = await self._ask_model(text, images, found, on_duty.name, voice=bool(heard))
            if out:
                reply = str(out.get("reply") or "").strip() or None
                model_severity = Severity.parse(out.get("severity"))
                if model_severity > severity:  # the one-way ratchet: the model can only raise it
                    severity = model_severity
                    kind = str(out.get("kind") or kind)
                    why = "; ".join(filter(None, [why, f"the model rated it {model_severity.label}"]))
                summary = str(out.get("family_summary") or "")
                care = out.get("care_update") if isinstance(out.get("care_update"), dict) else None
                if out.get("log"):
                    self.day_log.append(f"{self._local(now):%H:%M} {out['log']}")
            reply = _safety_reply(style, severity, kind, values) or reply
            if severity >= Severity.MEDIUM and kind == "money-request" and not reply:
                reply = phrase(style, "money", **values)
            if not reply:
                reply = phrase(style, "photo" if images else "received", **values)

        await self._post_parent(reply)
        self.history.append(Turn("hearth", reply, _utcnow()))
        said = (f"🎤 {heard}" if heard else event.body) or (
            "[photo]" if images else "[voice note]" if audio else "")
        if not any(said in line for line in self.day_log[-1:]):
            self.day_log.append(f"{self._local(now):%H:%M} {cfg.parent.name}: {said[:200]} ({severity.label})")

        ref = self._ref(event)
        scam_like = kind in _SCAM_KINDS
        if severity >= Severity.MEDIUM:
            alert = Alert(
                severity=severity, kind=kind, ref=ref, parent_said=said,
                told_parent=reply, why=why, summary=summary, about=about,
            )
            await self._post_family(format_alert(alert, cfg.parent.name))
            evidence = (images if scam_like else []) + audio
            if evidence:
                await self._relay(evidence, f"From {cfg.parent.name} · ref {ref}")
        # The care plan changes only for photos (prescriptions, reports) or a medicine or
        # appointment change she describes. Symptoms go to the daily log, not the scribe.
        care_worthy = care is not None and str(care.get("topic") or "").lower() in {"meds", "appointment"}
        if not scam_like and (images or care_worthy):
            await self._send_care_update(ref, care, images, on_duty.handle)

    async def _transcribe(self, audio: tuple[str, bytes, str]) -> Transcript | None:
        if self.transcriber is None or not self.transcriber.available:
            log.warning("a voice note arrived but no speech-to-text is configured")
            return None
        name, data, mime = audio
        try:
            transcript = await self.transcriber.transcribe(data, mime, name)
        except TranscriptionError as exc:
            log.warning("could not transcribe the voice note: %s", exc)
            return None
        log.info("voice note transcribed by %s (%d chars)", transcript.engine, len(transcript.text))
        return transcript

    async def _relay(self, files: list[tuple[str, bytes, str]], caption: str) -> None:
        try:
            await self.api.upload_media(self.family_room_id, files, caption=caption)
        except SwitchError as exc:
            log.warning("could not relay the attachment: %s", exc)

    async def _ask_model(
        self,
        text: str,
        images: list[tuple[str, bytes, str]],
        found: Assessment,
        on_duty_name: str,
        *,
        voice: bool = False,
    ) -> dict[str, Any] | None:
        if self.model is None:
            return None
        now_local = self._local(_utcnow())
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": system_prompt(
                    self.cfg,
                    now_local=now_local,
                    on_duty_name=on_duty_name,
                    room_instructions=self.room_instructions,
                    house_rules=self.house_rules,
                ),
            }
        ]
        for turn in self.history[-_HISTORY_TURNS:-1]:
            messages.append({"role": "user" if turn.who == "parent" else "assistant", "content": turn.text})
        notes = []
        if voice:
            notes.append("(This came as a voice note; the text is a transcript.)")
        if found.reasons:
            notes.append(f"(Hearth's safety check flagged: {'; '.join(found.reasons)})")
        prompt_text = (text or "(she sent a photo)") + ("\n\n" + "\n".join(notes) if notes else "")
        if images:
            content: Any = [{"type": "text", "text": prompt_text}] + [
                image_part(data, mime) for _, data, mime in images]
        else:
            content = prompt_text
        messages.append({"role": "user", "content": content})
        await self.api.set_typing(self.parent_room_id, True)
        try:
            return await self.model.complete_json(messages)
        except LLMError as exc:
            log.warning("LLM failed, using the fixed reply: %s", exc)
            return None
        finally:
            await self.api.set_typing(self.parent_room_id, False)

    async def _attachments(self, event: MessageEvent, *, kind: str) -> list[tuple[str, bytes, str]]:
        """Download the message's images or audio (voice notes) from Switch's media store."""
        found = []
        for attachment in event.attachments[:3]:
            mime = str(attachment.get("mimetype") or "")
            if kind == "image":
                if not mime.startswith(_IMAGE_TYPES) and attachment.get("msgtype") != "m.image":
                    continue
                limit = _MAX_IMAGE_BYTES
            else:
                if not is_audio(attachment):
                    continue
                limit = _MAX_AUDIO_BYTES
            if int(attachment.get("size") or 0) > limit:
                continue
            try:
                media = await self.api.download_media(self.parent_room_id, str(attachment["mxc"]))
            except (SwitchError, KeyError) as exc:
                log.warning("could not download an attachment: %s", exc)
                continue
            found.append((str(attachment.get("filename") or media.filename), media.data, mime or media.content_type))
        return found

    async def _send_care_update(
        self, ref: str, care: dict[str, Any] | None, images: list[tuple[str, bytes, str]], confirm_with: str
    ) -> None:
        topic = str((care or {}).get("topic") or "photo")
        details = str((care or {}).get("details") or "").strip()
        if not details:
            details = "I could not read it reliably. Please look at the photo below."
        body = format_care_update(
            self.cfg.agents.scribe, topic, ref,
            f"From {self.cfg.parent.name}'s chat. What I read:\n{details}", confirm_with,
        )
        await self._post_family(body)
        if images:
            try:
                await self.api.upload_media(
                    self.family_room_id, images, caption=f"Photo from {self.cfg.parent.name} · ref {ref}"
                )
            except SwitchError as exc:
                log.warning("could not relay the photo: %s", exc)

    # ── schedule: check-ins, no-reply alerts, daily summary ──────────────

    async def _schedule_loop(self) -> None:
        while True:
            await asyncio.sleep(20)
            if not self.holder or not self.conn or not self.conn.ready.is_set():
                continue
            try:
                await self._schedule_tick()
            except Exception as exc:  # noqa: BLE001 - one failed tick must not stop check-ins
                log.warning("schedule tick failed: %s", exc)

    async def _schedule_tick(self) -> None:
        cfg = self.cfg
        now = _utcnow()
        local = self._local(now)
        today = local.date().isoformat()
        for slot in cfg.checkins.times:
            key = f"checkin:{today}:{slot:%H:%M}"
            slot_at = local.replace(hour=slot.hour, minute=slot.minute, second=0, microsecond=0)
            if key in self.done or not (slot_at <= local < slot_at + timedelta(hours=2)):
                continue
            self._mark_done(key)
            if self.last_parent_message_at and self.last_parent_message_at >= slot_at.astimezone(timezone.utc):
                continue  # she already wrote today since this slot
            text = phrase(cfg.parent.template_style, "morning" if slot.hour < 12 else "evening",
                          call=cfg.parent.call_name)
            await self._post_parent(text)
            self.history.append(Turn("hearth", text, now))
            self.pending_checkin = now
            log.info("sent the %s check-in", f"{slot:%H:%M}")

        if self.pending_checkin and (
            not self.last_parent_message_at or self.last_parent_message_at < self.pending_checkin
        ):
            waited = now - self.pending_checkin
            if waited >= timedelta(minutes=cfg.checkins.no_reply_minutes):
                sent = self._local(self.pending_checkin)
                alert = Alert(
                    severity=Severity.MEDIUM, kind="no-reply", ref=f"noreply-{sent:%Y%m%d%H%M}",
                    summary=f"{cfg.parent.name} hasn't answered the {sent:%H:%M} check-in "
                            f"({int(waited.total_seconds() // 60)} minutes). Someone please call her.",
                    why="no reply to a check-in",
                )
                await self._post_family(format_alert(alert, cfg.parent.name))
                self.pending_checkin = None

        await self._quiet_check(now, local, today)

        summary_at = local.replace(
            hour=cfg.checkins.daily_summary_time.hour, minute=cfg.checkins.daily_summary_time.minute,
            second=0, microsecond=0,
        )
        key = f"summary:{today}"
        if key not in self.done and summary_at <= local < summary_at + timedelta(hours=2):
            self._mark_done(key)
            await self._post_daily_summary(today)

    async def _quiet_check(self, now: datetime, local: datetime, today: str) -> None:
        """The dead man's switch: no word from her by `expect_by` is a signal too."""
        quiet = self.cfg.checkins.quiet_check
        if quiet is None:
            return
        cfg = self.cfg
        expect_at = local.replace(hour=quiet.expect_by.hour, minute=quiet.expect_by.minute, second=0, microsecond=0)
        since_at = local.replace(hour=quiet.since.hour, minute=quiet.since.minute, second=0, microsecond=0)
        key = f"quiet:{today}"
        if key not in self.done and expect_at <= local < expect_at + timedelta(hours=3):
            self._mark_done(key)
            if not await self._parent_wrote_since(since_at.astimezone(timezone.utc)):
                text = phrase(cfg.parent.template_style, "quiet", call=cfg.parent.call_name)
                await self._post_parent(text)
                self.history.append(Turn("hearth", text, now))
                self.pending_quiet = now
                log.info("quiet check: no message since %s, checked in", f"{quiet.since:%H:%M}")

        if self.pending_quiet and (
            not self.last_parent_message_at or self.last_parent_message_at < self.pending_quiet
        ):
            waited = now - self.pending_quiet
            if waited >= timedelta(minutes=quiet.reply_within_minutes):
                last = (f"{self._local(self.last_parent_message_at):%a %H:%M}"
                        if self.last_parent_message_at else "before this companion started")
                alert = Alert(
                    severity=Severity.MEDIUM, kind="no-signal", ref=f"quiet-{today}",
                    summary=(f"No word from {cfg.parent.name} today (last message: {last}), and no answer "
                             f"to the {quiet.expect_by:%H:%M} check-in for {int(waited.total_seconds() // 60)} "
                             "minutes. Not an emergency: please call her."),
                    why="her usual morning message didn't come",
                )
                await self._post_family(format_alert(alert, cfg.parent.name))
                self.pending_quiet = None

    async def _parent_wrote_since(self, since_utc: datetime) -> bool:
        """Did a person write in her room since then? Asks Switch, so it holds after a restart."""
        if self.last_parent_message_at and self.last_parent_message_at >= since_utc:
            return True
        if not self.conn or not self.people:
            return False
        try:
            context = await self.conn.op(
                "read_context", {"limit": 50, "since": since_utc.strftime("%Y-%m-%dT%H:%M:%SZ")})
        except SwitchError:
            return False
        for thread in context.get("threads", []):
            for entry in [thread.get("root") or {}, *(thread.get("replies") or [])]:
                name = str(entry.get("sender_name") or "").lower()
                if entry.get("kind") == "message" and name not in (
                        self.people.agent_names | self.people.agent_labels):
                    return True
        return False

    async def _post_daily_summary(self, day: str) -> None:
        lines = list(self.day_log)
        text = ""
        if self.model and lines:
            try:
                text = await self.model.complete(summary_prompt(self.cfg, day, lines), temperature=0.2)
            except LLMError as exc:
                log.warning("summary failed: %s", exc)
        if not text:
            text = "\n".join(f"- {line}" for line in lines[-12:]) or "- No messages today."
        await self._post_family(
            f"📒 Today with {self.cfg.parent.call_name} · {day}\n"
            f"@{self.cfg.agents.scribe} please add this to the care log.\n{text.strip()}"
        )
        self.day_log.clear()

    # ── helpers ──────────────────────────────────────────────────────────

    def _local(self, moment: datetime) -> datetime:
        return moment.astimezone(self.cfg.parent.timezone)

    @staticmethod
    def _ref(event: MessageEvent) -> str:
        seed = event.message_id or f"{event.sender}:{event.timestamp_ms}"
        return "m-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8]

    async def _post_parent(self, text: str) -> None:
        await self.api.post_message(self.parent_room_id, text)

    async def _post_family(self, text: str) -> None:
        try:
            await self.api.post_message(self.family_room_id, text)
        except SwitchError as exc:
            log.error("could not post to the family room: %s", exc)


async def _check_llm() -> int:
    """Try each configured model, so you know which one answers before going live."""
    settings = LLMSettings.from_env()
    status = 1
    for candidate in (settings.model, *settings.fallback_models):
        model = ChatModel(LLMSettings(settings.base_url, settings.api_key, candidate))
        try:
            out = await model.complete_json(
                [
                    {"role": "system", "content": 'Reply with JSON: {"reply": "..."}'},
                    {"role": "user", "content": "Say namaskara in one short line."},
                ]
            )
            print(f"OK    {candidate}: {out.get('reply')}")
            status = 0
        except LLMError as exc:
            print(f"FAIL  {candidate}: {exc}")
        finally:
            await model.close()
    speech = Transcriber(language="Kannada and English")
    print("Voice notes:", "configured" if speech.available else "not configured (they will be relayed unheard)")
    await speech.close()
    return status


async def _run(name: str) -> None:
    cfg = load_config()
    if name not in cfg.agents.companions:
        raise ConfigError(f"{name!r} is not listed under agents.companions in the config")
    api = SwitchAPI(AgentCredentials.load(name))
    model: ChatModel | None = None
    if env("HEARTH_LLM_API_KEY"):
        model = ChatModel(LLMSettings.from_env())
    else:
        log.warning("HEARTH_LLM_API_KEY is not set: the companion will use fixed replies only")
    companion = Companion(cfg, api, model)
    companion.transcriber = Transcriber(language=cfg.parent.language)
    if not companion.transcriber.available:
        log.warning("no speech-to-text configured: voice notes will be relayed to the family unheard")
    try:
        await companion.start()
    finally:
        await api.close()
        await companion.transcriber.close()
        if model:
            await model.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Hearth companion agent.")
    parser.add_argument("--name", help="agent name (default: HEARTH_COMPANION_NAME from .env)")
    parser.add_argument("--check-llm", action="store_true", help="test the LLM settings and exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        load_config()  # loads .env as a side effect, and validates early
        if args.check_llm:
            raise SystemExit(asyncio.run(_check_llm()))
        name = args.name or env("HEARTH_COMPANION_NAME", required=True)
        asyncio.run(_run(name))
    except (ConfigError, FileNotFoundError) as exc:
        raise SystemExit(f"hearth-companion: {exc}") from None
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
