from datetime import datetime, timezone

from hearth.config import Window, parse_hhmm
from hearth.rota import is_on_duty, paging_order


def test_window_wraps_past_midnight():
    window = Window.parse("17:00-09:00")
    assert window.contains(parse_hhmm("23:30"))
    assert window.contains(parse_hhmm("08:59"))
    assert not window.contains(parse_hhmm("12:00"))


def test_follow_the_sun(cfg):
    shamanth, shreya = cfg.siblings
    # 10:00 in India (04:30 UTC): Shamanth is awake, Shreya (Berlin, 05:30) covers nights.
    morning_india = datetime(2026, 11, 5, 4, 30, tzinfo=timezone.utc)
    assert is_on_duty(shamanth, morning_india)
    assert is_on_duty(shreya, morning_india)  # 05:30 Berlin is inside 17:00-09:00
    assert [s.handle for s in paging_order(cfg.siblings, morning_india)] == ["ms_shamanth", "shreyabj"]

    # 02:00 in India (20:30 UTC previous day): Shamanth is off, Shreya (21:30 Berlin) is on.
    night_india = datetime(2026, 11, 4, 20, 30, tzinfo=timezone.utc)
    assert not is_on_duty(shamanth, night_india)
    assert [s.handle for s in paging_order(cfg.siblings, night_india)] == ["shreyabj", "ms_shamanth"]
