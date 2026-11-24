from pathlib import Path

from app.importers.parsing import parse_resume
from app.routers.recovered_data import _conditions


def test_mcsd_after_name_is_not_a_surname():
    fields = parse_resume("Jane Example, MCSD\nSenior Software Developer", Path("candidate.docx"))
    assert (fields["first_name"], fields["last_name"]) == ("Jane", "Example")


def test_rejected_recovered_records_are_excluded():
    clauses, _ = _conditions("", "", "", "", "", None, "", "", "")
    assert "review_status <> 'rejected'" in clauses


def test_high_risk_resume_screen_flags_are_excluded_from_data_results():
    clauses, _ = _conditions("", "", "", "", "", None, "", "", "")
    screen = next(clause for clause in clauses if "New_provider_resume_screen" in clause)
    assert "record_type <> 'profile'" in screen  # license rows have no resumes
    for flag in ("missing_name", "malformed_name", "credential_as_surname",
                 "nonclinical_technical_header", "header_category_conflict"):
        assert flag in screen
    assert "missing_specialty" not in screen
    assert "source_is_not_resume" not in screen
