"""hearth-register: register Hearth's custom agents with a Switch server.

    hearth-register --all                  # pulse and both companions, from config
    hearth-register hearth-companion-a     # one agent
    hearth-register --set-base-url hearth-companion-b https://example.trycloudflare.com

Credentials go to .hearth/agents/<name>.json (git-ignored). The API key is
shown by Switch only once, so keep that file. To run a companion on another
laptop, copy its file to the same path in that laptop's checkout.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from hearth.config import ConfigError, env, load_config, load_dotenv, switch_base_url
from hearth.switch.client import AgentCredentials, SwitchError, register_agent

DESCRIPTIONS = {
    "pulse": ("Hearth Desk", "Hearth's pager and clock: turns alerts into pages and re-pages until someone acks."),
    "companion": ("Hearth", "Hearth's companion: talks with the parent, spots trouble, alerts the family."),
}


async def _register(names: list[str], *, overwrite: bool, pulse_name: str) -> int:
    base_url = switch_base_url()
    print(f"Switch API: {base_url}")
    key = env("SWITCH_REGISTRATION_KEY", required=True)
    status = 0
    for name in names:
        path = AgentCredentials.path_for(name)
        if path.exists() and not overwrite:
            print(f"{name}: already registered ({path}). Use --overwrite to issue a new key.")
            continue
        display, description = DESCRIPTIONS["pulse" if name == pulse_name else "companion"]
        try:
            creds = await register_agent(
                base_url, key, name=name, description=description, display_name=display,
                connection_model="always_on", overwrite=overwrite,
            )
        except SwitchError as exc:
            hint = ""
            if exc.status == 409:
                hint = " (the name exists on the server; rerun with --overwrite to issue a new key)"
            elif exc.status in (401, 403):
                hint = " (check SWITCH_REGISTRATION_KEY: it must be a registration key from the Gateway)"
            print(f"{name}: {exc}{hint}", file=sys.stderr)
            status = 1
            continue
        saved = creds.save()
        print(f"{name}: registered as {creds.agent_id}. Credentials saved to {saved}")
    return status


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Register Hearth's agents with Switch.")
    parser.add_argument("names", nargs="*", help="agent names to register")
    parser.add_argument("--all", action="store_true", help="register pulse and every companion in the config")
    parser.add_argument("--overwrite", action="store_true", help="re-register: issues a new API key")
    parser.add_argument("--set-base-url", nargs=2, metavar=("NAME", "URL"),
                        help="point saved credentials at another server address (e.g. a tunnel)")
    args = parser.parse_args(argv)
    load_dotenv()

    if args.set_base_url:
        name, url = args.set_base_url
        creds = AgentCredentials.load(name)
        creds.base_url = url.rstrip("/")
        print(f"{name}: base URL set to {creds.base_url} ({creds.save()})")
        return

    try:
        cfg = load_config()
    except ConfigError as exc:
        raise SystemExit(f"hearth-register: {exc}") from None
    names = list(args.names)
    if args.all:
        names = [cfg.agents.pulse, *cfg.agents.companions]
    if not names:
        parser.error("name at least one agent, or pass --all")
    try:
        raise SystemExit(asyncio.run(_register(names, overwrite=args.overwrite, pulse_name=cfg.agents.pulse)))
    except ConfigError as exc:
        raise SystemExit(f"hearth-register: {exc}") from None


if __name__ == "__main__":
    main()
