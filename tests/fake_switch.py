"""An in-memory stand-in for a Switch server, for end-to-end tests of Hearth's agents.

It speaks the parts of the agent protocol Hearth uses: the WebSocket with
connection_state and pings, the operations Hearth calls, the connectionless
message route, participants, and exclusive roles whose lease is released
when the holder's connection drops (as a heartbeat lapse would).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from websockets.asyncio.server import ServerConnection, serve

from hearth.switch.client import AgentCredentials, SwitchAPI


@dataclass
class Room:
    id: str
    name: str
    people: list[str]
    agents: list[str] = field(default_factory=list)
    instructions: str = ""
    roles: dict[str, dict[str, Any]] = field(default_factory=dict)
    documents: dict[str, dict[str, Any]] = field(default_factory=dict)
    links: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Posted:
    agent: str
    room: str
    body: str
    targets: list[str] = field(default_factory=list)


class OpError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        self.status, self.detail = status, detail


class FakeSwitch:
    def __init__(self) -> None:
        self.rooms: dict[str, Room] = {}
        self.agents: dict[str, str] = {}  # agent id -> name
        self.posts: list[Posted] = []
        self.history: dict[str, list[dict[str, Any]]] = {}  # room id -> timeline entries
        self.media: dict[str, tuple[bytes, str, str]] = {}  # mxc -> (bytes, mime, filename)
        self.uploads: list[tuple[str, str, list[str]]] = []  # (agent, room, filenames)
        self.pongs: dict[str, int] = {}
        self.bound: dict[str, str] = {}  # connection id -> room id
        self.sockets: dict[str, tuple[str, ServerConnection]] = {}  # connection id -> (agent, socket)
        self._seq = itertools.count(1)
        self._server = None
        self.port = 0

    # ── setup helpers ────────────────────────────────────────────────────

    def add_room(self, room_id: str, name: str, people: list[str], agents: list[str] | None = None) -> Room:
        room = Room(room_id, name, people, list(agents or []))
        self.rooms[room_id] = room
        return room

    def add_agent(self, name: str) -> AgentCredentials:
        agent_id = f"id-{name}"
        self.agents[agent_id] = name
        return AgentCredentials(name=name, agent_id=agent_id, api_key=f"key-{name}",
                                base_url=f"http://127.0.0.1:{self.port}")

    def api(self, creds: AgentCredentials) -> SwitchAPI:
        return SwitchAPI(creds, transport=httpx.MockTransport(self._http))

    async def start(self) -> None:
        self._server = await serve(self._ws, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def drop(self, agent: str) -> None:
        """Simulate the agent's laptop closing: its sockets die, its leases lapse."""
        for name, socket in list(self.sockets.values()):
            if name == agent:
                await socket.close()

    def posts_in(self, room_id: str, agent: str | None = None) -> list[Posted]:
        return [p for p in self.posts if p.room == room_id and (agent is None or p.agent == agent)]

    # ── delivering messages ──────────────────────────────────────────────

    async def say(self, room_id: str, person: str, body: str, *, files: list[tuple[str, bytes, str]] | None = None,
                  at_ms: int | None = None) -> None:
        attachments = []
        for name, data, mime in files or []:
            mxc = f"mxc://test/{uuid.uuid4().hex}"
            self.media[mxc] = (data, mime, name)
            attachments.append({"filename": name, "mimetype": mime, "size": len(data), "mxc": mxc,
                                "msgtype": "m.audio" if mime.startswith("audio/") else "m.image"})
        await self._deliver(room_id, f"@switch-user-{person}:test", person, body, attachments, at_ms)

    async def _deliver(self, room_id: str, sender: str, sender_name: str, body: str,
                       attachments: list[dict[str, Any]] | None = None, at_ms: int | None = None) -> None:
        seq = next(self._seq)
        timestamp = at_ms or int(time.time() * 1000)
        self.history.setdefault(room_id, []).append({
            "id": f"$msg{seq}", "kind": "message", "sender": sender, "sender_name": sender_name,
            "body": body, "timestamp": timestamp, "attachments": attachments or [],
        })
        frame = {
            "event": "message",
            "id": seq,
            "data": {
                "type": "message", "room_id": room_id, "bridge_id": None, "channel_type": "channel_public",
                "sequence": seq,
                "payload": {
                    "addressed": False, "sender": sender, "sender_name": sender_name,
                    "message_id": f"$msg{seq}", "body": body, "timestamp": timestamp,
                    "thread_id": None, "attachments": attachments or [],
                },
            },
        }
        for conn_id, (_agent, socket) in list(self.sockets.items()):
            if self.bound.get(conn_id) == room_id:
                try:
                    await socket.send(json.dumps(frame))
                except Exception:  # noqa: BLE001 - a closing socket
                    pass

    async def _record(self, agent: str, room_id: str, body: str, targets: list[str] | None = None) -> str:
        self.posts.append(Posted(agent, room_id, body, list(targets or [])))
        prefix = " ".join(f"@{t}" for t in targets or [])
        await self._deliver(room_id, f"@switch-agent-{agent}:test", agent, f"{prefix} {body}".strip())
        return f"$evt{len(self.posts)}"

    # ── WebSocket ────────────────────────────────────────────────────────

    async def _ws(self, socket: ServerConnection) -> None:
        url = urlparse(socket.request.path)
        query = parse_qs(url.query)
        agent_id = url.path.split("/")[2]
        agent = self.agents[agent_id]
        conn_id = query["connection_id"][0]
        rooms = [r for r in (query.get("rooms", [""])[0]).split(",") if r]
        if rooms:
            self.bound[conn_id] = rooms[0]
        self.sockets[conn_id] = (agent, socket)
        await socket.send(json.dumps({"event": "connection_state", "data": {
            "connection_id": conn_id, "agent_id": agent_id, "generation": 1, "rooms": rooms, "cursor": 0,
        }}))

        async def pinger() -> None:
            while True:
                await asyncio.sleep(0.3)
                await socket.send(json.dumps({"event": "ping", "data": {}}))

        task = asyncio.create_task(pinger())
        try:
            async for raw in socket:
                if json.loads(raw).get("type") == "pong":
                    self.pongs[conn_id] = self.pongs.get(conn_id, 0) + 1
        except Exception:  # noqa: BLE001
            pass
        finally:
            task.cancel()
            self.sockets.pop(conn_id, None)
            self.bound.pop(conn_id, None)
            for room in self.rooms.values():  # the lease lapses with the connection
                for role in room.roles.values():
                    if role.get("conn") == conn_id:
                        role["holder"], role["conn"] = None, None

    # ── HTTP ─────────────────────────────────────────────────────────────

    def _http(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"ok": True})
        parts = path.strip("/").split("/")
        if parts[:2] == ["agents", "rooms"] and parts[-1] == "participants":
            room = self.rooms[parts[2]]
            people = [{"type": "user", "name": p, "agent_id": None} for p in room.people]
            agents = [{"type": "agent", "name": a, "agent_id": f"id-{a}"} for a in room.agents]
            return httpx.Response(200, json={"participants": people + agents})
        agent = self.agents.get(parts[1]) if len(parts) > 1 else None
        if agent is None:
            return httpx.Response(401, json={"detail": "unknown agent"})
        if parts[2] == "rooms" and parts[-1] == "media":
            if request.method == "GET":
                data, mime, name = self.media[request.url.params["mxc"]]
                return httpx.Response(200, content=data, headers={
                    "content-type": mime, "content-disposition": f'inline; filename="{name}"'})
            names = [line.split('filename="', 1)[1].split('"', 1)[0]
                     for line in request.content.decode("latin-1").splitlines() if 'filename="' in line]
            self.uploads.append((agent, parts[3], names))
            return httpx.Response(200, json={"ok": True})
        body = json.loads(request.content or b"{}") if request.content else {}
        if parts[2] == "typing":
            return httpx.Response(200, json={"ok": True})
        if parts[2] == "message":
            room = self.rooms[body["room_id"]]
            if agent not in room.agents:
                return httpx.Response(403, json={"detail": "not a member"})
            event_id = self._sync(self._record(agent, room.id, body["content"]))
            return httpx.Response(200, json={"ok": True, "event_id": event_id})
        if parts[2] == "ops":
            conn_id = request.headers.get("x-switch-connection-id")
            try:
                result = self._op(agent, parts[3], body, conn_id)
            except OpError as exc:
                return httpx.Response(exc.status, json={"detail": exc.detail})
            return httpx.Response(200, json={"result": result})
        return httpx.Response(404, json={"detail": f"no route {path}"})

    @staticmethod
    def _sync(coro: Any) -> Any:
        # MockTransport calls us synchronously inside the running loop: schedule the delivery.
        task = asyncio.get_event_loop().create_task(coro)
        return f"$evt-{id(task)}"

    def _room_of(self, conn_id: str | None) -> Room:
        if not conn_id or conn_id not in self.bound:
            raise OpError(409, "no connected room for this connection")
        return self.rooms[self.bound[conn_id]]

    def _role_view(self, room: Room) -> list[dict[str, Any]]:
        return [
            {"name": name, "exclusive": role["exclusive"],
             "held_by": [{"name": role["holder"]}] if role.get("holder") else []}
            for name, role in room.roles.items()
        ]

    def _op(self, agent: str, op: str, args: dict[str, Any], conn_id: str | None) -> Any:
        if op == "list_rooms":
            return [{"room_id": r.id, "name": r.name} for r in self.rooms.values() if agent in r.agents]
        if op == "list_all_rooms":
            if args:  # the released server accepts only include_archived
                raise OpError(400, f"list_all_rooms does not accept: {', '.join(args)}")
            return [{"room_id": r.id, "name": r.name, "archived": False} for r in self.rooms.values()]
        if op == "invite_agent_to_room":
            room = self.rooms[args["room_id"]]
            if args["agent_name"] in room.agents:
                raise OpError(409, "already a member")
            room.agents.append(args["agent_name"])
            return {"ok": True}
        if op == "connect_to_room":
            room = self.rooms[args["room_id"]]
            if agent not in room.agents:
                raise OpError(403, "not a member")
            self.bound[conn_id] = room.id
            return {"room_id": room.id, "instructions": room.instructions,
                    "documents": [{"id": i, "name": d["name"]} for i, d in room.documents.items()],
                    "roles": self._role_view(room)}
        if op == "update_room":
            room = self.rooms[args["room_id"]]
            room.instructions = args.get("instructions") or room.instructions
            return {"id": room.id}
        if op == "list_roles":
            return self._role_view(self._room_of(conn_id))
        if op in ("define_role", "edit_role"):
            room = self._room_of(conn_id)
            role = room.roles.setdefault(args["name"], {"holder": None, "conn": None})
            role["exclusive"] = bool(args.get("exclusive", False))
            role["instructions"] = args.get("instructions", "")
            return {"status": "ok"}
        if op == "assume_role":
            room = self._room_of(conn_id)
            role = room.roles.get(args["role"])
            if role is None:
                raise OpError(404, "no such role")
            if role.get("holder") and role["holder"] != agent:
                raise OpError(409, "role is held by another live agent")
            role["holder"], role["conn"] = agent, conn_id
            return {"role": args["role"], "instructions": role.get("instructions", "")}
        if op == "release_role":
            for room in self.rooms.values():
                for role in room.roles.values():
                    if role.get("holder") == agent:
                        role["holder"], role["conn"] = None, None
            return {"status": "released"}
        if op == "create_room_document":
            room = self._room_of(conn_id)
            doc_id = str(uuid.uuid4())
            room.documents[doc_id] = dict(args, creator=agent)
            return {"document_id": doc_id}
        if op == "update_room_document":
            for room in self.rooms.values():
                if args["document_id"] in room.documents:
                    room.documents[args["document_id"]].update({k: v for k, v in args.items() if v})
                    return {"status": "ok"}
            raise OpError(404, "no such document")
        if op == "load_internal_documents":
            room = self._room_of(conn_id)
            return [dict(room.documents[i], id=i) for i in args["ids"] if i in room.documents]
        if op == "list_linked_rooms":
            return self._room_of(conn_id).links
        if op == "link_rooms":
            self.rooms[args["source_room_id"]].links.append(
                {"target_room_id": args["target_room_id"], "label": args["label"]})
            return {"label": args["label"]}
        if op == "update_agent_detail":
            return {"ok": True}
        if op == "get_agent_detail":
            return {"display_name": None}
        if op == "get_room_detail":
            return {"roles": self._role_view(self.rooms[args["room_id"]])}
        if op == "read_context":
            room_id = args.get("room_id") or self._room_of(conn_id).id
            entries = self.history.get(room_id, [])
            if args.get("since"):
                since = datetime.fromisoformat(args["since"].replace("Z", "+00:00")).timestamp() * 1000
                entries = [e for e in entries if e["timestamp"] >= since]
            entries = entries[-int(args.get("limit") or 50):]
            return {"threads": [{"root": e, "replies": []} for e in entries], "truncated": False}
        if op == "send_targeted_message":
            room = self._room_of(conn_id)
            event_id = self._sync(self._record(agent, room.id, args["body"], args.get("target_names")))
            return {"event_id": event_id, "target_statuses": {}}
        if op == "post_message":
            room = self._room_of(conn_id)
            return {"event_id": self._sync(self._record(agent, room.id, args["body"]))}
        raise OpError(404, f"unknown operation {op}")
