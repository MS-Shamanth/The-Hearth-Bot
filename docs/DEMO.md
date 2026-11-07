# Demo script (about 2 minutes, plus optional scenes)

The same script works with the two companions on two laptops or in two terminal panes on one laptop. Only the failover trigger in Act II differs.

## Pick a layout

| | Layout A: two laptops | Layout B: one laptop, two panes |
|---|---|---|
| Switch server, `hearth-pulse`, scribe | Laptop 1 (stays open the whole time) | This laptop |
| `hearth-companion-a` (answers first) | Laptop 2, through the tunnel in [SETUP.md §8](SETUP.md#8-the-second-laptop-for-the-real-failover-demo) | Left terminal pane |
| `hearth-companion-b` (standby) | Laptop 1 | Right terminal pane |
| Failover trigger | Close laptop 2's lid, or turn off its Wi-Fi | Click the left pane and press `Ctrl+C`, or close it with `Ctrl+Shift+W` |
| What Switch sees | companion-a stops answering pings | companion-a stops answering pings |

Either way, Switch sends a ping every 2 seconds and releases the `companion` role lease 6 seconds after the last answered ping. The standby checks the role every 3 seconds, so the takeover message appears about 10 seconds after the trigger. A clean `Ctrl+C` goes through the same lapse: the lease stays with a dropped connection until its liveness runs out.

Layout B in Windows Terminal: open a tab in the repository folder, run companion-a, press `Alt+Shift+D` to split the pane, and run companion-b in the new pane.

```powershell
# left pane (start this first so it takes the role)
uv run hearth-companion --name hearth-companion-a
# right pane, after the left one logs "took the companion role"
uv run hearth-companion --name hearth-companion-b
```

## Before recording

- [ ] `HEARTH_DEMO=1` in `.env` on the machine running `hearth-pulse`, so a missed ack re-pages after 45 seconds.
- [ ] Start in this order: `hearth-pulse`, then companion-a, then companion-b. Wait for "🟢 hearth-companion-a is online" in Discord before starting companion-b.
- [ ] `!roles` in the Telegram group shows `companion` held by `hearth-companion-a`.
- [ ] Clear old incidents: `status`, then `resolved <n>` for anything open.
- [ ] Props: a fake prescription to photograph (Amlodipine 5 mg, 1-0-0), and the scam message typed out on Amma's phone.
- [ ] Optional scenes: a voice note recorded on Amma's phone; `checkins.quiet_check.expect_by` set two minutes ahead (and `reply_within_minutes: 1`), then restart the companions.
- [ ] Screen: Discord `#hearth-family-desk` on the left, Telegram "Amma Hearth" on the right (Telegram Desktop, or Amma's phone mirrored), terminals small at the bottom. In layout B, keep both companion panes visible.

## Act I: the "digital arrest" scam (0:00 to 0:50)

| Who | Does | The judges see |
|---|---|---|
| Voice-over | "Amma lives alone in Mysuru. Shamanth is in Bengaluru, Shreya in Berlin." | Two apps, one Switch server. |
| Amma | "A CBI officer called. My Aadhaar is in a money laundering case. I must stay on video and transfer 2 lakh for verification." | An instant fixed reply: "idu scam… Duddu kalisbedi, OTP kodbedi… Call cut maadi." No LLM in the path. |
| Discord | | `ESC-n · HIGH · scam`, paged to @ms_shamanth. |
| Shamanth | Does nothing (he's "in a meeting"). | After 45 s: "no ack from @ms_shamanth", paged to @shreyabj. |
| Shreya | `ack` | Telegram: "Amma, Shreya nimge eega call maadtare. 🙏" |

## Act II: zero-downtime failover (0:50 to 1:20)

| Who | Does | The judges see |
|---|---|---|
| Presenter | `!roles` in Telegram | `companion` held by `hearth-companion-a`. |
| Presenter | Layout A: close laptop 2's lid. Layout B: `Ctrl+C` in the left pane. | About 10 s later in Discord: "🔁 hearth-companion-b took over Amma's chat". |
| Amma | "Thank you kanda, I hung up" | An answer from companion-b, which read the room history and knows about the call. `!roles` shows the new holder. |

## Act III: prescription to git commit (1:20 to 2:00)

| Who | Does | The judges see |
|---|---|---|
| Amma | Sends the prescription photo | "photo bantu, thanks…" In Discord: `🩺 Hearth care update · meds` with what was read, and the photo. |
| Scribe | | A `diff` against `care-plan.md` and "Reply `confirm m-…` to commit". |
| Shreya | `confirm m-…` | The scribe updates `care-plan`, appends to `decisions.md`, commits, and says who confirmed. |
| Close | `resolved n: she hung up, nothing shared`, then show `hearth-incident-log` | "An on-call rotation for the people who raised you. Built on Switch." |

## Optional scenes (pick what fits the time)

- **Voice note.** Amma records "ede novu aagtide" (chest pain). The companion transcribes it, replies with the 108 instruction, pages everyone (RED) and attaches her voice note in Discord.
- **"We have detained your son".** Amma: "Police called, they said Shamanth is arrested, send 50000 for bail." Fixed reply, then in Discord a 🛡️ message asks @ms_shamanth to reply `safe`, showing when he was last active. He replies `safe`; Telegram: "Shamanth chennagiddare, safe aagiddare…"
- **Teach it the scam.** After resolving, `learn n: "your son is in police custody"`. Pulse rewrites `hearth-scam-patterns` in both rooms; within 2 minutes the companions flag those words without the LLM.
- **Quiet morning.** With `expect_by` two minutes ahead and Amma silent, the companion sends "Amma, kaafi aaytha?"; a minute later Discord shows a MEDIUM `no-signal` alert.
- **Shift handoff.** `handoff` in Discord: "🌅 Shift handoff: you're now first on call… Last 8h with Amma: …". It also posts by itself when the rota flips.

## Rehearsal without Telegram

In Discord: `drill scam high` runs the paging loop, `drill fall red` pages everyone at once, `drill relative-in-trouble high` runs the 🛡️ flow (it never messages Amma), `status` shows who is on duty and what is open.

## If something goes wrong on camera

- The companion is slow: the LLM is thinking. Emergencies and scams don't wait for it; small talk does.
- Nobody gets pinged: check `siblings[].handle` against `hearth-setup --participants`.
- No takeover after 15 seconds: check that companion-b is still running and that `!roles` shows the role free. In layout B, make sure you stopped the left pane, not the right one.
- The right pane holds the role instead of the left: companion-b started first. Stop companion-b, wait for "🔁 hearth-companion-a took over", then start companion-b again. Only the role holder ever answers, so Amma never gets two replies.
