from app.repair_resume_npi_phone import _labelled_alternate_phone


def test_uses_separate_explicitly_labeled_phone():
    text = "NPI: 1932107877\nPhone: (502) 555-0184"
    assert _labelled_alternate_phone(text) == "(502) 555-0184"


def test_does_not_return_other_unlabeled_number_as_phone():
    text = "NPI: 1932107877\nLicense: 502-555-0184"
    assert _labelled_alternate_phone(text) is None


def test_returns_none_when_resume_only_has_npi():
    assert _labelled_alternate_phone("NPI: 1932107877") is None
