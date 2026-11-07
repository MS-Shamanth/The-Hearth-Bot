import pytest

from hearth.protocol_text import Alert, format_alert, parse_alert, parse_reply
from hearth.triage import Severity


def test_alert_round_trip():
    alert = Alert(
        severity=Severity.HIGH, kind="scam", ref="m-1a2b3c4d",
        parent_said="A CBI officer called", told_parent="This is a scam", why="known scam pattern",
        summary="Possible digital arrest scam",
    )
    text = format_alert(alert, "Amma")
    parsed = parse_alert(text)
    assert parsed == alert


def test_alert_parse_ignores_other_messages():
    assert parse_alert("Today with Amma: all good") is None


@pytest.mark.parametrize(
    "body, action, incident",
    [
        ("ack", "ack", None),
        ("ack 17", "ack", 17),
        ("@hearth-pulse ack ESC-17", "ack", 17),
        ("on it", "ack", None),
        ("calling her now", "ack", None),
        ("resolved 17: she hung up, nothing shared", "resolve", 17),
        ("resolved", "resolve", None),
        ("done 4", "resolve", 4),
        ("status", "status", None),
        ("drill scam high", "drill", None),
    ],
)
def test_replies(body, action, incident):
    reply = parse_reply(body)
    assert reply is not None
    assert reply.action == action
    assert reply.incident_id == incident


def test_resolve_note():
    assert parse_reply("resolved 17: she hung up").note == "she hung up"


@pytest.mark.parametrize(
    "body",
    ["done with lunch", "closed the window", "acknowledgement letter came", "status of the parcel?", "hello"],
)
def test_everyday_words_are_not_commands(body):
    assert parse_reply(body) is None


def test_alert_about_round_trip():
    alert = Alert(severity=Severity.HIGH, kind="relative-in-trouble", ref="m-1", about="Shamanth")
    assert parse_alert(format_alert(alert, "Amma")).about == "Shamanth"


@pytest.mark.parametrize("body", ["safe", "I'm safe", "i am fine", "all good here!", "@hearth-pulse safe"])
def test_safe(body):
    assert parse_reply(body).action == "safe"


def test_learn_takes_quoted_phrases():
    reply = parse_reply('learn 17: "electricity will be disconnected tonight", "VM-BESCOM"')
    assert reply.action == "learn" and reply.incident_id == 17
    assert reply.phrases == ["electricity will be disconnected tonight", "VM-BESCOM"]


def test_learn_without_quotes_splits_on_commas():
    assert parse_reply("learn 3: courier customs fee, FedEx parcel").phrases == ["courier customs fee", "FedEx parcel"]


@pytest.mark.parametrize("body", ["is it safe to give her a dolo?", "learnt a lot today", "safe travels"])
def test_not_commands(body):
    assert parse_reply(body) is None


def test_handoff_command():
    assert parse_reply("handoff").action == "handoff"


def test_room_names_with_bridge_prefix():
    from hearth.switch.client import pick_room
    rooms = [{"room_id": "a", "name": "Mattermost: Town Square"},
             {"room_id": "b", "name": "Discord: hearth-family-desk"},
             {"room_id": "c", "name": "Telegram: Amma Hearth"}]
    assert pick_room(rooms, "hearth-family-desk") == "b"
    assert pick_room(rooms, "Amma Hearth") == "c"
    assert pick_room(rooms + [{"room_id": "d", "name": "hearth-family-desk"}], "hearth-family-desk") == "d"
    assert pick_room(rooms, "family") is None
