"""Regression cases for conservative resume taxonomy recovery."""

from app.repair_missing_provider_taxonomy import classify


def test_current_pa_title_beats_past_cna_work() -> None:
    text = ("Physician Assistant\nPreviously worked as a certified nursing assistant. "
            "Clinical examinations and patient documentation.")
    assert classify({"profession_type": "PA"}, text)[:2] == (
        "Physician Assistant", "Physician Assistant")


def test_physician_mentions_do_not_make_rn_a_physician() -> None:
    text = ("Registered Nurse\nProvided care according to physician orders. "
            "Consulted medical doctors each day about patient treatment.")
    assert classify({"profession_type": "RN"}, text)[:2] == (
        "Registered Nurse", "Nursing")


def test_conflicting_earlier_role_stays_unresolved() -> None:
    text = ("Physician Assistant\nRegistered Nurse\n"
            "This summary contains two roles and cannot identify the current one.")
    assert classify({"profession_type": "RN"}, text)[:2] == (None, None)


def test_do_verb_is_not_osteopathic_credential() -> None:
    text = ("I do patient care and do clinical documentation every day. "
            "I can do more medical office tasks when needed.")
    assert classify({"profession_type": "DO"}, text)[:2] == (None, None)


def test_physician_credential_does_not_invent_specialty() -> None:
    text = ("Physician\nDoctor of Medicine with clinical practice experience "
            "and work across several departments.")
    assert classify({"profession_type": "MD"}, text)[:2] == (None, "Physician")
