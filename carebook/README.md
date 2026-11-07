# Carebook

The scribe's working folder. In Switch Console, create the `hearth-scribe` agent with this folder as its directory (or a copy of it), so the scribe keeps the care book in git.

All data here is fictional demo data for a parent called Amma.

- `care-plan.md`: current medicines, doctors, conditions, follow-ups. The source of truth. Changed only after a sibling confirms.
- `decisions.md`: every confirmed change, with who confirmed it.
- `log/YYYY-MM-DD.md`: one file per day, from the companion's "Today with Amma" summaries.
- `visits/`: briefs generated for doctor visits (`python tools/visit_brief.py`).
