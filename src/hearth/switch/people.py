"""Who is who in a room: tell people's messages from agents' messages."""

from __future__ import annotations

import logging
import time

from hearth.switch.client import MessageEvent, SwitchAPI, SwitchError

log = logging.getLogger(__name__)

# Switch issues participant ids shaped @switch-<type>-<id>:<server>. The docs ask
# clients to treat ids as opaque, so this is only a fallback for senders the
# participant list doesn't name (Switch's own notices, a bridge relay).
_NON_PERSON_PREFIXES = ("switch-agent", "switch-admin", "switch-bridge")


class RoomPeople:
    def __init__(self, api: SwitchAPI, room_id: str) -> None:
        self.api = api
        self.room_id = room_id
        self.agent_names: set[str] = set()
        self.agent_labels: set[str] = set()
        self.people: set[str] = set()
        self._refreshed = 0.0

    async def refresh(self, *, max_age: float = 0) -> None:
        if max_age and time.monotonic() - self._refreshed < max_age:
            return
        try:
            participants = await self.api.participants(self.room_id)
        except SwitchError as exc:
            log.warning("could not list participants of %s: %s", self.room_id, exc)
            return
        agents, labels, people = set(), set(), set()
        for p in participants:
            name = str(p.get("name") or "").lower()
            if p.get("type") == "agent":
                agents.add(name)
                agent_id = p.get("agent_id")
                if agent_id:
                    try:
                        detail = await self.api.op("get_agent_detail", {"agent_id": agent_id})
                        if detail and detail.get("display_name"):
                            labels.add(str(detail["display_name"]).lower())
                    except SwitchError:
                        pass
            else:
                people.add(name)
        self.agent_names, self.agent_labels, self.people = agents, labels, people
        self._refreshed = time.monotonic()

    def is_agent(self, event: MessageEvent) -> bool:
        name = event.sender_name.lower().lstrip("@")
        if name in self.agent_names or name in self.agent_labels:
            return True
        localpart = event.sender.lstrip("@").split(":", 1)[0]
        return localpart.startswith(_NON_PERSON_PREFIXES)

    def handles(self) -> list[str]:
        return sorted(self.people)
