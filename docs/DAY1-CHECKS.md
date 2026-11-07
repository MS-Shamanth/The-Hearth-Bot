# Checks against a live Switch server

Hearth's protocol code follows the Switch docs and source (read on 2026-11-04), and its unit and end-to-end tests run against an in-memory fake of the parts Hearth uses. These are the behaviours only a live server can confirm. Tick each one off the first time you run Hearth.

| # | Check | How | If it fails |
|---|---|---|---|
| 1 | The agent bridge is on port 8000 | `curl.exe http://127.0.0.1:8000/health` returns 200 | Find the `switch` container's port with `docker ps`; set `SWITCH_BASE_URL` |
| 2 | Registration works with a Gateway registration key | `uv run hearth-register --all` prints three agent ids | 401/403: wrong key type. Try the deployment's bootstrap key (`AGENT_REGISTRATION_TOKEN` in Console's server `.env`) |
| 3 | Pulse can add itself and the companions to both rooms | `hearth-setup` logs "added ... to room" with no warnings | Add the agents by hand: `!invite-agent @hearth-pulse` (and the companions) in each room, then rerun setup |
| 4 | Pulse may write the rooms' instructions, role and documents | No "permission" errors in `hearth-setup` | The rooms must be owned by the same Switch user who created the registration key, or that user must be an admin |
| 5 | Pulse may set its own addressing to Anyone | No "could not set ... addressing" warning | Set **Who can talk to your agent: Anyone** for hearth-pulse in Console or the Gateway |
| 6 | The companion receives Amma's messages without an @mention | Amma writes "hello"; the companion answers | Run the companion with `-v` and look for `message` frames. If none arrive, check the connection's `rooms` in the "connected" log line |
| 7 | Agents and people are told apart on Telegram | The companion doesn't answer its own messages or pulse's | Set `parent.handle` to Amma's sender name (shown in `-v` logs) |
| 8 | Pages ping the right people in Discord | `drill scam high`: the first person gets a Discord notification | Fix `siblings[].handle`; each sibling links their Discord account |
| 9 | Telegram photos reach the companion and relay to Discord | Send a photo; a care update with the photo appears in Discord | Without vision, set a vision-capable `HEARTH_LLM_MODEL`; the photo still relays |
| 10 | The role moves when the holder dies | Stop companion-a; within ~10 s companion-b announces the takeover | Check `!roles` in Telegram. Leases follow the heartbeat (pings every 2 s, lapse after 6 s) |
| 11 | The watchdog pages when no companion is online | Stop both companions; after 30 s pulse opens a `companion-offline` incident | Check `escalation.companion_offline_seconds` |
| 12 | The scribe acts on messages from pulse and the companions | The scribe answers the kickoff and the care update | Scribe's **Who can talk to your agent** must allow the companions and both siblings |
| 13 | Telegram voice notes reach the companion as audio | Send a voice note; the companion logs "voice note transcribed by …" | Run with `-v` and check the attachment's `mimetype`/`msgtype`. If transcription fails, the note still reaches Discord as a MEDIUM alert with the audio |
| 14 | Pulse can read the parent's room without connecting to it | `handoff` in Discord lists Amma's recent messages | If it says "no messages from her" when there were some, `read_context` with `room_id` was refused; check pulse's membership of her room |
| 15 | Pulse can edit its document in the parent's room through a second connection | `learn n: "…"` reports "in both rooms" | If it says "the family room" only, check the warning in pulse's log. Companions read the parent room's copy, so rerun `hearth-setup`: it rewrites that copy with every learned phrase |
| 16 | The default model answers | `uv run hearth-companion --check-llm` shows OK for at least one model | Set `HEARTH_LLM_MODEL=gemini-2.5-flash`, or another model your key can use |
