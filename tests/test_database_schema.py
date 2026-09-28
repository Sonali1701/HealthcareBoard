from sqlalchemy import create_engine, inspect, text

from app import database
from app.models.auth import User


def test_ensure_schema_adds_required_boolean_defaults(monkeypatch, tmp_path):
    test_engine = create_engine(f"sqlite:///{tmp_path / 'pre_migration.db'}")
    database.Base.metadata.create_all(bind=test_engine)

    with test_engine.begin() as conn:
        conn.execute(
            User.__table__.insert(),
            {
                "email": "existing@example.com",
                "role": "job_seeker",
                "status": "active",
            },
        )
        conn.execute(text("ALTER TABLE users DROP COLUMN medhunt_ceipal_enabled"))
        conn.execute(text("ALTER TABLE users DROP COLUMN medhunt_nexus_enabled"))

    monkeypatch.setattr(database, "engine", test_engine)

    added = database.ensure_schema()

    columns = {column["name"]: column for column in inspect(test_engine).get_columns("users")}
    assert "users.medhunt_ceipal_enabled" in added
    assert "users.medhunt_nexus_enabled" in added
    assert columns["medhunt_ceipal_enabled"]["nullable"] is False
    assert columns["medhunt_nexus_enabled"]["nullable"] is False

    with test_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT medhunt_ceipal_enabled, medhunt_nexus_enabled "
                "FROM users WHERE email = :email"
            ),
            {"email": "existing@example.com"},
        ).one()

    assert tuple(row) == (0, 0)
