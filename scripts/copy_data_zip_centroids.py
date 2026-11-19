"""Copy non-personal ZIP centroids from the NPI database into neondb."""
from pathlib import Path

import psycopg
from sqlalchemy.engine import make_url

from scripts.recover_r2_new_provider import load_env


def main() -> None:
    values = load_env(Path(r"C:\Users\vs510\Downloads\HealthcareBoard.env"))
    raw = values["DATABASE_URL"]
    source = make_url(raw.replace("postgresql://", "postgresql+psycopg://")
                      .replace("postgres://", "postgresql+psycopg://"))
    if source.database != "neondb":
        raise RuntimeError("Expected neondb target")
    npi_url = source.set(database="healthcare_provider_directory")
    source_dsn = npi_url.render_as_string(hide_password=False).replace(
        "postgresql+psycopg://", "postgresql://")
    with psycopg.connect(source_dsn) as src, psycopg.connect(raw) as dst:
        with src.cursor() as reader, dst.cursor() as writer:
            reader.execute("SELECT zip::text, lat, lng FROM public.zip_centroids")
            points = reader.fetchall()
            if not 30000 <= len(points) <= 50000:
                raise RuntimeError("Unexpected ZIP centroid count")
            writer.execute('''CREATE TABLE IF NOT EXISTS public.data_zip_centroids (
                zip char(5) PRIMARY KEY, lat double precision NOT NULL,
                lng double precision NOT NULL)''')
            writer.execute("TRUNCATE public.data_zip_centroids")
            with writer.copy("COPY public.data_zip_centroids (zip,lat,lng) FROM STDIN") as copy:
                for row in points:
                    copy.write_row(row)
            writer.execute("SELECT count(*) FROM public.data_zip_centroids")
            loaded = writer.fetchone()[0]
            if loaded != len(points):
                raise RuntimeError("ZIP centroid copy was incomplete")
        dst.commit()
    print(f"Copied {loaded:,} ZIP centroids into neondb")


if __name__ == "__main__":
    main()
