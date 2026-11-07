from datetime import datetime, timedelta, timezone

import pytest
import yaml

from hearth import learned
from hearth.config import ConfigError, parse_config
from hearth.handoff import ParentMessage, build_handoff
from hearth.incidents import IncidentBook
from hearth.protocol_text import Alert
from hearth.triage import Severity
from test_config_and_rooms import EXAMPLE

NOW = datetime(2026, 11, 5, 17, 30, tzinfo=timezone.utc)  # 23:00 in India, 18:30 in Berlin


def test_learned_section_round_trip(tmp_path):
    path = tmp_path / "learned.json"
    items = [learned.LearnedScam("2026-11-05", "ESC-3", "Fake BESCOM SMS.", ["power cut tonight 9.30", "VM-BESCOM"], "shreyabj")]
    learned.save(path, items)
    text = learned.with_section("# Scams\n\nBase text.", learned.load(path))
    assert learned.phrases_from_document(text) == ["power cut tonight 9.30", "VM-BESCOM"]
    # Writing it again replaces the section instead of stacking a second one.
    again = learned.with_section(text, learned.load(path))
    assert again.count(learned.HEADING) == 1


def test_handoff_summarises_the_shift(cfg):
    shreya = cfg.sibling_by_handle("shreyabj")
    shamanth = cfg.sibling_by_handle("ms_shamanth")
    messages = [
        ParentMessage(NOW - timedelta(hours=6), "Good morning! BP 128/82, maatre tagonde"),
        ParentMessage(NOW - timedelta(hours=2), "knee is paining a bit on the stairs"),
    ]
    book = IncidentBook(cfg.siblings, lambda s: 60)
    book.open(Alert(Severity.HIGH, "scam", "m-1"), NOW - timedelta(hours=1))
    text = build_handoff(cfg, incoming=shreya, outgoing=shamanth, messages=messages,
                         incidents=book.incidents, now=NOW, addressed=True)
    first, second, third = text.splitlines()
    assert first.startswith("🌅 Shift handoff: you're now first on call (18:30 Europe/Berlin).")
    assert "@ms_shamanth is off" in first
    assert "2 messages" in second and "BP 128/82" in second
    assert "took her medicines at 17:00" in second  # 11:30 UTC is 17:00 in India
    assert "mentioned pain" in second
    assert "ESC-1 HIGH scam (not acked)" in third


def test_handoff_with_silence_says_so(cfg):
    shreya = cfg.sibling_by_handle("shreyabj")
    text = build_handoff(cfg, incoming=shreya, outgoing=None, messages=[], incidents=[], now=NOW)
    assert "@shreyabj is now first on call" in text
    assert "no messages from her" in text and "Open incidents: none." in text


def test_quiet_check_config():
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    cfg = parse_config(data)
    assert cfg.checkins.quiet_check.expect_by.hour == 10
    assert cfg.checkins.quiet_check.reply_within_minutes == 45
    data["checkins"]["quiet_check"]["since"] = "11:00"
    with pytest.raises(ConfigError):
        parse_config(data)
