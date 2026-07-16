"""Copy data from an old Neon/Postgres database into the current DATABASE_URL.

Run from the project root in PyCharm terminal:

    .venv\\Scripts\\python -m app.migrate_neon_data

Expected .env values:

    OLD_DATABASE_URL=postgresql://...old-neon...?sslmode=require
    DATABASE_URL=postgresql://...new-neon...?sslmode=require

This uses pg_dump/pg_restore, so PostgreSQL client tools must be installed and
available on PATH. The script is intentionally repo-based: it re-runs Alembic
after restore so the new database ends at this codebase's current schema.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DUMP = ROOT / "neon_migration.dump"
COUNT_TABLES = (
    "users",
    "profiles",
    "licenses",
    "certifications",
    "work_history",
    "profile_skills",
    "employers",
    "job_postings",
    "applications",
    "messages",
    "notifications",
)


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _postgres_url(url: str) -> str:
    url = (url or "").strip().strip('"').strip("'")
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url[len("postgresql+psycopg://") :]
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return url
    raise SystemExit("ERROR: database URL must start with postgresql:// or postgres://")


def _sqlalchemy_url(url: str) -> str:
    url = _postgres_url(url)
    return "postgresql+psycopg://" + url[len("postgresql://") :]


def _safe_label(url: str) -> str:
    parsed = urlparse(_postgres_url(url))
    host = parsed.hostname or "(missing-host)"
    db = (parsed.path or "").lstrip("/") or "(missing-db)"
    user = parsed.username or "(missing-user)"
    pool = "pooler" if "-pooler" in host else "direct"
    sslmode = dict(parse_qsl(parsed.query)).get("sslmode", "")
    return f"{user}@{host}/{db} [{pool}, sslmode={sslmode or 'not-set'}]"


def _hide_password(url: str) -> str:
    parsed = urlparse(_postgres_url(url))
    if parsed.password is None:
        return urlunparse(parsed)
    netloc = parsed.netloc.replace(f":{parsed.password}@", ":***@")
    return urlunparse(parsed._replace(netloc=netloc))


def _require_tool(name: str) -> None:
    if shutil.which(name):
        return
    raise SystemExit(
        f"ERROR: '{name}' was not found on PATH.\n"
        "Install PostgreSQL client tools, then reopen PyCharm terminal."
    )


def _run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
    shown = " ".join(_hide_password(x) if x.startswith(("postgres://", "postgresql://")) else x for x in cmd)
    print(f"\n> {shown}")
    subprocess.run(cmd, cwd=ROOT, env=env, check=True)


def _confirm(old_url: str, new_url: str, yes: bool) -> None:
    print("Old database:", _safe_label(old_url))
    print("New database:", _safe_label(new_url))
    print()
    print("WARNING: restore can replace tables/data in the NEW database.")
    print("Cloudflare R2 files are not copied here; this only copies Postgres data.")
    if yes:
        return
    answer = input("Type MIGRATE to continue: ").strip()
    if answer != "MIGRATE":
        raise SystemExit("Cancelled.")


def _table_counts(db_url: str) -> dict[str, int | None]:
    try:
        import psycopg
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "ERROR: psycopg is missing. Run: .venv\\Scripts\\python -m pip install -r requirements.txt"
        ) from exc

    counts: dict[str, int | None] = {}
    with psycopg.connect(_postgres_url(db_url)) as conn:
        with conn.cursor() as cur:
            for table in COUNT_TABLES:
                cur.execute("select to_regclass(%s)", (table,))
                if cur.fetchone()[0] is None:
                    counts[table] = None
                    continue
                cur.execute(f'select count(*) from "{table}"')
                counts[table] = int(cur.fetchone()[0])
    return counts


def _print_counts(title: str, counts: dict[str, int | None]) -> None:
    print(f"\n{title}")
    for table, count in counts.items():
        if count is not None:
            print(f"  {table}: {count}")


def _copy_with_python(old_url: str, new_url: str, *, batch_size: int, clean: bool) -> None:
    """Fallback copy path for machines without pg_dump/pg_restore installed."""
    try:
        from sqlalchemy import MetaData, create_engine, select
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "ERROR: SQLAlchemy is missing. Run: .venv\\Scripts\\python -m pip install -r requirements.txt"
        ) from exc

    old_engine = create_engine(_sqlalchemy_url(old_url), future=True)
    new_engine = create_engine(_sqlalchemy_url(new_url), future=True)
    old_meta = MetaData()
    new_meta = MetaData()
    old_meta.reflect(bind=old_engine)
    new_meta.reflect(bind=new_engine)

    table_names = [
        t.name
        for t in old_meta.sorted_tables
        if t.name != "alembic_version" and t.name in new_meta.tables
    ]
    if not table_names:
        raise SystemExit("ERROR: no matching tables found between old and new databases.")

    if clean:
        print("\nCleaning rows from new database tables...")
        with new_engine.begin() as conn:
            for table in reversed(new_meta.sorted_tables):
                if table.name == "alembic_version" or table.name not in table_names:
                    continue
                conn.execute(table.delete())

    print("\nCopying data with Python fallback...")
    with old_engine.connect() as old_conn:
        with new_engine.begin() as new_conn:
            for table_name in table_names:
                old_table = old_meta.tables[table_name]
                new_table = new_meta.tables[table_name]
                common_cols = [c.name for c in old_table.columns if c.name in new_table.columns]
                copied = 0
                batch: list[dict] = []
                result = old_conn.execution_options(stream_results=True).execute(select(old_table))
                for row in result:
                    mapping = row._mapping
                    batch.append({col: mapping[col] for col in common_cols})
                    if len(batch) >= batch_size:
                        new_conn.execute(new_table.insert(), batch)
                        copied += len(batch)
                        batch.clear()
                if batch:
                    new_conn.execute(new_table.insert(), batch)
                    copied += len(batch)
                print(f"  {table_name}: {copied}")


def _run_alembic(new_url: str) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = _sqlalchemy_url(new_url)
    _run([sys.executable, "-m", "alembic", "upgrade", "head"], env=env)


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate old Neon data into current DATABASE_URL.")
    parser.add_argument("--env-file", default=".env", help="Env file to read. Default: .env")
    parser.add_argument("--old-url", default="", help="Override OLD_DATABASE_URL")
    parser.add_argument("--new-url", default="", help="Override DATABASE_URL")
    parser.add_argument("--dump-file", default=str(DEFAULT_DUMP), help="Dump file path")
    parser.add_argument("--overwrite-dump", action="store_true", help="Allow replacing an existing dump file")
    parser.add_argument("--skip-dump", action="store_true", help="Use an existing dump file")
    parser.add_argument("--skip-restore", action="store_true", help="Only create the dump and show counts")
    parser.add_argument("--no-clean", action="store_true", help="Do not clean/drop existing objects before restore")
    parser.add_argument("--no-alembic", action="store_true", help="Do not run Alembic after restore")
    parser.add_argument("--counts-only", action="store_true", help="Only print old/new row counts")
    parser.add_argument("--method", choices=["auto", "pg", "python"], default="auto", help="Migration method")
    parser.add_argument("--batch-size", type=int, default=1000, help="Rows per Python-copy insert batch")
    parser.add_argument("--yes", action="store_true", help="Skip MIGRATE confirmation prompt")
    args = parser.parse_args()

    _load_env((ROOT / args.env_file).resolve() if not Path(args.env_file).is_absolute() else Path(args.env_file))

    old_url = _postgres_url(args.old_url or os.environ.get("OLD_DATABASE_URL", ""))
    new_url = _postgres_url(args.new_url or os.environ.get("DATABASE_URL", ""))
    if old_url == new_url:
        raise SystemExit("ERROR: OLD_DATABASE_URL and DATABASE_URL point to the same database.")

    _confirm(old_url, new_url, args.yes or args.counts_only)

    old_counts = _table_counts(old_url)
    new_counts_before = _table_counts(new_url)
    _print_counts("Old database counts", old_counts)
    _print_counts("New database counts before restore", new_counts_before)

    if args.counts_only:
        print("\nCounts-only mode: no data was copied.")
        return

    pg_tools_available = bool(shutil.which("pg_dump") and shutil.which("pg_restore"))
    use_python_copy = args.method == "python" or (args.method == "auto" and not pg_tools_available)

    if use_python_copy:
        if args.method == "auto":
            print("\npg_dump/pg_restore were not found, so using the Python copy fallback.")
        if not args.no_alembic:
            _run_alembic(new_url)
        _copy_with_python(
            old_url,
            new_url,
            batch_size=max(args.batch_size, 1),
            clean=not args.no_clean,
        )
        if not args.no_alembic:
            _run_alembic(new_url)
        new_counts_after = _table_counts(new_url)
        _print_counts("New database counts after restore", new_counts_after)
        print("\nDone. Restart the app so it uses the restored Neon data.")
        return

    _require_tool("pg_dump")
    _require_tool("pg_restore")

    dump_file = Path(args.dump_file)
    if not dump_file.is_absolute():
        dump_file = ROOT / dump_file

    if not args.skip_dump:
        if dump_file.exists() and not args.overwrite_dump:
            raise SystemExit(
                f"ERROR: dump file already exists: {dump_file}\n"
                "Use --overwrite-dump or choose --dump-file with another name."
            )
        _run([
            "pg_dump",
            "--dbname",
            old_url,
            "--format=custom",
            "--no-owner",
            "--no-acl",
            "--file",
            str(dump_file),
        ])

    if args.skip_restore:
        print(f"\nDump created at: {dump_file}")
        return

    restore_cmd = [
        "pg_restore",
        "--dbname",
        new_url,
        "--no-owner",
        "--no-acl",
        "--verbose",
    ]
    if not args.no_clean:
        restore_cmd.extend(["--clean", "--if-exists"])
    restore_cmd.append(str(dump_file))
    _run(restore_cmd)

    if not args.no_alembic:
        _run_alembic(new_url)

    new_counts_after = _table_counts(new_url)
    _print_counts("New database counts after restore", new_counts_after)
    print("\nDone. Restart the app so it uses the restored Neon data.")


if __name__ == "__main__":
    main()
