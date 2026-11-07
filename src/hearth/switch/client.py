"""Switch agent protocol client.

Wire contract (Switch Rooms docs, "The agent protocol" and "Agent connection"):

* Register once with ``POST /agents`` using a registration key. The response
  carries the agent's own API key, returned only once.
* Events arrive over ``GET /agents/{id}/connection/ws`` as JSON frames
  ``{"event", "data", "id"}``. The server sends ``ping`` every 2 seconds and
  the client must answer ``{"type": "pong", "cursor": n}`` straight away, or
  the connection lapses after 6 seconds and its room claim and role lease
  are released.
* Actions are HTTP: ``POST /agents/{id}/ops/{operation}`` with the arguments
  as the body and ``X-Switch-Connection-Id`` to bind the call to the room the
  connection claimed; ``POST /agents/{id}/message`` posts to any room the
  agent is a member of without a connection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from hearth.config import STATE_DIR

log = logging.getLogger(__name__)

CLIENT_NAME = "hearth"
CLIENT_VERSION = "0.1.0"


class SwitchError(RuntimeError):
    def __init__(self, status: int, detail: Any, *, what: str = "") -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"{what or 'Switch'} failed with {status}: {detail}")


class ConnectionStopped(RuntimeError):
    """The server ended the connection for good (taken over, refused, closed)."""


# ── Credentials ──────────────────────────────────────────────────────────────


@dataclass
class AgentCredentials:
    name: str
    agent_id: str
    api_key: str
    base_url: str

    @staticmethod
    def path_for(name: str) -> Path:
        return STATE_DIR / "agents" / f"{name}.json"

    @classmethod
    def load(cls, name: str) -> AgentCredentials:
        path = cls.path_for(name)
        if not path.exists():
            raise FileNotFoundError(
                f"No credentials for agent {name!r} at {path}. Run: hearth-register {name}"
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(**data)

    def save(self) -> Path:
        path = self.path_for(self.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.__dict__, indent=2), encoding="utf-8")
        return path


async def register_agent(
    base_url: str,
    registration_key: str,
    *,
    name: str,
    description: str,
    display_name: str | None = None,
    connection_model: str = "always_on",
    overwrite: bool = False,
) -> AgentCredentials:
    """``POST /agents``. Registering an existing name is a 409 unless overwrite,
    and overwrite issues a new API key, so the old one stops working."""
    body = {
        "name": name,
        "description": description,
        "display_name": display_name,
        "connector_type": "hearth-python",
        "integration_profile": {
            "connection_model": connection_model,
            "message_exchange": True,
            "pre_invocation_mediation": [],
            "post_invocation_mediation": [],
            "event_reporting": [],
        },
        "metadata": {"client": CLIENT_NAME, "client_version": CLIENT_VERSION},
        "overwrite": overwrite,
    }
    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=30) as http:
        response = await http.post(
            "/agents", json=body, headers={"Authorization": f"Bearer {registration_key}"}
        )
    if response.status_code >= 400:
        raise SwitchError(response.status_code, _detail(response), what="register")
    data = response.json()
    return AgentCredentials(name=name, agent_id=data["id"], api_key=data["api_key"], base_url=base_url)


def _detail(response: httpx.Response) -> Any:
    try:
        return response.json().get("detail", response.text)
    except (ValueError, AttributeError):
        return response.text


# ── HTTP actions ─────────────────────────────────────────────────────────────


@dataclass
class Media:
    data: bytes
    content_type: str
    filename: str


class SwitchAPI:
    """The HTTP half of the protocol, for one agent."""

    def __init__(
        self,
        creds: AgentCredentials,
        *,
        timeout: float = 60,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.creds = creds
        self.base_url = creds.base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={"Authorization": f"Bearer {creds.api_key}"},
            transport=transport,
        )

    @property
    def agent_id(self) -> str:
        return self.creds.agent_id

    @property
    def name(self) -> str:
        return self.creds.name

    async def close(self) -> None:
        await self._http.aclose()

    async def health(self) -> bool:
        try:
            response = await self._http.get("/health")
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def op(
        self, operation: str, args: dict[str, Any] | None = None, *, connection_id: str | None = None
    ) -> Any:
        headers = {"X-Switch-Connection-Id": connection_id} if connection_id else {}
        response = await self._http.post(
            f"/agents/{self.agent_id}/ops/{operation}", json=args or {}, headers=headers
        )
        if response.status_code >= 400:
            raise SwitchError(response.status_code, _detail(response), what=operation)
        return response.json().get("result")

    async def list_ops(self) -> dict[str, Any]:
        response = await self._http.get(f"/agents/{self.agent_id}/ops")
        response.raise_for_status()
        return response.json().get("operations", {})

    async def post_message(self, room_id: str, content: str) -> str | None:
        """Post to any room the agent belongs to. No thread support on this route."""
        response = await self._http.post(
            f"/agents/{self.agent_id}/message", json={"room_id": room_id, "content": content}
        )
        if response.status_code >= 400:
            raise SwitchError(response.status_code, _detail(response), what="post message")
        return response.json().get("event_id")

    async def set_typing(self, room_id: str, is_typing: bool) -> None:
        try:
            await self._http.post(
                f"/agents/{self.agent_id}/typing", json={"room_id": room_id, "is_typing": is_typing}
            )
        except httpx.HTTPError:
            log.debug("typing indicator failed", exc_info=True)

    async def participants(self, room_id: str) -> list[dict[str, Any]]:
        response = await self._http.get(f"/agents/rooms/{room_id}/participants")
        if response.status_code >= 400:
            raise SwitchError(response.status_code, _detail(response), what="participants")
        return response.json().get("participants", [])

    async def download_media(self, room_id: str, mxc: str) -> Media:
        response = await self._http.get(
            f"/agents/{self.agent_id}/rooms/{room_id}/media", params={"mxc": mxc}
        )
        if response.status_code >= 400:
            raise SwitchError(response.status_code, _detail(response), what="download media")
        disposition = response.headers.get("content-disposition", "")
        filename = "attachment"
        if "filename=" in disposition:
            filename = disposition.split("filename=", 1)[1].strip('"; ')
        return Media(
            data=response.content,
            content_type=response.headers.get("content-type", "application/octet-stream"),
            filename=filename,
        )

    async def upload_media(
        self,
        room_id: str,
        files: list[tuple[str, bytes, str]],
        *,
        caption: str | None = None,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        multipart = [("files", (name, data, mime)) for name, data, mime in files]
        form: dict[str, str] = {}
        if caption:
            form["caption"] = caption
        if thread_id:
            form["thread_id"] = thread_id
        response = await self._http.post(
            f"/agents/{self.agent_id}/rooms/{room_id}/media", files=multipart, data=form
        )
        if response.status_code >= 400:
            raise SwitchError(response.status_code, _detail(response), what="upload media")
        return response.json()


# ── Events ───────────────────────────────────────────────────────────────────


@dataclass
class MessageEvent:
    room_id: str
    sequence: int | None
    addressed: bool
    sender: str
    sender_name: str
    message_id: str
    body: str
    timestamp_ms: int
    thread_id: str | None
    attachments: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_frame(cls, data: dict[str, Any], sequence: int | None) -> MessageEvent:
        payload = data.get("payload") or {}
        return cls(
            room_id=str(data.get("room_id") or ""),
            sequence=sequence,
            addressed=bool(payload.get("addressed")),
            sender=str(payload.get("sender") or ""),
            sender_name=str(payload.get("sender_name") or ""),
            message_id=str(payload.get("message_id") or ""),
            body=str(payload.get("body") or ""),
            timestamp_ms=int(payload.get("timestamp") or 0),
            thread_id=payload.get("thread_id"),
            attachments=list(payload.get("attachments") or []),
        )


@dataclass
class Frame:
    event: str
    sequence: int | None
    data: dict[str, Any]


FrameHandler = Callable[[Frame], Awaitable[None]]

# Close codes after which reconnecting is wrong (Switch docs, "Agent connection").
# 4409 covers a takeover, a protocol mismatch and a room already claimed.
_FINAL_CLOSE_CODES = {
    4400: "bad connection parameters",
    4401: "missing or invalid API key",
    4403: "a declared room can't be served (is the agent a member?)",
    4409: "taken over, room already claimed, or too many connections",
}


class AgentConnection:
    """One agent connection over its WebSocket.

    Pings are answered on the reader task itself, before any event is
    handled, so a slow handler (an LLM call) can never make the connection
    lapse. Events go through a queue to a separate worker.
    """

    def __init__(
        self,
        api: SwitchAPI,
        *,
        rooms: list[str] | None = None,
        scope: str = "single",
        delivery_filter: str = "all",
        on_connected: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.api = api
        self.on_connected = on_connected
        self.connection_id = str(uuid.uuid4())
        self.rooms = rooms or []
        self.scope = scope
        self.filter = delivery_filter
        self.cursor: int | None = None
        self.generation: int | None = None
        self.ready = asyncio.Event()
        self.state: dict[str, Any] = {}
        self._queue: asyncio.Queue[Frame] = asyncio.Queue()
        self._stopped = False

    def _url(self) -> str:
        params: dict[str, str] = {
            "connection_id": self.connection_id,
            "scope": self.scope,
            "filter": self.filter,
            "client": CLIENT_NAME,
            "client_version": CLIENT_VERSION,
        }
        if self.rooms:
            params["rooms"] = ",".join(self.rooms)
        if self.cursor is not None:
            params["start_from"] = str(self.cursor)
        if self.generation is not None:
            params["expected_generation"] = str(self.generation)
        base = self.api.base_url
        ws_base = "wss" + base[len("https") :] if base.startswith("https") else "ws" + base[len("http") :]
        return f"{ws_base}/agents/{self.api.agent_id}/connection/ws?{urlencode(params)}"

    async def run(self, handler: FrameHandler) -> None:
        worker = asyncio.create_task(self._work(handler))
        backoff = 1.0
        try:
            while not self._stopped:
                try:
                    await self._read_once()
                    backoff = 1.0
                except ConnectionStopped:
                    raise
                except ConnectionClosed as exc:
                    code = exc.rcvd.code if exc.rcvd is not None else None
                    if code in _FINAL_CLOSE_CODES:
                        raise ConnectionStopped(
                            f"closed with {code}: {_FINAL_CLOSE_CODES[code]}"
                        ) from exc
                    log.warning("connection closed (%s); reconnecting in %.0fs", code, backoff)
                except InvalidStatus as exc:
                    status = exc.response.status_code
                    if status in {401, 403, 404}:
                        raise ConnectionStopped(f"handshake refused with HTTP {status}") from exc
                    log.warning("handshake failed (HTTP %s); retrying in %.0fs", status, backoff)
                except (OSError, InvalidHandshake) as exc:
                    log.warning("connection dropped (%s); reconnecting in %.0fs", exc, backoff)
                self.ready.clear()
                if self._stopped:
                    break
                await asyncio.sleep(backoff + random.random())
                backoff = min(backoff * 2, 30.0)
        finally:
            worker.cancel()

    def stop(self) -> None:
        self._stopped = True

    async def _read_once(self) -> None:
        headers = {"Authorization": f"Bearer {self.api.creds.api_key}"}
        async with ws_connect(
            self._url(), additional_headers=headers, max_size=8 * 1024 * 1024
        ) as socket:
            async for raw in socket:
                message = json.loads(raw)
                event = message.get("event")
                data = message.get("data") or {}
                if event == "ping":
                    await socket.send(json.dumps({"type": "pong", "cursor": self.cursor or 0}))
                    continue
                if event == "connection_state":
                    self.state = data
                    self.generation = data.get("generation", self.generation)
                    if self.cursor is None and data.get("cursor") is not None:
                        self.cursor = int(data["cursor"])
                    self.ready.set()
                    log.info(
                        "%s connected: rooms=%s generation=%s",
                        self.api.name, data.get("rooms"), self.generation,
                    )
                    if self.on_connected is not None:
                        # Through the event queue, so it runs before any event
                        # that arrives on this connection and never blocks pings.
                        await self._queue.put(Frame(event="_connected", sequence=None, data={}))
                    continue
                if event == "refused":
                    raise ConnectionStopped(f"refused: {data}")
                if event == "evicted":
                    code = data.get("code")
                    log.warning("%s evicted: %s (%s)", self.api.name, code, data.get("reason"))
                    if code in {"taken_over", "closed"}:
                        raise ConnectionStopped(f"evicted: {code}: {data.get('reason')}")
                    return  # heartbeat_lapsed and the rest: reconnect and resume
                if event == "gap":
                    log.warning("%s gap in events: %s", self.api.name, data.get("reason"))
                    if data.get("resumed_at") is not None:
                        self.cursor = int(data["resumed_at"])
                    continue
                sequence = message.get("id")
                if sequence is not None:
                    self.cursor = int(sequence)
                await self._queue.put(Frame(event=str(event), sequence=sequence, data=data))

    async def _work(self, handler: FrameHandler) -> None:
        while True:
            frame = await self._queue.get()
            try:
                if frame.event == "_connected":
                    if self.on_connected is not None:
                        await self.on_connected()
                    continue
                await handler(frame)
            except Exception:  # noqa: BLE001 - one bad event must not stop the agent
                log.exception("handler failed on %s frame", frame.event)

    async def op(self, operation: str, args: dict[str, Any] | None = None) -> Any:
        """An operation bound to this connection's room."""
        return await self.api.op(operation, args, connection_id=self.connection_id)


def room_name_matches(actual: str, wanted: str) -> int:
    """How well a room's name matches the configured one: 3 exact, 2 any case,
    1 with the bridge prefix Switch adds to adopted channels ("Discord: x",
    "Telegram: x"), 0 no match."""
    if actual == wanted:
        return 3
    a, w = actual.strip().lower(), wanted.strip().lower()
    if a == w:
        return 2
    if ": " in a and a.split(": ", 1)[1] == w:
        return 1
    return 0


def pick_room(rooms: list[dict[str, Any]], name: str) -> str | None:
    best = max(
        ((room_name_matches(str(r.get("name", "")), name), r) for r in rooms if not r.get("archived")),
        key=lambda pair: pair[0], default=(0, None),
    )
    return best[1]["room_id"] if best[0] else None


async def find_room_id(api: SwitchAPI, name: str) -> str | None:
    """Look up a room the agent belongs to by its configured name."""
    return pick_room(list(await api.op("list_rooms") or []), name)
