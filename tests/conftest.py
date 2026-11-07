from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hearth.config import HearthConfig, parse_config

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "hearth.example.yaml"


@pytest.fixture
def cfg() -> HearthConfig:
    return parse_config(yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")))
