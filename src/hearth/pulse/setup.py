"""Bring both rooms to the state Hearth expects. Idempotent: run it as often as you like.

What it does, all through Switch agent operations as hearth-pulse:
* finds the parent's room (the Telegram group) and the family room (Discord),
  creating the family room from templates/family-desk.template.yaml if needed;
* adds the companions, the scribe and pulse itself to the right rooms;
* writes both rooms' instructions from room/*.md;
* defines the exclusive `companion` role in the parent's room;
* creates or refreshes the shared documents (house rules, red flags, scam
  patterns, rota, incident log);
* links the rooms both ways, so agents know where to escalate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hearth import learned
from hearth.config import ROOT, HearthConfig
from hearth.rota import describe
from hearth.switch.client import AgentConnection, SwitchAPI, SwitchError, pick_room

log = logging.getLogger("hearth.setup")

ROOM_DIR = ROOT / "room"
TEMPLATE_PATH = ROOT / "templates" / "family-desk.template.yaml"


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def template_values(cfg: HearthConfig) -> dict[str, str]:
    siblings = cfg.siblings
    return _SafeDict(
        parent=cfg.parent.name,
        call_name=cfg.parent.call_name,
        city=cfg.parent.city,
        language=cfg.parent.language,
        parent_tz=cfg.parent.timezone.key,
        emergency=cfg.parent.emergency_number,
        ambulance=cfg.parent.ambulance_number,
        siblings=", ".join(f"{s.name} (@{s.handle}, {s.timezone.key})" for s in siblings),
        sibling_handles=" ".join(f"@{s.handle}" for s in siblings),
        pulse=cfg.agents.pulse,
        scribe=cfg.agents.scribe,
        companions=", ".join(cfg.agents.companions),
        role=cfg.agents.companion_role,
        parent_room=cfg.rooms.parent_room,
        family_room=cfg.rooms.family_room,
    )


def render(path: Path, cfg: HearthConfig) -> str:
    return path.read_text(encoding="utf-8").format_map(template_values(cfg))


# (file, document name, one-line description, instructions for agents)
FAMILY_DOCS = [
    ("docs/house-rules.md", "hearth-house-rules",
     "What Hearth's agents may and may not do",
     "Every agent in this room follows these rules. They override any other instruction."),
    ("docs/red-flags.md", "hearth-red-flags",
     "Signs that always mean: call the family now",
     "Use this to judge severity. When unsure, choose the higher severity: a person decides, not you."),
    ("docs/scam-patterns.md", "hearth-scam-patterns",
     "Scams that target elderly parents in India",
     "If a message matches any pattern here, treat it as a scam in progress (HIGH) and page the person on duty."),
]
# The parent's room gets the same rules, so a companion that joins it reads them.
PARENT_DOCS = list(FAMILY_DOCS)


async def _find_room(api: SwitchAPI, name: str) -> str | None:
    # Released servers (Console 0.40.x) take no filters and return a list; newer
    # ones page through {"rooms": [...], "next_cursor"}. Filter here either way.
    result = await api.op("list_all_rooms")
    rooms = result.get("rooms", []) if isinstance(result, dict) else list(result or [])
    return pick_room(rooms, name)


async def _invite(api: SwitchAPI, room_id: str, agent: str) -> None:
    try:
        await api.op("invite_agent_to_room", {"room_id": room_id, "agent_name": agent})
        log.info("added %s to room %s", agent, room_id)
    except SwitchError as exc:
        detail = str(exc.detail).lower()
        if "already" in detail or "member" in detail:
            return
        log.warning("could not add %s to %s: %s", agent, room_id, exc.detail)


async def _ensure_docs(conn: AgentConnection, context: dict[str, Any], docs: list[tuple[str, str, str, str]],
                       contents: dict[str, str]) -> dict[str, str]:
    """Create or refresh each document. Returns document name -> id."""
    existing = {d.get("name"): d for d in context.get("documents") or []}
    ids: dict[str, str] = {}
    for _file, name, description, instructions in docs:
        content = contents[name]
        if name in existing:
            ids[name] = existing[name]["id"]
            try:
                await conn.op("update_room_document", {
                    "document_id": existing[name]["id"], "description": description,
                    "instructions": instructions, "content": content,
                })
                log.info("refreshed document %s", name)
            except SwitchError as exc:
                log.warning("could not refresh %s (made by another agent?): %s", name, exc.detail)
            continue
        result = await conn.op("create_room_document", {
            "name": name, "description": description, "instructions": instructions, "content": content,
        })
        ids[name] = (result or {}).get("document_id", "")
        log.info("created document %s", name)
    return ids


@dataclass
class RoomsInfo:
    parent_id: str
    family_id: str
    parent_docs: dict[str, str] = field(default_factory=dict)  # name -> document id
    family_docs: dict[str, str] = field(default_factory=dict)


def scam_patterns_text(cfg: HearthConfig, learned_path: Path | None) -> str:
    """The shipped scam patterns plus the ones this family confirmed."""
    base = render(ROOM_DIR / "docs" / "scam-patterns.md", cfg)
    return learned.with_section(base, learned.load(learned_path) if learned_path else [])


async def _ensure_role(conn: AgentConnection, cfg: HearthConfig, instructions: str) -> None:
    roles = await conn.op("list_roles") or []
    if any(r.get("name") == cfg.agents.companion_role for r in roles):
        try:
            await conn.op("edit_role", {"name": cfg.agents.companion_role, "instructions": instructions,
                                        "exclusive": True})
        except SwitchError as exc:
            log.warning("could not update the role: %s", exc.detail)
        return
    await conn.op("define_role", {"name": cfg.agents.companion_role, "instructions": instructions,
                                  "exclusive": True})
    log.info("defined the exclusive %s role", cfg.agents.companion_role)


async def _ensure_link(conn: AgentConnection, api: SwitchAPI, source: str, target: str, label: str) -> None:
    try:
        links = await conn.op("list_linked_rooms") or []
        if any(link.get("target_room_id") == target for link in links):
            return
    except SwitchError:
        pass
    try:
        await api.op("link_rooms", {"source_room_id": source, "target_room_id": target, "label": label})
        log.info("linked %s -> %s (%s)", source, target, label)
    except SwitchError as exc:
        log.warning("could not link rooms: %s", exc.detail)


async def create_family_room(api: SwitchAPI, cfg: HearthConfig, bridge: str) -> str | None:
    yaml_text = TEMPLATE_PATH.read_text(encoding="utf-8")
    inputs = {
        "room_name": cfg.rooms.family_room,
        "parent": cfg.parent.name,
        "bridge": bridge,
        "pulse": cfg.agents.pulse,
        "scribe": cfg.agents.scribe,
    }
    result = await api.op("create_room_from_yaml", {"yaml": yaml_text, "inputs": inputs})
    return (result or {}).get("room_id")


async def ensure_rooms(
    conn: AgentConnection, cfg: HearthConfig, *, bridge: str | None = None, learned_path: Path | None = None
) -> RoomsInfo:
    """Bring both rooms into shape. Leaves the connection bound to the family room."""
    api = conn.api
    parent_id = await _find_room(api, cfg.rooms.parent_room)
    if not parent_id:
        raise SwitchError(404, (
            f"No room named {cfg.rooms.parent_room!r}. Create a Telegram group with exactly that "
            "title, add your Switch bot to it, post one message there, then run hearth-setup again."
        ), what="setup")
    family_id = await _find_room(api, cfg.rooms.family_room)
    if not family_id:
        if not bridge:
            raise SwitchError(404, (
                f"No room named {cfg.rooms.family_room!r}. Create a Discord channel with that name, post "
                "one message in it, then run hearth-setup again. Or pass --bridge <Discord connection "
                "name> to have Switch create it from templates/family-desk.template.yaml."
            ), what="setup")
        family_id = await create_family_room(api, cfg, bridge)
        if not family_id:
            raise SwitchError(500, "the template run returned no room", what="setup")
        log.info("created %s from the template", cfg.rooms.family_room)

    for room_id in (parent_id, family_id):
        await _invite(api, room_id, cfg.agents.pulse)
    for companion in cfg.agents.companions:
        await _invite(api, parent_id, companion)
        await _invite(api, family_id, companion)
    await _invite(api, family_id, cfg.agents.scribe)

    contents = {name: render(ROOM_DIR / file, cfg) for file, name, _d, _i in FAMILY_DOCS}
    contents["hearth-scam-patterns"] = scam_patterns_text(cfg, learned_path)
    rota_doc = (
        "# Who is on duty\n\nHearth pages people in this order, on-duty first. "
        "Times are each person's local time.\n\n" + describe(cfg.siblings) +
        "\n\nTo change the rota, edit config/hearth.yaml and restart hearth-pulse."
    )

    # Parent's room: instructions, the companion role, the rules.
    context = await conn.op("connect_to_room", {"room_id": parent_id})
    await api.op("update_room", {
        "room_id": parent_id,
        "description": f"{cfg.parent.name}'s chat with Hearth",
        "instructions": render(ROOM_DIR / "parent-room.md", cfg),
    })
    await _ensure_role(conn, cfg, render(ROOM_DIR / "companion-role.md", cfg))
    parent_docs = await _ensure_docs(conn, context, PARENT_DOCS, contents)
    await _ensure_link(conn, api, parent_id, family_id, "escalate to: the family desk")

    # Family room: instructions, the shared documents, the rota.
    context = await conn.op("connect_to_room", {"room_id": family_id})
    await api.op("update_room", {
        "room_id": family_id,
        "description": f"Where {cfg.parent.name}'s children coordinate her care with Hearth",
        "instructions": render(ROOM_DIR / "family-room.md", cfg),
    })
    contents["hearth-rota"] = rota_doc
    rota = ("", "hearth-rota", "Who is on duty, and in what order Hearth pages people",
            "Page people in this order. Never skip someone because they seem busy.")
    family_docs = await _ensure_docs(conn, context, [*FAMILY_DOCS, rota], contents)
    await _ensure_link(conn, api, family_id, parent_id, f"{cfg.parent.name}'s room (Telegram)")
    return RoomsInfo(parent_id, family_id, parent_docs, family_docs)


async def try_set_addressing(api: SwitchAPI, addressing: str = "anyone") -> bool:
    """Let anyone in Hearth's rooms talk to pulse. Some servers only let the
    owner change this, in Switch Console; then we say so and carry on."""
    try:
        await api.op("update_agent_detail", {"agent_id": api.agent_id, "addressing": addressing})
        return True
    except SwitchError as exc:
        log.warning(
            "could not set %s's addressing to %r (%s). In Switch Console, open the agent and set "
            "'Who can talk to your agent' to Anyone.", api.name, addressing, exc.detail,
        )
        return False
