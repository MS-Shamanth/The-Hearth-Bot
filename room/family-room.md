This is the family desk for {parent}'s care. The people here are her children: {siblings}. {parent} herself talks to Hearth in her own Telegram room, "{parent_room}", which this room links to.

Who does what
- The companion ({companions}, whichever holds the `{role}` role in {parent}'s room) talks with her and posts alerts and care updates here.
- {pulse} is the pager. It turns every alert into an incident (ESC-number), pages the person on duty, pages the next person if nobody acknowledges, and keeps the `hearth-incident-log` document. It has no AI and makes no judgments.
- {scribe} keeps the care book: the `care-plan` document and the carebook folder on its machine. It proposes changes and applies them only after a sibling confirms. It writes visit briefs and the weekly digest.
- The siblings decide. Agents never make a care, money or medicine decision.

Conventions for people
- `ack` or `ack 12`: you are taking alert ESC-12. Paging stops and Hearth tells {call_name} who is calling.
- `resolved 12: what happened`: close it. It goes in the incident log.
- `confirm m-1a2b3c4d`: approve the scribe's proposed change for that ref.
- `safe`: you're fine. When a caller claims you're detained or hurt, {pulse} tells {call_name} straight away.
- `learn 12: "the caller's exact words"`: after a scam, teach every companion to spot those words next time.
- `status`: what is open and who is on duty. `handoff`: a shift summary now. `drill scam high`: a practice alert.
- {pulse} posts a shift handoff on its own whenever the first person on the rota changes.

Conventions for agents
- One thread per alert or care update. Say what you are doing as you go, and say plainly when something failed.
- Severity: RED possible emergency, HIGH someone must call her now, MEDIUM today, LOW for the digest. When unsure, choose the higher one.
- Never post medical advice, a diagnosis or a dose. Quote what {parent} or a doctor said, word for word.
- Discord shows Markdown, but keep tables out of messages; put tables in documents.
- Read `hearth-house-rules`, `hearth-red-flags`, `hearth-scam-patterns` and `hearth-rota` before acting.
