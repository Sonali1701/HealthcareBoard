"""Speed organization dashboards and analytics counts."""
from alembic import op
import sqlalchemy as sa


revision = "r7b8c9d0e1f2"
down_revision = "q6a7b8c9d0e1"
branch_labels = None
depends_on = None


INDEX_NAME = "ix_audit_logs_actor_action_created"
JOB_INDEX_NAME = "ix_job_postings_employer_id"


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} "
                "ON audit_logs (actor_user_id, action, created_at)"
            )
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {JOB_INDEX_NAME} "
                "ON job_postings (employer_id)"
            )
    else:
        inspector = sa.inspect(op.get_bind())
        if not inspector.has_index("audit_logs", INDEX_NAME):
            op.create_index(INDEX_NAME, "audit_logs", ["actor_user_id", "action", "created_at"])
        if not inspector.has_index("job_postings", JOB_INDEX_NAME):
            op.create_index(JOB_INDEX_NAME, "job_postings", ["employer_id"])


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {JOB_INDEX_NAME}")
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}")
    else:
        op.drop_index(JOB_INDEX_NAME, table_name="job_postings")
        op.drop_index(INDEX_NAME, table_name="audit_logs")
