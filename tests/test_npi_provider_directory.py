from scripts.import_npi_provider_directory_20261112 import (
    is_real_name,
    npi_is_valid,
    parse_source_row,
)
from app.routers.provider_directory import _conditions, _normalized_database_url


def row(**updates):
    source = {
        "npi": "1234567893",
        "first_name": "Jane",
        "middle_name": "Q",
        "last_name": "Doe",
        "credential": "MD",
        "gender": "F",
        "status": "Active",
        "specialty": "Family Medicine",
        "taxonomy_code": "207Q00000X",
        "address": "must not be imported",
        "city": "New York",
        "state": "NY",
        "zip": "100011234",
        "phone": "must not be imported",
    }
    source.update(updates)
    return source


def test_import_normalizes_zip_plus_four_and_requires_a_zip_centroid():
    record, reasons = parse_source_row(row(), {"10001": (40.75, -73.99)})
    assert reasons == []
    assert record[11:14] == ("10001", 40.75, -73.99)
    assert len(record) == 14

    no_point, reasons = parse_source_row(row(), {})
    assert no_point is None
    assert reasons == ["no_zip_centroid"]


def test_import_excludes_flagged_names_and_non_us_state_codes():
    assert is_real_name("Jane", "Doe")
    assert npi_is_valid("1234567893")
    bad_name, reasons = parse_source_row(
        row(first_name="Registered Nurse"), {"10001": (40.75, -73.99)}
    )
    assert bad_name is None
    assert "name_flag" in reasons

    foreign_state, reasons = parse_source_row(
        row(state="AB"), {"10001": (40.75, -73.99)}
    )
    assert foreign_state is None
    assert "invalid_state" in reasons


def test_directory_filters_combine_specialty_state_and_radius():
    where, params = _conditions(
        q="nurse practitioner",
        specialty="Family Nurse Practitioner",
        state_code="NY",
        zip_code="10001",
        radius_mi=25,
        center=(40.75, -73.99),
    )
    assert "p.specialty = :specialty" in where
    assert "p.state_code = :state_code" in where
    assert "p.lat BETWEEN :min_lat AND :max_lat" in where
    assert "p.lng BETWEEN :min_lng AND :max_lng" in where
    assert params["specialty"] == "Family Nurse Practitioner"
    assert params["state_code"] == "NY"
    assert params["radius_mi"] == 25
    assert params["role_prefix"] == "363L"


def test_short_credentials_search_the_credential_field_only():
    for abbreviation in ("MD", "do", "DPT"):
        where, params = _conditions(
            q=abbreviation, specialty=None, state_code=None,
            zip_code=None, radius_mi=None, center=None,
        )
        assert "upper(btrim(p.credential)) = :credential" in where
        assert "lower(p.specialty) LIKE" not in where
        assert "lower(p.first_name) LIKE" not in where
        assert params == {"credential": abbreviation.upper()}


def test_profession_abbreviation_and_full_name_use_same_taxonomy():
    pairs = (
        ("RN", "registered nurse", "163W"),
        ("NP", "nurse practitioner", "363L"),
        ("PA", "physician assistant", "363A"),
        ("PT", "physical therapist", "2251"),
        ("OT", "occupational therapist", "225X"),
        ("SLP", "speech-language pathologist", "235Z"),
        ("RRT", "registered respiratory therapist", "2279"),
        ("CRT", "certified respiratory therapist", "2278"),
        ("RD", "registered dietitian", "133V"),
        ("CNS", "clinical nurse specialist", "364S"),
    )
    for abbreviation, title, prefix in pairs:
        for term in (abbreviation, title):
            where, params = _conditions(
                q=term, specialty=None, state_code=None,
                zip_code=None, radius_mi=None, center=None,
            )
            assert "left(p.taxonomy_code, 4) = :role_prefix" in where
            assert "p.credential" not in where
            assert params == {"role_prefix": prefix}


