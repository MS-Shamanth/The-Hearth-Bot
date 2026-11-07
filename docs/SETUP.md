# Set up Hearth

About an hour the first time, most of it Switch Console's first download. Written for Windows; macOS and Linux differ only in the shell commands.

You will end up with:

- a Switch server running in Docker on laptop 1, with Telegram and Discord connected;
- a Telegram group "Amma Hearth" (the parent's room) and a Discord channel `#hearth-family-desk` (the siblings' room);
- `hearth-pulse` and the standby `hearth-companion-b` on laptop 1, and `hearth-companion-a` (the one that answers first) on laptop 2;
- `hearth-scribe`, a Codex or Claude Code agent run by Switch Console on laptop 1.

Laptop 1 hosts the server, so it stays on. The failover demo closes laptop 2.

## 1. Install the tools (laptop 1)

| Tool | How | Check |
|---|---|---|
| Docker Desktop | [docker.com](https://docs.docker.com/get-started/get-docker/), then start it | `docker info` prints a server version |
| Switch Console | Windows: [switch-console-x64.exe](https://github.com/sandbox-quantum/switch/releases/download/switch-console-v0.40.1/switch-console-x64.exe) (v0.40.1, early access on Windows). Other platforms: the [release page](https://github.com/sandbox-quantum/switch/releases/tag/switch-console-v0.40.1) | Console opens |
| uv | `powershell -c "irm https://astral.sh/uv/install.ps1 \| iex"` | `uv --version` |
| Codex CLI (for the scribe) | `npm install -g @openai/codex`, then `codex login` | `codex --version` |
| An LLM key for the companion | A free Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey), or any OpenAI-compatible endpoint | `uv run hearth-companion --check-llm` (step 7) |

Claude Code works for the scribe instead of Codex: `npm install -g @anthropic-ai/claude-code`, then `claude auth login`.

If Switch Console says **CLI not installed** for Codex after you install it, quit Console completely and open it again. It reads PATH once at start.

## 2. Run a Switch server

1. Open Switch Console, select **Add a server**, then **Run a server on this computer**.
2. Wait for **Docker is ready.**, then **Start**. The first start downloads a few GB. When it's done, the sidebar reads **Running locally**.
3. Find the agent bridge, which Hearth's custom agents talk to. Console picks the port and writes it to `%APPDATA%\switchdash\local-switch-server\ports.json` (`"api"`, for example 64729). Hearth reads that file itself when `SWITCH_BASE_URL` is empty. To check:

   ```powershell
   Get-Content "$env:APPDATA\switchdash\local-switch-server\ports.json"
   curl.exe http://127.0.0.1:64729/health      # use your "api" port
   ```

4. Create a registration key in Switch Console: **Settings** (bottom left, `Ctrl+L`) > **API keys** > **Create registration key**. You become the owner of every agent registered with it. Copy it; it is shown once.

## 3. Connect Discord (the siblings' room)

Follow [Connect Discord](https://docs.switchagents.ai/switch-rooms/deploy/messaging-apps/discord.md). In short:

1. [Discord Developer Portal](https://discord.com/developers/applications) > **New Application** > **Bot**: copy the token. Turn on **Server Members Intent** and **Message Content Intent**, then **Save Changes**.
2. **OAuth2 > URL Generator**: scopes `bot` and `applications.commands`; permissions View Channels, Send Messages, Send Messages in Threads, Manage Webhooks, Manage Channels, Manage Roles, Read Message History, Attach Files, Add Reactions. Open the URL and add the bot to your family's Discord server.
3. Discord **User Settings > Developer** (older clients: **Advanced**) > **Developer Mode** on, then right-click the server icon > **Copy Server ID**.
4. Switch Console > server **Home** > **Messaging apps** > **Connect** > **Discord**. Name the connection `Discord`, paste the token and the server id (Guild Id), **Connect**, then **Link my account** and pick yourself.
5. In Discord, create a private channel `hearth-family-desk`, add both siblings, and post any message. Posting is what creates the Switch room.

Each sibling links their own Discord account (server **Home** > **Link my account**) so Hearth can page them by name.

## 4. Connect Telegram (the parent's room)

Follow [Connect Telegram](https://docs.switchagents.ai/switch-rooms/deploy/messaging-apps/telegram.md). In short:

1. In Telegram, message [@BotFather](https://t.me/BotFather): `/newbot`, give it a name ("Hearth") and a username ending in `bot`. Save the token.
2. **Before adding the bot anywhere**: `/mybots` > your bot > **Bot Settings** > **Group Privacy** > **Turn off**.
3. Switch Console > **Messaging apps** > **Connect** > **Telegram**: paste the token and the bot username.
4. Create a Telegram group titled exactly `Amma Hearth` with the parent's account and the bot. For the demo, use a second Telegram account to play Amma (a teammate's phone works).
5. Post one message in the group. Switch creates the room. Link your own Telegram account in Console if you're in the group too.

## 5. Create the scribe (Switch Console)

1. Copy the care book somewhere outside the repository and make it a git repo, so the scribe's commits stay separate:

   ```powershell
   Copy-Item -Recurse carebook "$env:USERPROFILE\hearth-carebook"
   git -C "$env:USERPROFILE\hearth-carebook" init
   git -C "$env:USERPROFILE\hearth-carebook" add -A
   git -C "$env:USERPROFILE\hearth-carebook" commit -m "Amma's care book"
   pip install fpdf2   # for the visit brief PDF, in the Python the scribe will use
   ```

2. Console > **Your Agents** > the dashed **+** card:
   - Name `hearth-scribe`, description "Keeps Amma's care book".
   - Agent instructions: paste [agents/scribe/AGENT.md](../agents/scribe/AGENT.md).
   - Run location **This computer**, directory `%USERPROFILE%\hearth-carebook`.
   - Provider **Codex** (or Claude Code).
3. On the agent's page, set **Who can talk to your agent** to **Anyone** (or custom rules naming the family room), so both siblings and the companions can address it.

The scribe's folder holds only the care book. It never sees Hearth's credentials in `.hearth/`.

## 6. Configure Hearth (laptop 1)

```bash
cd "Flip Switch"           # this repository
uv sync
cp .env.example .env
cp config/hearth.example.yaml config/hearth.yaml
```

Edit `.env`:

- `SWITCH_REGISTRATION_KEY` from step 2.
- `HEARTH_LLM_API_KEY`. The default model is `gemini-flash-latest`, with `gemini-2.5-flash` as the fallback if the alias isn't available. To pin the stable model, set `HEARTH_LLM_MODEL=gemini-2.5-flash`. For another provider, set `HEARTH_LLM_BASE_URL` and `HEARTH_LLM_MODEL` (and `HEARTH_LLM_FALLBACK_MODELS`, comma-separated).
- Voice notes work with the Gemini key and nothing else. To use Whisper instead (OpenAI or Groq), set `HEARTH_STT_BASE_URL`, `HEARTH_STT_API_KEY` and `HEARTH_STT_MODEL` as shown in `.env.example`.

Edit `config/hearth.yaml`: names, time zones, `on_duty` windows, and the room names if yours differ. Leave the handles for now.

## 7. Register, set up, run

```bash
uv run hearth-companion --check-llm       # tries each model in turn; says whether voice notes are on
uv run hearth-register --all              # hearth-pulse, hearth-companion-a, hearth-companion-b
uv run hearth-setup --participants        # configures both rooms, lists who Switch sees
```

`hearth-setup` prints the people in each room. Put the siblings' names exactly as printed into `siblings[].handle` in `config/hearth.yaml`. If the parent's Telegram group has other people in it, set `parent.handle` to her name as printed. Run `uv run hearth-setup` again after editing; it is safe to repeat.

Setup adds pulse and both companions to the rooms, writes both rooms' instructions, defines the exclusive `companion` role, creates the shared documents and links the rooms. If it reports that pulse can't change its own "who can talk" setting, set **Who can talk to your agent** to **Anyone** for `hearth-pulse` in Console or the Gateway.

Then, in separate terminals (the companion that starts first takes the role; on one laptop, two side-by-side panes work, see [DEMO.md](DEMO.md#pick-a-layout)):

```bash
uv run hearth-pulse
uv run hearth-companion --name hearth-companion-a    # or on laptop 2, below
uv run hearth-companion --name hearth-companion-b    # the standby
```

In Discord you should see "💓 hearth-pulse is on the desk", "🟢 hearth-companion-a is online", and the scribe's care-plan summary. Type `drill scam high` in `#hearth-family-desk` to watch a page, then `ack`.

## 8. The second laptop (for the real failover demo)

The local server listens on `127.0.0.1` only, so laptop 2 needs a tunnel to laptop 1's port 8000. A quick Cloudflare tunnel works:

```powershell
# laptop 1
winget install Cloudflare.cloudflared
cloudflared tunnel --url http://127.0.0.1:8000     # prints https://<random>.trycloudflare.com
```

On laptop 2: clone the repository, `uv sync`, copy `.env` (only the LLM settings are needed) and `config/hearth.yaml`, then copy `.hearth/agents/hearth-companion-a.json` from laptop 1 to the same path, and point it at the tunnel:

```powershell
uv run hearth-register --set-base-url hearth-companion-a https://<random>.trycloudflare.com
uv run hearth-companion --name hearth-companion-a
```

Start companion-a on laptop 2 before companion-b on laptop 1, so laptop 2 holds the role. Closing laptop 2's lid then shows the failover.

The tunnel exposes the agent bridge to the internet. Every call needs an agent API key, but stop the tunnel when you're done. No second laptop? Run both companions on laptop 1 and stop companion-a's terminal to show the failover.

## Troubleshooting

| You see | Do |
|---|---|
| `Switch is not answering at .../health` | Console shows **Running locally**? Docker running? Check the port with `docker ps`. |
| `hearth-register` 401/403 | The key must be a **registration** key from the Gateway's API Keys page. |
| `No room named 'Amma Hearth'` | The Telegram group title must match `rooms.parent_room`, and someone must have posted in it. |
| The bot posts that it can only see mentions | Group Privacy was on when the bot joined. Turn it off in BotFather, remove the bot from the group and add it back. |
| Pages don't ping anyone in Discord | `siblings[].handle` must match the names `hearth-setup --participants` prints, and each sibling must have linked their Discord account. |
| The scribe refuses messages from a sibling or the companion | Set the scribe's **Who can talk to your agent** to **Anyone**. |
| `4409` and the companion stops | Another process is running the same companion. Stop the other one. |
