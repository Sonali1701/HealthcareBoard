from app.importers.parsing import _phone_match_excluding_npi


def test_does_not_extract_labeled_npi_as_phone():
    text = "Provider Information\nNPI: 1932107877\nCredential: MD"
    assert _phone_match_excluding_npi(text) is None


def test_extracts_phone_elsewhere_after_skipping_npi():
    text = "NPI: 1932107877\nPhone: (502) 555-0184"
    match = _phone_match_excluding_npi(text)
    assert match is not None
    assert match.group(0) == "(502) 555-0184"
