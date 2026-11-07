from datetime import datetime, timedelta, timezone

from hearth.incidents import ACKED, NOTED, RESOLVED, IncidentBook
from hearth.protocol_text import Alert
from hearth.triage import Severity

T0 = datetime(2026, 11, 5, 4, 30, tzinfo=timezone.utc)  # 10:00 in India


def book(cfg, tmp_path=None):
    return IncidentBook(cfg.siblings, lambda s: 60, path=(tmp_path / "i.json") if tmp_path else None)


def test_high_pages_on_duty_then_next_then_everyone(cfg):
    b = book(cfg)
    incident, created = b.open(Alert(Severity.HIGH, "scam", "m-1"), T0)
    assert created
    first = b.due(T0)
    assert [a.handles for a in first] == [["ms_shamanth"]]
    assert b.due(T0 + timedelta(seconds=30)) == []
    second = b.due(T0 + timedelta(seconds=61))
    assert [a.handles for a in second] == [["shreyabj"]]
    assert "no ack from @ms_shamanth" in second[0].reason
    third = b.due(T0 + timedelta(seconds=122))
    assert third[0].everyone
    assert b.due(T0 + timedelta(seconds=500)) == []


def test_ack_stops_paging_and_resolve_closes(cfg):
    b = book(cfg)
    incident, _ = b.open(Alert(Severity.HIGH, "scam", "m-2"), T0)
    b.due(T0)
    acked = b.ack("shreyabj", None, T0 + timedelta(seconds=20))
    assert acked is incident and incident.state == ACKED
    assert b.due(T0 + timedelta(seconds=200)) == []
    resolved = b.resolve("shreyabj", incident.id, "hung up, nothing shared", T0 + timedelta(seconds=300))
    assert resolved.state == RESOLVED and resolved.note == "hung up, nothing shared"


def test_red_pages_everyone_and_repeats(cfg):
    b = IncidentBook(cfg.siblings, lambda s: 60, max_red_pages=2)
    b.open(Alert(Severity.RED, "fall", "m-3"), T0)
    first = b.due(T0)
    assert first[0].everyone and set(first[0].handles) == {"ms_shamanth", "shreyabj"}
    assert b.due(T0 + timedelta(seconds=61))[0].everyone
    assert b.due(T0 + timedelta(seconds=200)) == []


def test_same_ref_is_one_incident(cfg):
    b = book(cfg)
    one, created_one = b.open(Alert(Severity.HIGH, "scam", "m-4"), T0)
    two, created_two = b.open(Alert(Severity.HIGH, "scam", "m-4"), T0)
    assert created_one and not created_two and one is two


def test_low_is_noted_not_paged(cfg):
    b = book(cfg)
    incident, _ = b.open(Alert(Severity.LOW, "pain", "m-5"), T0)
    assert incident.state == NOTED
    assert b.due(T0) == []


def test_ambiguous_ack_needs_a_number(cfg):
    b = book(cfg)
    b.open(Alert(Severity.MEDIUM, "unwell", "m-6"), T0)
    b.open(Alert(Severity.MEDIUM, "unwell", "m-7"), T0)
    b.due(T0)  # both page ms_shamanth
    assert b.ack("ms_shamanth", None, T0) is None
    assert b.ack("ms_shamanth", 2, T0).id == 2


def test_persistence_round_trip(cfg, tmp_path):
    b = book(cfg, tmp_path)
    b.open(Alert(Severity.HIGH, "scam", "m-8"), T0)
    b.due(T0)
    b.save()
    again = book(cfg, tmp_path)
    again.load()
    assert again.incidents[0].ref == "m-8"
    assert again.incidents[0].pages[0].handles == ["ms_shamanth"]
    nxt, _ = again.open(Alert(Severity.HIGH, "scam", "m-9"), T0)
    assert nxt.id == 2
    assert "ESC-1" in again.to_markdown()
