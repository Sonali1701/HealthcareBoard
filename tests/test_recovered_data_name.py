from pathlib import Path

from app.importers.parsing import parse_resume
from app.routers.recovered_data import _conditions


def test_mcsd_after_name_is_not_a_surname():
    fields = parse_resume("Jane Example, MCSD\nSenior Software Developer", Path("candidate.docx"))
    assert (fields["first_name"], fields["last_name"]) == ("Jane", "Example")


def test_rejected_recovered_records_are_excluded():
    clauses, _ = _conditions("", "", "", "", "", None, "", "", "")
    assert "review_status <> 'rejected'" in clauses
