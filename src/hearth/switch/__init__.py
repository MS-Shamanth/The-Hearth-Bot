"""A small client for the Switch agent protocol (WebSocket events, HTTP actions)."""

from hearth.switch.client import (
    AgentConnection,
    AgentCredentials,
    Media,
    MessageEvent,
    SwitchAPI,
    SwitchError,
    register_agent,
)

__all__ = [
    "AgentConnection",
    "AgentCredentials",
    "Media",
    "MessageEvent",
    "SwitchAPI",
    "SwitchError",
    "register_agent",
]
