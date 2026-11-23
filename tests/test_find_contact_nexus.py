"""Successful directory lookups hand verified identities to the Nexus queue."""
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AtsOrganizationIntegration, Employer, NexusContactDelivery, User
from app.services import contact_lookup_queue, nexus_contact_delivery


def _match():
    return SimpleNamespace(email="jane@example.org", phone="5551234567")


def test_npi_contact_uses_source_identity_and_deduplication_key():
    row = {"first_name": "Jane Marie", "last_name": "Doe", "credential": "RN",
           "specialty": "Registered Nurse", "city": "Austin", "state_code": "TX",
           "zip_code": "78701"}
    conn = mock.MagicMock()
    conn.execute.return_value.mappings.return_value.first.return_value = row
    engine = mock.MagicMock()
    engine.connect.return_value.__enter__.return_value = conn
    user = SimpleNamespace(medhunt_nexus_enabled=True)
    with mock.patch.dict("sys.modules", {"app.routers.provider_directory":
                                       SimpleNamespace(_engine_or_503=lambda: engine)}), \
         mock.patch.object(nexus_contact_delivery, "enqueue_identity") as enqueue:
        contact_lookup_queue._queue_found_contact(mock.MagicMock(), user,
                                                  "npi:1234567890", "Jane Doe",
                                                  "Austin, TX", _match())
    key, identity = enqueue.call_args.args[2:]
    assert key == "npi:1234567890"
    assert identity["first_name"] == "Jane Marie"
    assert identity["specialty"] == "Registered Nurse"
    assert identity["email"] == "jane@example.org"


def test_data_contact_includes_original_resume_and_taxonomy():
    row = {"first_name": "Sam", "last_name": "Lee", "credential": "NP",
           "profession": "Nurse Practitioner", "specialty": "Family Medicine",
           "job_category": "APP", "city": "Dallas", "state_code": "TX",
           "zip_code": "75201", "npi_number": "", "resume_url": "r2://resume.pdf"}
    db = mock.MagicMock()
    db.execute.return_value.mappings.return_value.first.return_value = row
    user = SimpleNamespace(medhunt_nexus_enabled=True)
    with mock.patch.object(nexus_contact_delivery, "enqueue_identity") as enqueue:
        contact_lookup_queue._queue_found_contact(db, user, "profile:abc",
                                                  "Sam Lee", "Dallas, TX", _match())
    key, identity = enqueue.call_args.args[2:]
    assert key == "profile:abc"
    assert identity["resume_url"] == "r2://resume.pdf"
    assert identity["profession"] == "Nurse Practitioner"


def test_disabled_nexus_connection_does_not_queue_contact():
    with mock.patch.object(nexus_contact_delivery, "enqueue_identity") as enqueue:
        contact_lookup_queue._queue_found_contact(mock.MagicMock(),
                                                  SimpleNamespace(medhunt_nexus_enabled=False),
                                                  "npi:1234567890", "Jane Doe", "Austin, TX", _match())
    enqueue.assert_not_called()


def test_repeated_lookups_queue_only_one_delivery_per_nexus_tenant():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            user = User(email="recruiter@example.org", medhunt_nexus_enabled=True)
            db.add(user)
            db.flush()
            employer = Employer(owner_user_id=user.user_id, org_name="Test Clinic")
            db.add(employer)
            db.flush()
            integration = AtsOrganizationIntegration(
                employer_id=employer.employer_id, provider="nexus",
                settings_encrypted="test",
            )
            db.add(integration)
            db.flush()
            target = (employer, integration)
            identity = {"first_name": "Jane", "last_name": "Doe",
                        "email": "jane@example.org"}
            with mock.patch.object(nexus_contact_delivery.ats_connections,
                                   "for_user", return_value=target):
                assert nexus_contact_delivery.enqueue_identity(db, user, "npi:1234567890", identity)
                assert not nexus_contact_delivery.enqueue_identity(db, user, "npi:1234567890", identity)
            rows = db.scalars(select(NexusContactDelivery)).all()
            assert len(rows) == 1
            assert rows[0].payload["email"] == "jane@example.org"
    finally:
        engine.dispose()
