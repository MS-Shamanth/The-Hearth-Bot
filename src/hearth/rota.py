"""Who is on duty right now: follow-the-sun across the siblings' time zones."""

from __future__ import annotations

from datetime import datetime, timezone

from hearth.config import Sibling


def is_on_duty(sibling: Sibling, now_utc: datetime) -> bool:
    local = now_utc.astimezone(sibling.timezone)
    return sibling.on_duty.contains(local.time().replace(tzinfo=None))


def paging_order(siblings: tuple[Sibling, ...] | list[Sibling], now_utc: datetime | None = None) -> list[Sibling]:
    """Everyone, on-duty people first. Config order breaks ties, so the
    family decides who is first when two people are both awake."""
    now_utc = now_utc or datetime.now(timezone.utc)
    on = [s for s in siblings if is_on_duty(s, now_utc)]
    off = [s for s in siblings if not is_on_duty(s, now_utc)]
    return on + off


def describe(siblings: tuple[Sibling, ...] | list[Sibling], now_utc: datetime | None = None) -> str:
    """A short human summary for the rota document and `status` replies."""
    now_utc = now_utc or datetime.now(timezone.utc)
    lines = []
    for sibling in paging_order(siblings, now_utc):
        local = now_utc.astimezone(sibling.timezone)
        state = "on duty" if is_on_duty(sibling, now_utc) else "off duty"
        window = f"{sibling.on_duty.start:%H:%M}-{sibling.on_duty.end:%H:%M}"
        lines.append(
            f"- {sibling.name} (@{sibling.handle}): {state}, local time {local:%a %H:%M} "
            f"{sibling.timezone.key}, covers {window}"
        )
    return "\n".join(lines)
