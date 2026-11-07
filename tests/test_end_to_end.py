"""Hearth's agents against an in-memory Switch: paging, acks, failover, scam replies."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

import hearth.companion.agent as companion_module
import hearth.pulse.agent as pulse_module
from hearth.companion.agent import Companion
from hearth.pulse.agent import Pulse
from hearth.switch.client import AgentConnection
from fake_switch import FakeSwitch

PARENT, FAMILY = "room-parent", "room-family"


async def wait_for(predicate, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


@pytest.fixture
async def fake(tmp_path, monkeypatch):
    monkeypatch.setattr(pulse_module, "STATE_DIR", tmp_path)
    monkeypatch.setattr(companion_module, "STATE_DIR", tmp_path)
    monkeypatch.delenv("HEARTH_DEMO", raising=False)
    server = FakeSwitch()
    await server.start()
    server.add_room(PARENT, "Amma Hearth", ["amma"])
    server.add_room(FAMILY, "hearth-family-desk", ["ms_shamanth", "shreyabj"], ["hearth-scribe"])
    yield server
    await server.stop()


@contextlib.asynccontextmanager
async def running(coro):
    task = asyncio.create_task(coro)
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def test_connection_answers_pings(fake):
    api = fake.api(fake.add_agent("hearth-pulse"))
    conn = AgentConnection(api)
    async with running(conn.run(lambda frame: asyncio.sleep(0))):
        await asyncio.wait_for(conn.ready.wait(), 5)
        await wait_for(lambda: sum(fake.pongs.values()) >= 3, 5)
    await api.close()


async def test_setup_then_page_ack_and_tell_parent(fake, cfg):
    api = fake.api(fake.add_agent("hearth-pulse"))
    for name in cfg.agents.companions:
        fake.add_agent(name)
    pulse = Pulse(cfg, api)
    async with running(pulse.run()):
        await wait_for(lambda: any("on the desk" in p.body for p in fake.posts_in(FAMILY, "hearth-pulse")))

        # Setup: members, instructions, the exclusive role, documents, links.
        assert {"hearth-pulse", "hearth-companion-a", "hearth-companion-b"} <= set(fake.rooms[PARENT].agents)
        assert "hearth-scribe" in fake.rooms[FAMILY].agents
        assert fake.rooms[PARENT].roles["companion"]["exclusive"] is True
        assert "Amma" in fake.rooms[PARENT].instructions
        names = {d["name"] for d in fake.rooms[FAMILY].documents.values()}
        assert {"hearth-house-rules", "hearth-red-flags", "hearth-scam-patterns", "hearth-rota"} <= names
        assert fake.rooms[PARENT].links and fake.rooms[FAMILY].links
        assert any("hearth-scribe" in p.targets for p in fake.posts_in(FAMILY, "hearth-pulse"))

        # A companion's alert becomes ESC-1, paged to whoever is first on the rota.
        await fake._record("hearth-companion-a", FAMILY,
                           "🔴 Hearth alert · HIGH · scam · ref m-abc12345\nAmma said: \"CBI called\"")
        await wait_for(lambda: any("ESC-1" in p.body and p.targets for p in fake.posts_in(FAMILY, "hearth-pulse")))
        page = next(p for p in fake.posts_in(FAMILY, "hearth-pulse") if "ESC-1" in p.body)
        first = page.targets[0]
        assert first in {"ms_shamanth", "shreyabj"}

        # The same alert again (the other companion) does not open a second incident.
        await fake._record("hearth-companion-b", FAMILY, "🔴 Hearth alert · HIGH · scam · ref m-abc12345")
        await asyncio.sleep(0.3)
        assert len(pulse.book.incidents) == 1

        # ack stops paging and tells Amma who is calling.
        await fake.say(FAMILY, first, "ack 1")
        await wait_for(lambda: any("paging stopped" in p.body for p in fake.posts_in(FAMILY, "hearth-pulse")))
        await wait_for(lambda: bool(fake.posts_in(PARENT, "hearth-pulse")))
        sibling = cfg.sibling_by_handle(first)
        assert sibling.name in fake.posts_in(PARENT, "hearth-pulse")[-1].body

        await fake.say(FAMILY, first, "resolved 1: she hung up, nothing shared")
        await wait_for(lambda: pulse.book.get(1).state == "resolved")
    await api.close()


async def test_companion_answers_scam_and_fails_over(fake, cfg):
    pulse_api = fake.api(fake.add_agent("hearth-pulse"))
    a_api = fake.api(fake.add_agent("hearth-companion-a"))
    b_api = fake.api(fake.add_agent("hearth-companion-b"))
    pulse = Pulse(cfg, pulse_api)
    await pulse.setup()  # rooms, membership, role

    a = Companion(cfg, a_api, model=None)
    b = Companion(cfg, b_api, model=None)
    task_a = asyncio.create_task(a.start())
    await wait_for(lambda: a.holder, 10)
    task_b = asyncio.create_task(b.start())
    await wait_for(lambda: b.conn is not None and b.conn.ready.is_set(), 10)
    await asyncio.sleep(0.5)
    assert not b.holder

    # A scam gets the fixed safety reply from the holder only, and an alert for the family.
    await fake.say(PARENT, "amma", "A CBI officer called, my Aadhaar is in a money laundering case, "
                                   "they want 2 lakh transferred for verification")
    await wait_for(lambda: bool(fake.posts_in(PARENT, "hearth-companion-a")))
    reply = fake.posts_in(PARENT, "hearth-companion-a")[-1].body
    assert "scam" in reply.lower() and "OTP" in reply
    assert not fake.posts_in(PARENT, "hearth-companion-b")
    await wait_for(lambda: any("Hearth alert · HIGH · scam" in p.body for p in fake.posts_in(FAMILY, "hearth-companion-a")))

    # Close a's "laptop": its lease lapses and b takes the role and announces it.
    task_a.cancel()
    await fake.drop("hearth-companion-a")
    await wait_for(lambda: b.holder, 12)
    await wait_for(lambda: any("took over" in p.body for p in fake.posts_in(FAMILY, "hearth-companion-b")))

    await fake.say(PARENT, "amma", "Good evening, had my dinner")
    await wait_for(lambda: bool(fake.posts_in(PARENT, "hearth-companion-b")))

    task_b.cancel()
    for task in (task_a, task_b):
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    for api in (pulse_api, a_api, b_api):
        await api.close()


async def test_display_names_map_to_handles(fake, cfg):
    api = fake.api(fake.add_agent("hearth-pulse"))
    pulse = Pulse(cfg, api)
    assert pulse._handle_for("ms_shamanth") == "ms_shamanth"
    assert pulse._handle_for("Shamanth MS") == "ms_shamanth"
    assert pulse._handle_for("@ShreyaBJ") == "shreyabj"
    assert pulse._handle_for("Shreya") == "shreyabj"
    assert pulse._handle_for("Lakshmi") == "Lakshmi"
    await api.close()


async def test_shield_learn_and_handoff(fake, cfg):
    api = fake.api(fake.add_agent("hearth-pulse"))
    for name in cfg.agents.companions:
        fake.add_agent(name)
    pulse = Pulse(cfg, api)
    async with running(pulse.run()):
        await wait_for(lambda: any("on the desk" in p.body for p in fake.posts_in(FAMILY, "hearth-pulse")))
        await fake.say(FAMILY, "ms_shamanth", "good morning all")

        # "They detained Shamanth": pulse asks him, and tells Amma he was just active.
        await fake._record("hearth-companion-a", FAMILY,
                           "🔴 Hearth alert · HIGH · relative-in-trouble · ref m-rel00001\n"
                           "Amma said: \"Police called, Shamanth is arrested\"\nAbout: Shamanth")
        await wait_for(lambda: any("🛡️" in p.body for p in fake.posts_in(FAMILY, "hearth-pulse")))
        shield = next(p for p in fake.posts_in(FAMILY, "hearth-pulse") if "🛡️" in p.body)
        assert shield.targets == ["ms_shamanth"] and "last active here" in shield.body
        await wait_for(lambda: any("family chat" in p.body for p in fake.posts_in(PARENT, "hearth-pulse")))

        await fake.say(FAMILY, "ms_shamanth", "safe")
        await wait_for(lambda: any("safe aagiddare" in p.body for p in fake.posts_in(PARENT, "hearth-pulse")))
        incident = pulse.book.by_ref("m-rel00001")
        assert incident.state == "acked" and incident.acked_by == "ms_shamanth"

        # Resolve it, then teach Hearth the caller's words: both rooms' documents change.
        await fake.say(FAMILY, "ms_shamanth", f"resolved {incident.id}: fake police call")
        await wait_for(lambda: any("learn" in p.body and "resolved by" in p.body
                                   for p in fake.posts_in(FAMILY, "hearth-pulse")))
        await fake.say(FAMILY, "ms_shamanth", f'learn {incident.id}: "your son is in police custody"')
        await wait_for(lambda: any("Added to `hearth-scam-patterns` in both rooms" in p.body
                                   for p in fake.posts_in(FAMILY, "hearth-pulse")), 15)
        for room in (PARENT, FAMILY):
            doc = next(d for d in fake.rooms[room].documents.values() if d["name"] == "hearth-scam-patterns")
            assert "your son is in police custody" in doc["content"]

        # A shift handoff on request, built from Amma's own messages.
        await fake.say(PARENT, "amma", "Good morning, BP 128/82, took my tablets")
        await fake.say(FAMILY, "shreyabj", "handoff")
        await wait_for(lambda: any("Shift handoff" in p.body for p in fake.posts_in(FAMILY, "hearth-pulse")))
        handoff = next(p for p in fake.posts_in(FAMILY, "hearth-pulse") if "Shift handoff" in p.body)
        assert "BP 128/82" in handoff.body and "took her medicines" in handoff.body
    await api.close()


class StubTranscriber:
    def __init__(self, transcript=None):
        self.transcript = transcript
        self.available = transcript is not None

    async def transcribe(self, data, mime, name):
        from hearth.speech import TranscriptionError
        if self.transcript is None:
            raise TranscriptionError("nothing configured")
        return self.transcript

    async def close(self):
        pass


async def _one_companion(fake, cfg, transcript=None):
    pulse_api = fake.api(fake.add_agent("hearth-pulse"))
    api = fake.api(fake.add_agent("hearth-companion-a"))
    fake.add_agent("hearth-companion-b")
    await Pulse(cfg, pulse_api).setup()
    companion = Companion(cfg, api, model=None)
    companion.transcriber = StubTranscriber(transcript)
    task = asyncio.create_task(companion.start())
    await wait_for(lambda: companion.holder, 10)
    return companion, task, [pulse_api, api]


async def _stop(task, apis):
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
    for api in apis:
        await api.close()


async def test_voice_note_emergency_is_triaged_and_relayed(fake, cfg):
    from hearth.speech import Transcript
    companion, task, apis = await _one_companion(
        fake, cfg, Transcript(text="ಎದೆ ನೋವು ಆಗ್ತಿದೆ", english="I have chest pain", engine="stub"))
    await fake.say(PARENT, "amma", "", files=[("voice.oga", b"OggS-fake", "audio/ogg")])
    await wait_for(lambda: bool(fake.posts_in(PARENT, "hearth-companion-a")))
    assert "108" in fake.posts_in(PARENT, "hearth-companion-a")[-1].body
    await wait_for(lambda: any("Hearth alert · RED" in p.body for p in fake.posts_in(FAMILY, "hearth-companion-a")))
    alert = next(p for p in fake.posts_in(FAMILY, "hearth-companion-a") if "Hearth alert" in p.body)
    assert "🎤 ಎದೆ ನೋವು ಆಗ್ತಿದೆ" in alert.body
    await wait_for(lambda: any(room == FAMILY and "voice.oga" in names for _a, room, names in fake.uploads))
    await _stop(task, apis)


async def test_unheard_voice_note_goes_to_a_person(fake, cfg):
    companion, task, apis = await _one_companion(fake, cfg, transcript=None)
    await fake.say(PARENT, "amma", "", files=[("voice.oga", b"OggS-fake", "audio/ogg")])
    await wait_for(lambda: any("Hearth alert · MEDIUM · voice-note" in p.body
                               for p in fake.posts_in(FAMILY, "hearth-companion-a")))
    assert "kelisalilla" in fake.posts_in(PARENT, "hearth-companion-a")[-1].body
    await _stop(task, apis)


async def test_relative_in_trouble_gets_the_shield_reply(fake, cfg):
    companion, task, apis = await _one_companion(fake, cfg)
    await fake.say(PARENT, "amma", "Police called, they said Shamanth is arrested, send 50000 for bail")
    await wait_for(lambda: bool(fake.posts_in(PARENT, "hearth-companion-a")))
    assert "check maadtiddini" in fake.posts_in(PARENT, "hearth-companion-a")[-1].body
    await wait_for(lambda: any("relative-in-trouble" in p.body and "About: Shamanth" in p.body
                               for p in fake.posts_in(FAMILY, "hearth-companion-a")))
    await _stop(task, apis)


async def test_quiet_morning_checks_in_then_alerts(fake, cfg):
    from datetime import datetime, timedelta, timezone

    companion, task, apis = await _one_companion(fake, cfg)
    tz = cfg.parent.timezone
    now = datetime.now(timezone.utc)
    local = now.astimezone(tz).replace(hour=10, minute=31, second=0, microsecond=0)
    companion.last_parent_message_at = None
    await companion._quiet_check(now, local, "test-day")
    await wait_for(lambda: any("kaafi aaytha" in p.body for p in fake.posts_in(PARENT, "hearth-companion-a")))
    companion.pending_quiet = now - timedelta(minutes=46)
    await companion._quiet_check(now, local, "test-day")
    await wait_for(lambda: any("no-signal" in p.body for p in fake.posts_in(FAMILY, "hearth-companion-a")))
    await _stop(task, apis)
