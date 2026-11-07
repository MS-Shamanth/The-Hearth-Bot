import string
from pathlib import Path

import pytest
import yaml

from hearth.config import ConfigError, parse_config
from hearth.pulse.setup import FAMILY_DOCS, ROOM_DIR, TEMPLATE_PATH, render, template_values

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "hearth.example.yaml"


def test_example_config_loads(cfg):
    assert cfg.parent.call_name == "Amma"
    assert cfg.parent.template_style == "kanglish"
    assert cfg.agents.companion_role == "companion"
    assert cfg.sibling_by_handle("@ShreyaBJ").name == "Shreya"


def test_bad_time_zone_is_reported():
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    data["siblings"][0]["timezone"] = "Mars/Olympus"
    with pytest.raises(ConfigError, match="unknown time zone"):
        parse_config(data)


def test_room_texts_have_no_unknown_placeholders(cfg):
    known = set(template_values(cfg))
    files = ["parent-room.md", "family-room.md", "companion-role.md", *[f for f, *_ in FAMILY_DOCS]]
    for name in files:
        text = (ROOM_DIR / name).read_text(encoding="utf-8")
        fields = {f for _, f, _, _ in string.Formatter().parse(text) if f}
        assert fields <= known, f"{name} uses unknown placeholders {fields - known}"
        rendered = render(ROOM_DIR / name, cfg)
        assert "{" not in rendered, name


def test_template_is_valid_yaml_with_expected_params():
    doc = yaml.safe_load(TEMPLATE_PATH.read_text(encoding="utf-8"))
    assert doc["version"] == 1
    assert {"room_name", "parent", "bridge", "pulse", "scribe"} <= set(doc["params"])
    assert doc["room"]["agents"] == ["{pulse}", "{scribe}"]
