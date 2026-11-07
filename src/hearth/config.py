"""Load the family config (YAML) and the environment (.env)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / ".hearth"

_TIME_RE = re.compile(r"^([01]\d|2[0-4]):([0-5]\d)$")


class ConfigError(ValueError):
    """The family config or environment is missing something or is malformed."""


def load_dotenv(path: Path | None = None) -> None:
    """Read KEY=VALUE lines into os.environ without overriding what is set."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def parse_hhmm(text: str) -> time:
    match = _TIME_RE.match(str(text).strip())
    if not match:
        raise ConfigError(f"expected a time as HH:MM, got {text!r}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour == 24:
        if minute != 0:
            raise ConfigError(f"24:{minute:02d} is not a time")
        return time(23, 59, 59)
    return time(hour, minute)


@dataclass(frozen=True)
class Window:
    """A daily local-time window. `start > end` wraps past midnight."""

    start: time
    end: time

    @classmethod
    def parse(cls, text: str) -> Window:
        text = str(text).strip().lower()
        if text in {"always", "any", "00:00-24:00"}:
            return cls(time(0, 0), time(23, 59, 59))
        if "-" not in text:
            raise ConfigError(f"expected a window as HH:MM-HH:MM, got {text!r}")
        start, end = text.split("-", 1)
        return cls(parse_hhmm(start), parse_hhmm(end))

    def contains(self, moment: time) -> bool:
        if self.start <= self.end:
            return self.start <= moment <= self.end
        return moment >= self.start or moment <= self.end


@dataclass(frozen=True)
class Parent:
    name: str
    call_name: str
    language: str
    timezone: ZoneInfo
    city: str
    emergency_number: str
    ambulance_number: str
    # The parent's name in the chat room, as Switch reports the sender. Empty
    # means any person in the parent's room is treated as the parent.
    handle: str = ""
    # "kanglish" or "english": the style of the fixed safety messages.
    template_style: str = "english"


@dataclass(frozen=True)
class Sibling:
    name: str
    handle: str
    timezone: ZoneInfo
    on_duty: Window


@dataclass(frozen=True)
class Rooms:
    parent_room: str
    family_room: str


@dataclass(frozen=True)
class Agents:
    pulse: str
    companions: tuple[str, ...]
    scribe: str
    companion_role: str


@dataclass(frozen=True)
class QuietCheck:
    """The dead man's switch: silence by a time of day is itself a signal."""

    expect_by: time            # she usually writes before this
    since: time                # "today" starts here, for counting her messages
    reply_within_minutes: int  # no answer to the gentle check-in this long raises an alert


@dataclass(frozen=True)
class Checkins:
    times: tuple[time, ...]
    no_reply_minutes: int
    daily_summary_time: time
    quiet_check: QuietCheck | None = None


@dataclass(frozen=True)
class Shield:
    """The "your child is in trouble" scam shield."""

    recent_minutes: int  # a sibling active this recently is reported to the parent as just seen


@dataclass(frozen=True)
class Escalation:
    ack_timeout_seconds: dict[str, int]
    demo_ack_timeout_seconds: int
    companion_offline_seconds: int

    def timeout_for(self, severity: str, demo: bool) -> int:
        if demo:
            return self.demo_ack_timeout_seconds
        return int(self.ack_timeout_seconds.get(severity, 300))


@dataclass(frozen=True)
class Digest:
    weekday: int  # Monday = 0
    time: time
    timezone: ZoneInfo


@dataclass(frozen=True)
class HearthConfig:
    parent: Parent
    siblings: tuple[Sibling, ...]
    rooms: Rooms
    agents: Agents
    checkins: Checkins
    escalation: Escalation
    digest: Digest
    shield: Shield = Shield(recent_minutes=15)
    handoff_hours: int = 8
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    def sibling_by_handle(self, handle: str) -> Sibling | None:
        wanted = handle.lstrip("@").lower()
        for sibling in self.siblings:
            if sibling.handle.lower() == wanted:
                return sibling
        return None


_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _require(data: dict[str, Any], key: str, where: str) -> Any:
    if key not in data or data[key] in (None, ""):
        raise ConfigError(f"{where}.{key} is required")
    return data[key]


def _zone(name: str, where: str) -> ZoneInfo:
    try:
        return ZoneInfo(str(name))
    except Exception as exc:  # noqa: BLE001 - ZoneInfo raises several types
        raise ConfigError(f"{where}: unknown time zone {name!r}") from exc


def parse_config(data: dict[str, Any]) -> HearthConfig:
    if not isinstance(data, dict):
        raise ConfigError("the config must be a YAML mapping")

    p = _require(data, "parent", "config")
    parent = Parent(
        name=str(_require(p, "name", "parent")),
        call_name=str(p.get("call_name") or p["name"]),
        language=str(p.get("language") or "English"),
        timezone=_zone(_require(p, "timezone", "parent"), "parent.timezone"),
        city=str(p.get("city") or ""),
        emergency_number=str(p.get("emergency_number") or "112"),
        ambulance_number=str(p.get("ambulance_number") or "108"),
        handle=str(p.get("handle") or "").lstrip("@"),
        template_style=str(p.get("template_style") or "english").lower(),
    )
    if parent.template_style not in {"english", "kanglish"}:
        raise ConfigError("parent.template_style must be 'english' or 'kanglish'")

    raw_siblings = _require(data, "siblings", "config")
    if not isinstance(raw_siblings, list) or not raw_siblings:
        raise ConfigError("config.siblings must list at least one person")
    siblings = tuple(
        Sibling(
            name=str(_require(s, "name", f"siblings[{i}]")),
            handle=str(_require(s, "handle", f"siblings[{i}]")).lstrip("@"),
            timezone=_zone(_require(s, "timezone", f"siblings[{i}]"), f"siblings[{i}].timezone"),
            on_duty=Window.parse(s.get("on_duty", "always")),
        )
        for i, s in enumerate(raw_siblings)
    )

    r = _require(data, "rooms", "config")
    rooms = Rooms(
        parent_room=str(_require(r, "parent_room", "rooms")),
        family_room=str(_require(r, "family_room", "rooms")),
    )

    a = _require(data, "agents", "config")
    companions = tuple(str(c) for c in _require(a, "companions", "agents"))
    if not companions:
        raise ConfigError("agents.companions must name at least one agent")
    agents = Agents(
        pulse=str(_require(a, "pulse", "agents")),
        companions=companions,
        scribe=str(_require(a, "scribe", "agents")),
        companion_role=str(a.get("companion_role") or "companion"),
    )

    c = data.get("checkins") or {}
    q = c.get("quiet_check")
    quiet = None
    if q:
        quiet = QuietCheck(
            expect_by=parse_hhmm(q.get("expect_by", "10:30")),
            since=parse_hhmm(q.get("since", "05:00")),
            reply_within_minutes=int(q.get("reply_within_minutes", 45)),
        )
        if quiet.since >= quiet.expect_by:
            raise ConfigError("checkins.quiet_check.since must be earlier than expect_by")
    checkins = Checkins(
        times=tuple(parse_hhmm(t) for t in c.get("times", ["08:30", "20:00"])),
        no_reply_minutes=int(c.get("no_reply_minutes", 120)),
        daily_summary_time=parse_hhmm(c.get("daily_summary_time", "21:30")),
        quiet_check=quiet,
    )

    e = data.get("escalation") or {}
    escalation = Escalation(
        ack_timeout_seconds={
            k: int(v)
            for k, v in (e.get("ack_timeout_seconds") or {"red": 120, "high": 300, "medium": 900}).items()
        },
        demo_ack_timeout_seconds=int(e.get("demo_ack_timeout_seconds", 45)),
        companion_offline_seconds=int(e.get("companion_offline_seconds", 30)),
    )

    d = data.get("digest") or {}
    weekday_name = str(d.get("weekday", "sunday")).lower()
    if weekday_name not in _WEEKDAYS:
        raise ConfigError(f"digest.weekday must be a day name, got {weekday_name!r}")
    digest = Digest(
        weekday=_WEEKDAYS.index(weekday_name),
        time=parse_hhmm(d.get("time", "19:00")),
        timezone=_zone(d.get("timezone", parent.timezone.key), "digest.timezone"),
    )

    sh = data.get("shield") or {}
    return HearthConfig(
        parent=parent,
        siblings=siblings,
        rooms=rooms,
        agents=agents,
        checkins=checkins,
        escalation=escalation,
        digest=digest,
        shield=Shield(recent_minutes=int(sh.get("recent_minutes", 15))),
        handoff_hours=int((data.get("handoff") or {}).get("lookback_hours", 8)),
        raw=data,
    )


def load_config(path: str | Path | None = None) -> HearthConfig:
    load_dotenv()
    path = Path(path or os.environ.get("HEARTH_CONFIG") or ROOT / "config" / "hearth.yaml")
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        raise ConfigError(
            f"{path} not found. Copy config/hearth.example.yaml to config/hearth.yaml and edit it."
        )
    with path.open(encoding="utf-8") as handle:
        return parse_config(yaml.safe_load(handle))


def env(name: str, default: str | None = None, *, required: bool = False) -> str:
    """An environment value; an empty one (``KEY=`` in .env) counts as unset."""
    value = os.environ.get(name) or default
    if required and not value:
        raise ConfigError(f"{name} is not set. Add it to .env (see .env.example).")
    return value or ""


def _console_ports_files() -> list[Path]:
    """Where Switch Console records the ports of the server it runs on this computer."""
    candidates = []
    for base in (os.environ.get("APPDATA"), str(Path.home() / "Library" / "Application Support"),
                 os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")):
        if base:
            candidates.append(Path(base) / "switchdash" / "local-switch-server" / "ports.json")
    return candidates


def switch_base_url() -> str:
    """SWITCH_BASE_URL if set; otherwise the API port of the server Switch Console
    runs on this computer (it picks the port, e.g. 64729); otherwise :8000."""
    explicit = env("SWITCH_BASE_URL")
    if explicit:
        return explicit.rstrip("/")
    import json

    for path in _console_ports_files():
        try:
            port = json.loads(path.read_text(encoding="utf-8")).get("api")
        except (OSError, ValueError):
            continue
        if port:
            return f"http://127.0.0.1:{int(port)}"
    return "http://127.0.0.1:8000"


def demo_mode() -> bool:
    return env("HEARTH_DEMO", "0").strip().lower() in {"1", "true", "yes", "on"}
