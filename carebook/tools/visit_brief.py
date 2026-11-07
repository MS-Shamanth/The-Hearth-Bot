"""Build a one-page brief for a doctor's visit from the care book.

    python tools/visit_brief.py --doctor "Dr. Rao" --date 2026-11-12 \
        --question "Can she take something for the knee pain?"

Reads care-plan.md and log/*.md (relative to the carebook folder), writes
visits/brief-<date>-<doctor>.html and, when fpdf2 is installed, a PDF too.
Standalone on purpose: the scribe's folder holds only the care book, never
Hearth's credentials.

The brief collects what the family logged. It is not medical advice.
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

CAREBOOK = Path(__file__).resolve().parents[1]

_BP = re.compile(r"\bBP\D{0,6}(\d{2,3})\s*/\s*(\d{2,3})", re.IGNORECASE)
_SUGAR = re.compile(r"\bsugar\D{0,10}(\d{2,3})", re.IGNORECASE)
_SYMPTOMS = {
    "knee or joint pain": re.compile(r"\bknee|joint", re.IGNORECASE),
    "pain (other)": re.compile(r"\b(pain|hurts|ache)\b", re.IGNORECASE),
    "dizziness": re.compile(r"\b(dizzy|giddy|talesuttu)\b", re.IGNORECASE),
    "low mood or loneliness": re.compile(r"\b(low|lonely|sad|bejaar)\b", re.IGNORECASE),
    "sleep problems": re.compile(r"\b(couldn't sleep|no sleep|slept badly|insomnia)\b", re.IGNORECASE),
    "missed or skipped doses": re.compile(r"\b(missed|skipped|forgot)\b.*\b(tablet|dose|medicine)", re.IGNORECASE),
    "fall or near-fall": re.compile(r"\b(fell|fall|slipped)\b", re.IGNORECASE),
}
_FONT_CANDIDATES = [
    Path("C:/Windows/Fonts/segoeui.ttf"),
    Path("C:/Windows/Fonts/arial.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/Library/Fonts/Arial Unicode.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
]


@dataclass
class Brief:
    title: str
    doctor: str
    visit: date
    since: date
    sections: dict[str, list[str]]
    readings: list[tuple[date, str, str]] = field(default_factory=list)  # (day, bp, sugar)
    symptoms: dict[str, list[date]] = field(default_factory=dict)
    notes: list[tuple[date, str]] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)


def parse_care_plan(text: str) -> tuple[str, dict[str, list[str]]]:
    title = "Care plan"
    sections: dict[str, list[str]] = defaultdict(list)
    current = "General"
    for line in text.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
        elif line.startswith("## "):
            current = line[3:].strip()
        elif line.strip().startswith("- "):
            sections[current].append(line.strip()[2:])
    return title, dict(sections)


def read_logs(folder: Path, since: date, until: date) -> list[tuple[date, str]]:
    entries = []
    for path in sorted(folder.glob("*.md")):
        try:
            day = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if not since <= day <= until:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("- "):
                entries.append((day, line.strip()[2:]))
    return entries


def build(carebook: Path, doctor: str, visit: date, since: date | None, questions: list[str]) -> Brief:
    title, sections = parse_care_plan((carebook / "care-plan.md").read_text(encoding="utf-8"))
    since = since or visit - timedelta(days=30)
    notes = read_logs(carebook / "log", since, visit)
    brief = Brief(title=title, doctor=doctor, visit=visit, since=since, sections=sections,
                  notes=notes, questions=list(questions))
    per_day: dict[date, list[str]] = defaultdict(lambda: ["", ""])
    for day, text in notes:
        for match in _BP.finditer(text):
            per_day[day][0] = f"{match.group(1)}/{match.group(2)}"
        for match in _SUGAR.finditer(text):
            per_day[day][1] = match.group(1)
        for label, pattern in _SYMPTOMS.items():
            if label == "pain (other)" and _SYMPTOMS["knee or joint pain"].search(text):
                continue  # already counted as knee pain
            if pattern.search(text):
                brief.symptoms.setdefault(label, [])
                if day not in brief.symptoms[label]:
                    brief.symptoms[label].append(day)
    brief.readings = [(day, bp, sugar) for day, (bp, sugar) in sorted(per_day.items()) if bp or sugar]
    for item in sections.get("Follow-ups", []):
        if "ask" in item.lower() and item not in brief.questions:
            brief.questions.append(item)
    return brief


def _days(days: list[date]) -> str:
    return ", ".join(f"{d:%d %b}" for d in sorted(days))


def _usual(lines: list[str]) -> str:
    return "; ".join(line.rstrip(".") for line in lines)


def to_html(brief: Brief) -> str:
    e = html.escape
    parts = [
        "<!doctype html><meta charset='utf-8'><title>Visit brief</title>",
        "<style>body{font-family:system-ui,'Noto Sans Kannada',sans-serif;max-width:46em;margin:2em auto;"
        "line-height:1.4}h1{font-size:1.4em}h2{font-size:1.1em;margin-top:1.2em;border-bottom:1px solid #ccc}"
        "table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:.2em .6em}"
        "footer{margin-top:2em;color:#555;font-size:.9em}</style>",
        f"<h1>Visit brief · {e(brief.title)}</h1>",
        f"<p><b>{e(brief.doctor)}</b> · visit on {brief.visit:%d %B %Y} · covers {brief.since:%d %b} to {brief.visit:%d %b}</p>",
    ]
    for name in ("About", "Conditions", "Medicines"):
        if brief.sections.get(name):
            parts.append(f"<h2>{e(name)}</h2><ul>" + "".join(f"<li>{e(x)}</li>" for x in brief.sections[name]) + "</ul>")
    if brief.readings:
        parts.append("<h2>Readings logged</h2><table><tr><th>Date</th><th>BP</th><th>Sugar</th></tr>")
        parts += [f"<tr><td>{d:%d %b}</td><td>{e(bp)}</td><td>{e(s)}</td></tr>" for d, bp, s in brief.readings]
        parts.append("</table>")
        usual = brief.sections.get("Usual readings")
        if usual:
            parts.append("<p>Usual for her: " + e(_usual(usual)) + "</p>")
    if brief.symptoms:
        parts.append("<h2>What she reported</h2><ul>")
        parts += [f"<li>{e(k)}: {len(v)} day(s) ({_days(v)})</li>" for k, v in brief.symptoms.items()]
        parts.append("</ul>")
    if brief.questions:
        parts.append("<h2>Questions from the family</h2><ol>" + "".join(f"<li>{e(q)}</li>" for q in brief.questions) + "</ol>")
    if brief.notes:
        parts.append("<h2>Daily notes</h2><ul>" + "".join(f"<li>{d:%d %b}: {e(t)}</li>" for d, t in brief.notes) + "</ul>")
    parts.append("<footer>Prepared by Hearth from the family's care log, for discussion with the doctor. "
                 "Not medical advice. Medicines are copied from the care plan; please check them against the "
                 "prescription.</footer>")
    return "\n".join(parts)


def _latin(text: str) -> str:
    return (text.replace("·", "-").replace("–", "-").replace("—", "-").replace("’", "'")
            .replace("“", '"').replace("”", '"').encode("latin-1", "replace").decode("latin-1"))


def to_pdf(brief: Brief, path: Path) -> bool:
    try:
        from fpdf import FPDF
    except ImportError:
        return False
    import logging

    logging.getLogger("fontTools").setLevel(logging.ERROR)  # quiet font-subsetting notes
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(True, margin=14)
    pdf.add_page()
    font = next((p for p in _FONT_CANDIDATES if p.exists()), None)
    clean = (lambda s: s) if font else _latin
    if font:
        pdf.add_font("Body", "", str(font))
        pdf.add_font("Body", "B", str(font))
        family = "Body"
    else:
        family = "Helvetica"
    width = pdf.w - pdf.l_margin - pdf.r_margin

    def heading(text: str) -> None:
        pdf.ln(2)
        pdf.set_font(family, "B", 12)
        pdf.multi_cell(width, 7, clean(text), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font(family, "", 10)

    def bullet(text: str) -> None:
        pdf.multi_cell(width, 5.5, clean(f"- {text}"), new_x="LMARGIN", new_y="NEXT")

    pdf.set_font(family, "B", 15)
    pdf.multi_cell(width, 8, clean(f"Visit brief · {brief.title}"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(family, "", 10)
    pdf.multi_cell(width, 5.5, clean(
        f"{brief.doctor} · visit on {brief.visit:%d %B %Y} · covers {brief.since:%d %b} to {brief.visit:%d %b}"
    ), new_x="LMARGIN", new_y="NEXT")
    for name in ("About", "Conditions", "Medicines"):
        if brief.sections.get(name):
            heading(name)
            for item in brief.sections[name]:
                bullet(item)
    if brief.readings:
        heading("Readings logged")
        for day, bp, sugar in brief.readings:
            bullet(f"{day:%d %b}: " + ", ".join(x for x in (f"BP {bp}" if bp else "", f"sugar {sugar}" if sugar else "") if x))
        if brief.sections.get("Usual readings"):
            bullet("Usual for her: " + _usual(brief.sections["Usual readings"]))
    if brief.symptoms:
        heading("What she reported")
        for label, days in brief.symptoms.items():
            bullet(f"{label}: {len(days)} day(s) ({_days(days)})")
    if brief.questions:
        heading("Questions from the family")
        for i, question in enumerate(brief.questions, 1):
            pdf.multi_cell(width, 5.5, clean(f"{i}. {question}"), new_x="LMARGIN", new_y="NEXT")
    if brief.notes:
        heading("Daily notes")
        for day, text in brief.notes:
            bullet(f"{day:%d %b}: {text}")
    pdf.ln(3)
    pdf.set_font(family, "", 8)
    pdf.multi_cell(width, 4.5, clean(
        "Prepared by Hearth from the family's care log, for discussion with the doctor. Not medical advice. "
        "Medicines are copied from the care plan; please check them against the prescription."
    ), new_x="LMARGIN", new_y="NEXT")
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(path))
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a doctor-visit brief from the care book.")
    parser.add_argument("--doctor", required=True)
    parser.add_argument("--date", required=True, type=date.fromisoformat, help="visit date, YYYY-MM-DD")
    parser.add_argument("--since", type=date.fromisoformat, help="first day to include (default: 30 days back)")
    parser.add_argument("--question", action="append", default=[], help="a question to add (repeatable)")
    parser.add_argument("--carebook", type=Path, default=CAREBOOK)
    args = parser.parse_args(argv)

    brief = build(args.carebook, args.doctor, args.date, args.since, args.question)
    slug = re.sub(r"[^a-z0-9]+", "-", args.doctor.lower()).strip("-")
    stem = args.carebook / "visits" / f"brief-{args.date.isoformat()}-{slug}"
    stem.parent.mkdir(parents=True, exist_ok=True)
    html_path = stem.with_suffix(".html")
    html_path.write_text(to_html(brief), encoding="utf-8")
    print(f"HTML: {html_path}")
    pdf_path = stem.with_suffix(".pdf")
    if to_pdf(brief, pdf_path):
        print(f"PDF:  {pdf_path}")
    else:
        print("PDF skipped: install fpdf2 (pip install fpdf2) for a PDF.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
