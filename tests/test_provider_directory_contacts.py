"""Tests for queued contact lookup on NPI-only directory records."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Employer, ProviderDirectoryContactLookupJob, User, UserRole, UserStatus
from app.routers import provider_directory


class ProviderDirectoryContactRouteTests(unittest.TestCase):
    def test_directory_lookup_request_rejects_more_than_twenty(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            provider_directory.DirectoryContactLookupRequest(npis=[f"{i:010d}" for i in range(21)])

    def setUp(self):
        self.core_engine = create_engine("sqlite://")
        Base.metadata.create_all(self.core_engine)
        self.directory_engine = create_engine("sqlite://")
        with self.directory_engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE provider_directory (
                    npi CHAR(10) PRIMARY KEY, first_name TEXT, middle_name TEXT,
                    last_name TEXT, city TEXT, state_code CHAR(2), status TEXT
                )
            """))
            conn.execute(text("""
                INSERT INTO provider_directory
                VALUES ('1234567890', 'JANE', NULL, 'DOE', 'AUSTIN', 'TX', 'ACTIVE')
            """))
        self.db = Session(self.core_engine)
        self.user = User(user_id="recruiter-1", email="recruiter@example.com",
                         password_hash="x", role=UserRole.recruiter, status=UserStatus.active)
        self.other_user = User(user_id="recruiter-2", email="other@example.com",
                               password_hash="x", role=UserRole.recruiter, status=UserStatus.active)
        self.db.add_all([self.user, self.other_user])
        self.org = Employer(owner_user_id=self.user.user_id, org_name="Test Agency")
        self.db.add(self.org)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.directory_engine.dispose()
        self.core_engine.dispose()

    def test_queue_reuses_pending_and_only_owner_receives_completed_contact(self):
        request = provider_directory.DirectoryContactLookupRequest(npis=["1234567890"])
        with patch.object(provider_directory, "_engine", self.directory_engine), \
             patch.object(provider_directory.quick_sourcer, "available", return_value=True):
            first = provider_directory.enqueue_directory_contact_lookups(
                request, self.user, self.db, employer_id=self.org.employer_id)
            second = provider_directory.enqueue_directory_contact_lookups(
                request, self.user, self.db, employer_id=self.org.employer_id)
            self.assertEqual(first["jobs"][0]["job_id"], second["jobs"][0]["job_id"])
            self.assertEqual(first["jobs"][0]["status"], "queued")
            self.assertNotIn("email", first["jobs"][0])

            job = self.db.get(ProviderDirectoryContactLookupJob, first["jobs"][0]["job_id"])
            self.assertEqual(job.employer_id, self.org.employer_id)
            with self.assertRaises(HTTPException) as denied:
                provider_directory.enqueue_directory_contact_lookups(
                    request, self.other_user, self.db, employer_id=self.org.employer_id)
            self.assertEqual(denied.exception.status_code, 403)
            job.status = "completed"
            job.found = True
            job.email = "jane@example.com"
            job.phone = "+1 512 555 0100"
            self.db.commit()
            owner_result = provider_directory.directory_contact_lookup_status(
                job.job_id, self.user, self.db)
            other_result = provider_directory.directory_contact_lookup_status(
                job.job_id, self.other_user, self.db)

        self.assertEqual(owner_result["jobs"][0]["email"], "jane@example.com")
        self.assertEqual(owner_result["jobs"][0]["phone"], "+1 512 555 0100")
        self.assertEqual(other_result["jobs"], [])


if __name__ == "__main__":
    unittest.main()
