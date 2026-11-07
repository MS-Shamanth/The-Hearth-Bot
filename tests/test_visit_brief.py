import importlib.util
import sys
from datetime import date
from pathlib import Path

CAREBOOK = Path(__file__).resolve().parents[1] / "carebook"
_spec = importlib.util.spec_from_file_location("visit_brief", CAREBOOK / "tools" / "visit_brief.py")
visit_brief = importlib.util.module_from_spec(_spec)
sys.modules["visit_brief"] = visit_brief  # dataclasses look the module up while it loads
_spec.loader.exec_module(visit_brief)


def test_brief_collects_readings_symptoms_and_questions(tmp_path):
    brief = visit_brief.build(CAREBOOK, "Dr. Rao", date(2026, 11, 12), date(2026, 10, 20), ["Painkiller for the knee?"])
    assert any("Telmisartan" in m for m in brief.sections["Medicines"])
    assert (date(2026, 11, 3), "150/92", "") in brief.readings
    assert len(brief.symptoms["knee or joint pain"]) == 3
    assert brief.questions[0] == "Painkiller for the knee?"
    assert any("knee" in q.lower() for q in brief.questions[1:])
    html = visit_brief.to_html(brief)
    assert "Not medical advice" in html
    assert visit_brief.to_pdf(brief, tmp_path / "brief.pdf")
    assert (tmp_path / "brief.pdf").stat().st_size > 1000