def test_nurse_specialties_keep_distinct_taxonomies():
    for term, code in (("FNP", "363LF0000X"),
                       ("CRNA", "367500000X"),
                       ("NNP", "363LN0000X"),
                       ("PNP", "363LP0200X"),
                       ("PMHNP", "363LP0808X"),
                       ("SLPA", "2355S0801X"),
                       ("pharm tech", "183700000X")):
        where, params = _conditions(
            q=term, specialty=None, state_code=None,
            zip_code=None, radius_mi=None, center=None,
        )
        assert "p.taxonomy_code = :role_code" in where
        assert params == {"role_code": code}


def test_new_role_aliases_match_full_taxonomy_titles():
    for abbreviation, title in (
        ("NNP", "neonatal nurse practitioner"),
        ("PNP", "pediatric nurse practitioner"),
        ("PMHNP", "psychiatric/mental health nurse practitioner"),
        ("SLPA", "speech-language assistant"),
        ("pharm tech", "pharmacy technician"),
    ):
        short = _conditions(q=abbreviation, specialty=None, state_code=None,
                            zip_code=None, radius_mi=None, center=None)
        full = _conditions(q=title, specialty=None, state_code=None,
                           zip_code=None, radius_mi=None, center=None)
        assert short == full


def test_emt_search_excludes_paramedics_and_emergency_attendants():
    abbreviation = _conditions(q="EMT", specialty=None, state_code=None,
                               zip_code=None, radius_mi=None, center=None)
    full = _conditions(q="emergency medical technician", specialty=None,
                       state_code=None, zip_code=None, radius_mi=None,
                       center=None)
    assert abbreviation == full
    assert "p.taxonomy_code IN (:role_code_0, :role_code_1)" in abbreviation[0]
    assert set(abbreviation[1].values()) == {"146N00000X", "146M00000X"}
    for term, code in (("EMT-B", "146N00000X"), ("EMT-I", "146M00000X")):
        where, params = _conditions(q=term, specialty=None, state_code=None,
                                    zip_code=None, radius_mi=None, center=None)
        assert "p.taxonomy_code = :role_code" in where
        assert params == {"role_code": code}


def test_certification_searches_keep_their_credential_meaning():
    for abbreviation, title in (("CNA", "certified nursing assistant"),
                                ("CNM", "certified nurse midwife"),
                                ("LCSW", "licensed clinical social worker"),
                                ("LPC", "licensed professional counselor"),
                                ("BCBA", "board certified behavior analyst"),
                                ("RBT", "registered behavior technician"),
                                ("RDH", "registered dental hygienist")):
        short = _conditions(q=abbreviation, specialty=None, state_code=None,
                            zip_code=None, radius_mi=None, center=None)
        full = _conditions(q=title, specialty=None, state_code=None,
                           zip_code=None, radius_mi=None, center=None)
        assert short == full
        assert short[1] == {"credential": abbreviation}


def test_common_license_abbreviations_match_recorded_credentials_only():
    for abbreviation in ("PA-C", "APRN", "LMSW", "LMFT", "COTA", "OTR/L",
                         "ATC", "RDN", "PMHNP-BC"):
        where, params = _conditions(
            q=abbreviation.lower(), specialty=None, state_code=None,
            zip_code=None, radius_mi=None, center=None,
        )
        assert "upper(btrim(p.credential)) = :credential" in where
        assert "lower(p.specialty) LIKE" not in where
        assert params == {"credential": abbreviation}


def test_unknown_two_letter_search_does_not_match_inside_specialty():
    where, params = _conditions(
        q="er", specialty=None, state_code=None,
        zip_code=None, radius_mi=None, center=None,
    )
    assert "lower(p.specialty) LIKE :q0_text" in where
    assert params["q0_text"] == "er%"


def test_neon_plain_postgres_urls_use_installed_psycopg_driver():
    assert _normalized_database_url("postgres://u:p@db.example/neondb").drivername == "postgresql+psycopg"
    assert _normalized_database_url("postgresql://u:p@db.example/neondb").drivername == "postgresql+psycopg"
