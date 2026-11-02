"""Migration-chain regression tests."""
from __future__ import annotations

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from app.config import settings


def test_fresh_database_upgrades_to_head(tmp_path, monkeypatch):
    database_path = tmp_path / "fresh.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setattr(settings, "database_url", database_url)

    config = Config("alembic.ini")
    command.upgrade(config, "head")

    tables = set(inspect(create_engine(database_url)).get_table_names())
    assert "team_invites" in tables
    assert "organization_teams" in tables
